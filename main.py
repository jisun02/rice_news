# 제목과 링크만 제공하는 것은 '단순 링크' 또는 '딥 링크'에 해당하여
# 저작권 침해로 보지 않는다는 것이 대법원의 판례이다.
# 팀원들이 제목을 보고 관심 있는 기사의 링크를 클릭하면
# 해당 언론사 홈페이지로 이동해서 트래픽을 발생시켜 주기 때문에 언론사 입장에서도 문제 삼지 않는다고 한다.
# 따라서 직접 스크래퍼나 봇을 개발하실 때는 반드시
# "본문은 가져오지 않고, 제목과 링크 위주로만 Teams에 쏴준다"는 원칙만 지키면
# 회사에서 안전하게 사용하실 수 있다고 한다.

import feedparser
import os
import re
import html
from email.utils import parsedate
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
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
NAVER_KEYWORDS = [
    "쌀 수매", 
    "쌀 수출",
    "쌀 생산",
    "쌀값", 
    "쌀 작황",
    "구곡"
    "양곡관리", 
    "미곡처리장", 
    "TRQ"
]

SOURCE_DAY_RULE = {
    "http://www.newsfarm.co.kr/rss/allArticle.xml": 1,
    "http://www.farminsight.net/rss/allArticle.xml": 1
}

KEYWORDS = ["쌀", "벼", "곡물", "농업", "미곡", "미", "양곡", "정부", "비축", "TRQ", "수급", "식량", "물가", "농산물"]
BANNED_WORDS = ["vietnam", "기부", "나눔"]

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
            "display": 15,  # 서비스용: 키워드당 15개 추출
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

def filter_banned(articles):
    before = len(articles)
    result = []
    removed = []
    for a in articles:
        if any(b in (a["title"] + " " + a["url"]).lower() for b in BANNED_WORDS):
            removed.append(a["title"])
        else:
            result.append(a)
            
    logging.info(f"\n🚫 [BANNED WORDS FILTER]")
    logging.info(f"입력: {before} → 출력: {len(result)}")
    logging.info(f"제거됨 ({len(removed)}개):")
    for r in removed:
        logging.info(f" - {r}")
        
    return result

def filter_keywords(articles):
    before = len(articles)
    result = []
    removed = []
    for a in articles:
        if any(k in a["title"] for k in KEYWORDS):
            result.append(a)
        else:
            removed.append(a["title"])
            
    logging.info(f"\n🔍 [KEYWORD FILTER]")
    logging.info(f"입력: {before} → 출력: {len(result)}")
    logging.info(f"제거됨 ({len(removed)}개):")
    for r in removed:
        logging.info(f" - {r}")
        
    return result

# ---------------------------
# GPT 필터 (알림 강화)
# ---------------------------
def ai_filter(articles):
    if not articles:
        return []

    before = len(articles)

    # 정규식(re)을 사용해 맨 뒤 언론사 꼬리표와 앞의 [속보] 등을 깔끔하게 제거합니다.
    compact_articles = []
    for a in articles:
        clean_title = a["title"]
        # 1. 괄호 안의 단어 제거 (예: [속보], (종합))
        clean_title = re.sub(r'\[.*?\]|\(.*?\)', '', clean_title)
        # 2. 맨 뒤에 붙은 하이픈(-), 파이프(|), 꺾쇠(>) 뒤의 언론사명 제거
        clean_title = re.sub(r'\s*[-|>|ⓒ]\s*[^-|>|ⓒ]*$', '', clean_title).strip()
        
        compact_articles.append({"title": clean_title, "url": a["url"]})

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            max_tokens=8000,
            messages=[
                {
                    "role": "system", 
                    "content": """You are an expert Rice news editor.
Your task is to FILTER irrelevant news and REMOVE redundant duplicates, while KEEPING AS MANY diverse articles as possible.

[Deduplication Rules]
- Be inclusive: Keep multiple articles on the same topic if they offer different perspectives or angles.
- MERGE AND REMOVE DUPLICATES IF they report the exact same specific local event or press release (e.g., "A specific city exports rice to Australia" or "A specific brand launches a new product"). 
- Even if the headlines use slightly different words, if the core factual event is identical, pick ONLY ONE representative article and DISCARD the rest.

[Selection Rules - INCLUDE]
1. TRQ (Tariff-Rate Quota): Include ALL news mentioning TRQ, even if it's about other crops like soybeans (콩) or wheat.
2. Agricultural material prices, rice production costs (생산비/농사 비용), farm profitability, agricultural budget/subsidies, and general grain/crop market trends.
3. Processed rice products, new rice varieties, export/market expansion, and rice consumption trends.
4. Production, stockpile, rice market prices (쌀값), price stabilization, and government rice policy.
5. Rice varieties, cultivation, climate impact on rice
6. Rice industry, distribution, exports, market expansion

[Selection Rules - EXCLUDE]
1. General agriculture policy not specific to rice
2. Farmer welfare, education, events
3. Government meetings or plans without rice relevance
4. Non-food use of rice (cosmetics, beauty, etc.)

Output a FLAT JSON ARRAY ONLY.
Example format: [{"title": "...", "url": "..."}, {"title": "...", "url": "..."}]"""

                },
                {"role": "user", "content": json.dumps(compact_articles, ensure_ascii=False)}
            ]
        )
        content = response.choices[0].message.content
    except Exception as e:
        error_msg = f"🚨 OpenAI 호출 실패: {e}"
        logging.error(error_msg)
        send_teams_log(error_msg)
        return []

    if content.startswith("```"):
        content = content.split("```")[1].replace("json", "").strip()

    try:
        result = json.loads(content, strict=False)
        
        # 만약 GPT가 지시를 무시하고 2단 구조(카테고리/articles)로 보낼 경우를 대비한 강제 평탄화 로직
        flattened_result = []
        if isinstance(result, dict):
            # {"category": [{"title": ...}]} 형태인 경우
            for k, v in result.items():
                if isinstance(v, list):
                    flattened_result.extend(v)
            result = flattened_result
        elif isinstance(result, list) and len(result) > 0 and "articles" in result[0]:
            # [{"category": "TRQ", "articles": [...]}] 형태인 경우
            for item in result:
                if "articles" in item and isinstance(item["articles"], list):
                    flattened_result.extend(item["articles"])
                else:
                    flattened_result.append(item)
            result = flattened_result
            
    except Exception as e:
        error_msg = f"❌ JSON 파싱 에러: {e}"
        logging.error(error_msg)
        send_teams_log(error_msg)
        return []

    # 상세 로그 출력을 위한 탈락 기사 추적
    kept_titles = set([a.get("title") for a in result if isinstance(a, dict) and "title" in a])
    removed = [a["title"] for a in articles if a["title"] not in kept_titles]

    logging.info(f"\n🤖 [GPT FILTER]")
    logging.info(f"입력: {before} → 출력: {len(result)}")
    logging.info(f"제거됨 ({len(removed)}개):")
    for r in removed:
        logging.info(f" - {r}")

    return result

# ---------------------------
# 메인 API 엔드포인트
# ---------------------------
@app.get("/news")
def process_news():
    start_time = datetime.now(KST).strftime('%H:%M:%S')
    send_teams_log(f"🔄 뉴스 수집 프로세스 시작 ({start_time})")

    try:
        # 1. 수집 (RSS + 네이버 API 병합)
        rss_articles = fetch_rss()
        naver_articles = fetch_naver_news()
        articles = rss_articles + naver_articles
        
        # 2. 1차 필터링
        articles = filter_date(articles)
        articles = filter_banned(articles)
        articles = filter_keywords(articles)
        
        mid_count = len(articles)
        logging.info(f"1차 필터 통과: {mid_count}건")

        # 3. AI 필터링
        final_articles = ai_filter(articles)
        final_count = len(final_articles)

        # 4. 완료 보고
        status_msg = f"✅ 뉴스 필터링 완료!\n- 수집: {len(articles)}건\n- AI 최종 선정: {final_count}건"
        logging.info(status_msg)
        send_teams_log(status_msg)

        return final_articles

    except Exception as e:
        error_msg = f"🔥 서버 내부 치명적 에러: {e}"
        logging.error(error_msg)
        send_teams_log(error_msg)
        return []