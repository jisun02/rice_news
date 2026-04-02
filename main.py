import feedparser
import os
import re
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
    "http://www.farminsight.net/rss/allArticle.xml",
    "https://news.google.com/rss/search?q=쌀+when:1d&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=양곡+when:7d&hl=ko&gl=KR&ceid=KR:ko",
    "https://news.google.com/rss/search?q=TRQ+when:7d&hl=ko&gl=KR&ceid=KR:ko"
]

SOURCE_DAY_RULE = {
    "http://www.newsfarm.co.kr/rss/allArticle.xml": 2,
    "http://www.farminsight.net/rss/allArticle.xml": 2
}

KEYWORDS = ["쌀", "벼", "곡물", "농업", "미곡", "미", "양곡", "정부", "비축", "TRQ", "수급", "식량", "물가", "농산물"]
BANNED_WORDS = ["vietnam", "기부", "나눔"]

# ---------------------------
# Teams 알림 유틸리티
# ---------------------------
def send_teams_log(message):
    """Teams 웹훅으로 로그 메시지를 전송합니다."""
    webhook_url = os.getenv("TEAMS_WEBHOOK_URL")
    
    # 1. 환경변수가 아예 설정되지 않은 경우 확실하게 로그에 띄움
    if not webhook_url:
        logging.warning("⚠️ TEAMS_WEBHOOK_URL 환경변수가 없어 Teams 알림을 생략합니다. (Render 설정을 확인하세요)")
        return 

    try:
        res = requests.post(webhook_url, json={"message": message}, timeout=10)
        # 2. 전송은 했는데 Power Automate가 거절한 경우 에러 띄움
        res.raise_for_status() 
    except Exception as e:
        logging.error(f"❌ Teams 알림 전송 실패: {e}")

# ---------------------------
# RSS 수집 및 필터 함수들
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
                    "summary": entry.get("summary", ""),
                    "url": entry.link,
                    "published": entry.get("published_parsed", None),
                    "source": url
                })
        except Exception as e:
            logging.error(f"RSS 수집 에러 ({url}): {e}")
    return articles

def filter_date(articles):
    now = datetime.utcnow()
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
        # 뒤에서부터 찾아서 지우기 때문에 제목 본문의 하이픈은 비교적 안전합니다.
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
        # 1. 수집
        articles = fetch_rss()
        
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