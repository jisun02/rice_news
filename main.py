# 제목과 링크만 제공하는 것은 '단순 링크' 또는 '딥 링크'에 해당하여
# 저작권 침해로 보지 않는다는 것이 대법원의 판례이다.
# 팀원들이 제목을 보고 관심 있는 기사의 링크를 클릭하면
# 해당 언론사 홈페이지로 이동해서 트래픽을 발생시켜 주기 때문에 언론사 입장에서도 문제 삼지 않는다고 한다.
# 따라서 직접 스크래퍼나 봇을 개발하실 때는 반드시
# "본문은 가져오지 않고, 제목과 링크 위주로만 Teams에 쏴준다"는 원칙만 지키면
# 회사에서 안전하게 사용할 수 있다고 한다.

import feedparser
import os
import re
import html
from email.utils import parsedate
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, BackgroundTasks
from openai import OpenAI
import json
import requests
import urllib3
import logging
import time

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

KST = timezone(timedelta(hours=9))

# 로그 설정
os.makedirs("logs", exist_ok=True)
log_filename = f"logs/news_log_{datetime.now().strftime('%Y-%m-%d')}.log"
logging.Formatter.converter = lambda *args: datetime.now(KST).timetuple()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_filename, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------
# 설정
# ---------------------------
RSS_LIST = [
    "http://www.newsfarm.co.kr/rss/allArticle.xml",
    "http://www.farminsight.net/rss/allArticle.xml"
    # "https://news.google.com/rss/search?q=쌀+when:1d&hl=ko&gl=KR&ceid=KR:ko",
    # "https://news.google.com/rss/search?q=양곡+when:7d&hl=ko&gl=KR&ceid=KR:ko",
    # "https://news.google.com/rss/search?q=TRQ+when:7d&hl=ko&gl=KR&ceid=KR:ko"
]

# 새로 추가된 네이버 API용 고도화 키워드
NAVER_KEYWORDS = ["쌀", "벼", "쌀 수매", "쌀 수출", "쌀 생산", "쌀값", "쌀 작황", "구곡",
    "양곡관리", "미곡처리장", "TRQ", "도매 쌀값", "쌀 재고", "벼 재배",
    "농식품부 인사", "농촌진흥청 인사", "농촌경제연구원 인사", "한국농수산식품유통공사 인사",
    "농식품부 장관 인터뷰", "쌀 정책 인터뷰", "식량 정책 인터뷰", "aT 쌀"]

SOURCE_DAY_RULE = {
    "http://www.newsfarm.co.kr/rss/allArticle.xml": 1,
    "http://www.farminsight.net/rss/allArticle.xml": 1
}

KEYWORDS = ["쌀", "벼", "곡물", "농업", "미곡", "미", "양곡", "정부",
            "비축", "TRQ", "수급", "식량", "물가", "농산물", "GMO",
            "인사", "인터뷰", "취임", "발탁", "전보", "농식품부", "농림축산식품부",
            "농진청", "농촌진흥청", "농경연", "농촌경제연구원", "한국농어촌공사", "농어촌공사"]
BANNED_WORDS = ["vietnam", "기부", "나눔"]

SYSTEM_PROMPT = """You are an expert Rice Trade & Market Intelligence Analyst.
Your task is to FILTER irrelevant news and REMOVE redundant duplicates, while KEEPING AS MANY diverse articles as possible.

[Deduplication Rules]
- Be inclusive: Keep multiple articles on the same topic if they offer different perspectives or angles.
- MERGE AND REMOVE DUPLICATES IF they report the exact same specific local event or press release.
- Pick ONLY ONE representative article and DISCARD the rest for identical core factual events.

[Selection Rules - INCLUDE]
1. TRQ (Tariff-Rate Quota): Include ALL news mentioning TRQ.
2. Economy: Material prices, rice production costs, budget/subsidies, grain market trends.
3. Industry: Processed rice products, new varieties, export, consumption trends.
4. Market: Production, stockpile, rice market prices, stabilization, government policy.
5. Personnel: Changes/interviews in MAFRA, RDA, KREI, etc.

[Selection Rules - EXCLUDE]
1. General agriculture policy not specific to rice.
2. Farmer welfare, education, events.
3. Government meetings without rice relevance.
4. Non-food use of rice.

Output a FLAT JSON ARRAY ONLY.
MUST include the original 'id' exactly as provided.
Example format: [{"id": 0, "title": "...", "url": "..."}, {"id": 1, "title": "...", "url": "..."}]"""

# ---------------------------
# Teams 알림 유틸리티
# ---------------------------
def send_teams_log(message):
    """Teams 웹훅으로 로그 메시지를 전송합니다."""
    webhook_url = os.getenv("TEAMS_WEBHOOK_URL")
    
    if not webhook_url:
        logging.warning("⚠️ TEAMS_WEBHOOK_URL 환경변수가 없어 Teams 알림을 생략합니다. (Render 설정을 확인하세요)")
        return 

    try:
        res = requests.post(webhook_url, json={"message": message}, timeout=10)
        res.raise_for_status() 
    except Exception as e:
        logging.error(f"❌ Teams 알림 전송 실패: {e}")


def send_news_to_pa(articles):
    """최종 선정된 뉴스를 Power Automate(Teams 전송용)로 쏴주는 함수"""
    news_webhook_url = os.getenv("PA_NEWS_WEBHOOK_URL")
    if not news_webhook_url:
        logging.error("뉴스 전송용 웹훅 URL이 없습니다.")
        return

    try:
        # 뉴스 배열(리스트)을 그대로 Power Automate로 전송
        requests.post(news_webhook_url, json=articles, timeout=10)
        logging.info("✅ 최종 뉴스를 Power Automate로 성공적으로 전송했습니다.")
    except Exception as e:
        logging.error(f"❌ 뉴스 전송 실패: {e}")

# ---------------------------
# 수집 및 필터 함수들
# ---------------------------
def fetch_rss():
    articles = []
    headers = {"User-Agent": "Mozilla/5.0"}
    for url in RSS_LIST:
        try:
            res = requests.get(url, headers=headers, verify=False, timeout=10)
            feed = feedparser.parse(res.content)
            logging.info(f"수집 시작 ({url}): {len(feed.entries)}건")
            for entry in feed.entries:
                articles.append({
                    "title": entry.title,
                    "url": entry.link,
                    "published": entry.get("published_parsed", None),
                    "source": url
                })
        except Exception as e:
            logging.error(f"RSS 수집 에러 ({url}): {e}")
    return articles

def fetch_naver_news():
    """네이버 검색 API를 활용해 뉴스를 수집합니다."""
    client_id = os.getenv("NAVER_CLIENT_ID")
    client_secret = os.getenv("NAVER_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        logging.warning("⚠️ 네이버 API 키(NAVER_CLIENT_ID, NAVER_CLIENT_SECRET)가 설정되지 않아 네이버 검색을 건너뜁니다.")
        return []

    articles = []
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret
    }

    for keyword in NAVER_KEYWORDS:
        params = {
            "query": keyword,
            "display": 12,  # 서비스용: 키워드당 12개 추출
            "sort": "date"
        }
        
        try:
            # verify=False 를 추가해서 사내망 SSL 에러 무시
            res = requests.get(url, headers=headers, params=params, verify=False, timeout=10)
            
            if res.status_code != 200:
                logging.error(f"❌ 네이버 API 요청 실패 ({keyword}): 상태 코드 {res.status_code} - {res.text}")
                continue
                
            data = res.json()
            items = data.get("items", [])
            logging.info(f"네이버 API 수집 ({keyword}): {len(items)}건")
            
            for item in items:
                # 1. HTML 태그 제거 (<b>쌀</b> -> 쌀)
                clean_title = html.unescape(re.sub(r'<[^>]+>', '', item["title"]))
                
                # 2. 날짜 포맷 변환 (RSS 형태와 맞춤)
                published_tuple = parsedate(item["pubDate"]) if item.get("pubDate") else None
                
                # 3. 원문 링크 우선순위 적용
                article_url = item.get("originallink") or item.get("link")

                articles.append({
                    "title": clean_title,
                    "url": article_url,
                    "published": published_tuple,
                    "source": f"Naver API ({keyword})"
                })
        except Exception as e:
            logging.error(f"❌ 네이버 API 수집 에러 ({keyword}): {e}")
            
    return articles

def filter_date(articles):
    now = datetime.utcnow()
    # Naver API 출처는 SOURCE_DAY_RULE에 없으므로 기본값 7일이 적용됩니다. (필요시 조정 가능)
    result = [a for a in articles if a["published"] and (now - datetime(*a["published"][:6])) <= timedelta(days=SOURCE_DAY_RULE.get(a["source"], 7))]
    return result

def filter_keywords(articles):
    before = len(articles)
    result, removed = [], []
    for a in articles:
        if any(k in a["title"] for k in KEYWORDS):
            result.append(a)
        else:
            removed.append(a["title"])
            
    logging.info(f"\n🔍 [KEYWORD FILTER]")
    logging.info(f"입력: {before} → 출력: {len(result)}")

    for r in removed:
        logging.info(f" - {r}")
        
    return result

def filter_banned(articles):
    before = len(articles)
    result, removed = [], []
    for a in articles:
        if any(b in (a["title"] + " " + a["url"]).lower() for b in BANNED_WORDS):
            removed.append(a["title"])
        else:
            result.append(a)
            
    logging.info(f"\n🚫 [BANNED WORDS FILTER]")
    logging.info(f"입력: {before} → 출력: {len(result)}")

    for r in removed:
        logging.info(f" - {r}")
        
    return result

# ---------------------------
# GPT 필터
# ---------------------------
def ai_filter(articles):
    if not articles:
        return []

    before = len(articles)
    final_result_ids = set() # GPT가 살려둔 ID들을 담을 바구니

    # 🌟 1. 100개씩 Chunking 분할
    chunk_size = 100
    chunks = [articles[i:i + chunk_size] for i in range(0, len(articles), chunk_size)]

    for chunk_idx, chunk in enumerate(chunks):
        compact_articles = []
        for a in chunk:
            clean_title = a["title"]
            clean_title = re.sub(r'\[.*?\]|\(.*?\)', '', clean_title)
            clean_title = re.sub(r'\s*[-|>|ⓒ]\s*[^-|>|ⓒ]*$', '', clean_title).strip()
            
            # 🌟 2. GPT에게 보낼 때는 가볍게 (id, title, url만)
            compact_articles.append({"id": a["id"], "title": clean_title, "url": a["url"]})

        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0,
                max_tokens=8000,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(compact_articles, ensure_ascii=False)}
                ]
            )
            content = response.choices[0].message.content
        except Exception as e:
            error_msg = f"🚨 OpenAI 호출 실패 (Chunk {chunk_idx+1}): {e}"
            logging.error(error_msg)
            send_teams_log(error_msg)
            continue

        if content.startswith("```"):
            content = content.split("```")[1].replace("json", "").strip()

        try:
            result = json.loads(content, strict=False)
            
            # 작성하신 강제 평탄화 로직
            flattened_result = []
            if isinstance(result, dict):
                for k, v in result.items():
                    if isinstance(v, list):
                        flattened_result.extend(v)
                result = flattened_result
            elif isinstance(result, list) and len(result) > 0 and "articles" in result[0]:
                for item in result:
                    if "articles" in item and isinstance(item["articles"], list):
                        flattened_result.extend(item["articles"])
                    else:
                        flattened_result.append(item)
                result = flattened_result
                
            # 🌟 3. 성공한 결과에서 ID만 추출하여 세트에 추가
            for item in result:
                if isinstance(item, dict) and "id" in item:
                    final_result_ids.add(item["id"])

        except Exception as e:
            error_msg = f"❌ JSON 파싱 에러 (Chunk {chunk_idx+1}): {e}"
            logging.error(error_msg)
            send_teams_log(error_msg)
            continue

    # 🌟 4. ID를 기준으로 원본 기사(published, source 포함) 완벽 복원
    final_articles = [a for a in articles if a.get("id") in final_result_ids]
    removed = [a["title"] for a in articles if a.get("id") not in final_result_ids]

    logging.info(f"\n🤖 [GPT FILTER]")
    logging.info(f"입력: {before} → 출력: {len(final_articles)}")
    logging.info(f"제거됨 ({len(removed)}개):")
    for r in removed: # 너무 길면 [:20] 등으로 자를 수 있습니다.
        logging.info(f" - {r}")

    return final_articles

# ---------------------------
# 메인 작업 (뉴스 수집 및 웹훅 전송)
# ---------------------------
def background_news_job():
    start_time = datetime.now(KST).strftime('%H:%M:%S')
    send_teams_log(f"🔄 뉴스 수집 프로세스 시작 ({start_time})")

    try:
        # 1. 수집 (RSS + 네이버 API 병합)
        rss_articles = fetch_rss()
        naver_articles = fetch_naver_news()
        articles = rss_articles + naver_articles

        # 🌟 수집 직후 모든 기사에 고유 ID 부여 (가장 안전한 위치)
        for idx, a in enumerate(articles):
            a["id"] = idx

        initial_count = len(articles)
        
        # 2. 1차 필터링
        articles = filter_date(articles)
        articles = filter_banned(articles)
        articles = filter_keywords(articles)
        
        logging.info(f"1차 필터 통과: {len(articles)}건")

        # 3. AI 필터링
        final_articles = ai_filter(articles)
        final_count = len(final_articles)

        # 4. 완료 보고
        status_msg = f"✅ 뉴스 필터링 완료!\n- 최초 수집: {initial_count}건\n- AI 최종 선정: {final_count}건"
        logging.info(status_msg)
        send_teams_log(status_msg)

        if final_count > 0:
            send_news_to_pa(final_articles)

    except Exception as e:
        error_msg = f"🔥 백그라운드 작업 에러: {e}"
        logging.error(error_msg)
        send_teams_log(error_msg)

# ---------------------------
# 메인 API 엔드포인트
# ---------------------------
@app.get("/news")
def trigger_news(background_tasks: BackgroundTasks):
    # 뒤에서 일할 작업(background_news_job)을 대기열에 등록
    background_tasks.add_task(background_news_job)
    # 0.1초 만에 바로 응답! (타임아웃 발생 절대 안 함)
    return {"message": "주문 접수 완료. 백그라운드에서 뉴스를 수집합니다."}