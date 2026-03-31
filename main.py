import feedparser
import os
import re
from datetime import datetime, timedelta
from fastapi import FastAPI
from openai import OpenAI
import json
import requests
import urllib3
import logging

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 로그 설정
os.makedirs("logs", exist_ok=True)
log_filename = f"logs/news_log_{datetime.now().strftime('%Y-%m-%d')}.log"

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
    "http://www.newsfarm.co.kr/rss/allArticle.xml": 1,
    "http://www.farminsight.net/rss/allArticle.xml": 1
}

KEYWORDS = ["쌀", "벼", "곡물", "농업", "미곡", "미", "양곡", "정부", "비축", "TRQ", "수급", "식량", "물가", "농산물"]
BANNED_WORDS = ["vietnam", "기부"]

# ---------------------------
# Teams 알림 유틸리티 (새로 추가)
# ---------------------------
def send_teams_log(message):
    """Teams 웹훅으로 로그 메시지를 전송합니다."""
    webhook_url = os.getenv("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        return 

    try:
        # Power Automate의 'HTTP 요청을 수신할 때' 트리거로 전송
        requests.post(webhook_url, json={"message": message}, timeout=5)
    except Exception as e:
        logging.error(f"Teams 알림 전송 실패: {e}")

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
    result = [a for a in articles if not any(b in (a["title"] + " " + a["url"]).lower() for b in BANNED_WORDS)]
    return result

def filter_keywords(articles):
    result = [a for a in articles if any(k in a["title"] for k in KEYWORDS)]
    return result

# ---------------------------
# GPT 필터 (알림 강화)
# ---------------------------
def ai_filter(articles):
    if not articles:
        return []

    compact_articles = [{"title": a["title"], "url": a["url"]} for a in articles]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": "You are an expert Rice news editor. Filter irrelevant news and group similar ones. [Include: TRQ, Production costs(생산비), Price(쌀값), Policy, Distribution] Output a JSON ARRAY ONLY."},
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
        result = json.loads(content)
        if isinstance(result, dict):
            result = next((v for v in result.values() if isinstance(v, list)), [])
    except Exception as e:
        error_msg = f"❌ JSON 파싱 에러: {e}"
        logging.error(error_msg)
        send_teams_log(error_msg)
        return []

    return result

# ---------------------------
# 메인 API 엔드포인트
# ---------------------------
@app.get("/news")
def process_news():
    start_time = datetime.now().strftime('%H:%M:%S')
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