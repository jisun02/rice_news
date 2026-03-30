import feedparser
import os
import re
from datetime import datetime, timedelta
from fastapi import FastAPI
from openai import OpenAI
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
import json
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import logging
from datetime import datetime

# 로그 파일 이름 (날짜별로 생성)
os.makedirs("logs", exist_ok=True)
log_filename = f"logs/news_log_{datetime.now().strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_filename, encoding="utf-8"),
        logging.StreamHandler()  # 콘솔에도 같이 출력
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
    "https://news.google.com/rss/search?q=쌀+when:1d&hl=ko&gl=KR&ceid=KR:ko"
    # "https://news.google.com/rss/search?q=양곡+when:7d&hl=ko&gl=KR&ceid=KR:ko",
    # "https://news.google.com/rss/search?q=TRQ+when:7d&hl=ko&gl=KR&ceid=KR:ko"
]

# 여기에 없는 RSS는 자동으로 default(7일) 적용
SOURCE_DAY_RULE = {
    "http://www.newsfarm.co.kr/rss/allArticle.xml": 1,
    "http://www.farminsight.net/rss/allArticle.xml": 1
}

KEYWORDS = ["쌀", "벼", "곡물", "농업", "미곡", "미", "양곡", "정부", "비축", "TRQ", "수급", "식량", "물가", "농산물"]
BANNED_WORDS = ["vietnam", "기부"] # 대소문자 구분 없이 필터링하기 위해 소문자로 작성

STORAGE_FILE = "sent_news.json"

# ---------------------------
# 유틸리티
# ---------------------------
def clean_title(title):
    """임베딩 정확도를 높이기 위해 언론사명, 특수문자 등을 제거합니다."""
    title = re.sub(r"\[.*?\]|\(.*?\)", "", title)
    title = re.sub(r"[^\w\s]", " ", title)
    return " ".join(title.split())

# ---------------------------
# 기존 기사 로드/저장
# ---------------------------
def load_existing():
    if os.path.exists(STORAGE_FILE):
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_existing(titles):
    with open(STORAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(titles, f, ensure_ascii=False)

# ---------------------------
# RSS 수집
# ---------------------------
def fetch_rss():
    articles = []

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    for url in RSS_LIST:
        res = requests.get(url, headers=headers, verify=False, timeout=10)

        feed = feedparser.parse(res.content)

        # 오류 수정: f-string 사용
        logging.info(f"entries: {len(feed.entries)}")

        for entry in feed.entries:
            articles.append({
                "title": entry.title,
                "summary": entry.get("summary", ""),
                "url": entry.link,
                "published": entry.get("published_parsed", None),
                "source": url
            })

    return articles

# ---------------------------
# 필터링
# ---------------------------
def filter_date(articles):
    before = len(articles)
    now = datetime.utcnow()

    result = []
    removed = []

    for a in articles:
        if a["published"]:
            pub = datetime(*a["published"][:6])

            # ✅ source별 필터 기간 설정 (딕셔너리에 없으면 7일)
            days = SOURCE_DAY_RULE.get(a["source"], 7)
            limit = timedelta(days=days)

            if now - pub <= limit:
                result.append(a)
            else:
                removed.append(a["title"])
        else:
            removed.append(a["title"])

    logging.info(f"\n📅 [DATE FILTER]")
    logging.info(f"입력: {before} → 출력: {len(result)}")
    logging.info(f"제거됨 ({len(removed)}개):")

    return result

def filter_banned(articles):
    """Vietnam, 베트남 등 명시적 금지어가 포함된 기사 1차 제외"""
    before = len(articles)
    result = []
    removed = []

    for a in articles:
        text_to_check = (a["title"] + " " + a["url"]).lower()
        if any(b in text_to_check for b in BANNED_WORDS):
            removed.append(a["title"])
        else:
            result.append(a)

    logging.info(f"\n🚫 [BANNED WORDS FILTER]")
    logging.info(f"입력: {before} → 출력: {len(result)}")
    logging.info(f"제거됨 ({len(removed)}개):")
    for r in removed[:5]: # 너무 많을 수 있으니 5개만 출력
        # 오류 수정: f-string 사용
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
        # 오류 수정: f-string 사용
        logging.info(f" - {r}")

    return result

def remove_existing(articles, existing_titles):
    before = len(articles)

    result = []
    removed = []

    for a in articles:
        if a["title"] not in existing_titles:
            result.append(a)
        else:
            removed.append(a["title"])

    logging.info(f"\n🧾 [EXISTING FILTER]")
    logging.info(f"입력: {before} → 출력: {len(result)}")
    logging.info(f"중복 제거 ({len(removed)}개):")
    for r in removed:
        # 오류 수정: f-string 사용
        logging.info(f" - {r}")

    return result


# ---------------------------
# Embedding 중복 제거
# ---------------------------
def remove_duplicates_embedding(articles):
    before = len(articles)

    if not articles:
        return []

    titles = [a["title"] for a in articles]

    # [수정된 부분] API 호출을 방어막(try-except)으로 감쌉니다.
    try:
        embeddings = client.embeddings.create(
            model="text-embedding-3-small",
            input=titles
        ).data
    except Exception as e:
        logging.error(f"❌ OpenAI Embedding API 호출 에러 (원본 그대로 반환): {e}")
        return articles # 에러가 나면 중복 제거를 건너뛰고 그대로 반환합니다.

    vectors = [e.embedding for e in embeddings]

    unique_articles = []
    used = set()
    removed = []

    for i in range(len(vectors)):
        if i in used:
            continue

        unique_articles.append(articles[i])

        for j in range(i+1, len(vectors)):
            sim = cosine_similarity([vectors[i]], [vectors[j]])[0][0]
            if sim > 0.77:
                used.add(j)
                removed.append(articles[j]["title"])

    logging.info(f"\n🧠 [EMBEDDING DEDUP]")
    logging.info(f"입력: {before} → 출력: {len(unique_articles)}")
    logging.info(f"중복 제거 ({len(removed)}개):")
    for r in removed:
        logging.info(f" - {r}")

    return unique_articles


# ---------------------------
# GPT 필터
# ---------------------------
def ai_filter(articles):
    if not articles:
        return []

    before = len(articles)

    compact_articles = [
        {
            "title": a["title"],
            "url": a["url"]
        }
        for a in articles
    ]

# [수정된 부분] OpenAI API 호출 자체를 try-except로 감쌉니다.
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": """You are an expert Rice news editor.
Your task is to FILTER irrelevant news AND GROUP similar news articles, picking ONLY ONE representative article per topic.

[Selection Rules - INCLUDE]
1. TRQ (Tariff-Rate Quota): Include ALL news mentioning TRQ, even if it's about other crops like soybeans (콩) or wheat.
2. Agricultural material prices, agricultural budget/subsidies, and general grain/crop market trends.
3. Processed rice products, new rice varieties, export/market expansion, and rice consumption trends.
4. Production, stockpile, price stabilization, and government rice policy.
5. Rice varieties, cultivation, climate impact on rice
6. Rice industry, distribution, exports, market expansion

[Selection Rules - EXCLUDE]
1. General agriculture policy not specific to rice
2. Farmer welfare, education, events
3. Government meetings or plans without rice relevance
4. Non-food use of rice (cosmetics, beauty, etc.)

[Grouping Rules]
If multiple articles cover the exact same event, select the ONE with the most informative title and completely DISCARD the rest.

Output a JSON ARRAY ONLY. Example format: [{"title": "...", "url": "..."}, ...]"""
                },
                {
                    "role": "user",
                    "content": json.dumps(compact_articles, ensure_ascii=False)
                }
            ]
        )
        content = response.choices[0].message.content

    except Exception as e:
        # API 통신 에러, 키 오류, 타임아웃 등 발생 시 뻗지 않고 여기서 중단
        logging.error(f"❌ OpenAI API 호출 에러 (재시도 안 함): {e}")
        return []

    if content.startswith("```"):
        content = content.split("```")[1]
        content = content.replace("json", "").strip()

    try:
        result = json.loads(content)
        
        if isinstance(result, dict):
            for k, v in result.items():
                if isinstance(v, list):
                    result = v
                    break
            else:
                result = [result] 
                
        if not isinstance(result, list):
            result = []

    except Exception as e:
        logging.info(f"❌ JSON 파싱 실패 ({e}): {content}")
        return []

    after = len(result)

    kept_titles = set([a.get("title") for a in result if isinstance(a, dict) and "title" in a])
    removed = [a["title"] for a in articles if a["title"] not in kept_titles]

    logging.info(f"\n🤖 [GPT FILTER]")
    logging.info(f"입력: {before} → 출력: {after}")
    logging.info(f"제거됨 ({len(removed)}개):")
    for r in removed:
        logging.info(f" - {r}")

    return result

# ---------------------------
# API
# ---------------------------
@app.get("/news")
def process_news():

    articles = fetch_rss()
    articles = filter_date(articles)
    articles = filter_banned(articles)
    articles = filter_keywords(articles)

    # existing_titles = load_existing()
    # articles = remove_existing(articles, existing_titles)

    # articles = remove_duplicates_embedding(articles)
    final_articles = ai_filter(articles)

    # 저장
    # new_titles = existing_titles + [a["title"] for a in final_articles]
    # save_existing(new_titles)

    return final_articles