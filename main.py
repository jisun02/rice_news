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

# import urllib3
# urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------
# 설정
# ---------------------------
RSS_LIST = [
    "http://www.newsfarm.co.kr/rss/allArticle.xml",
    "http://www.farminsight.net/rss/allArticle.xml",
    "https://news.google.com/rss/search?q=쌀+when:1d&hl=ko&gl=KR&ceid=KR:ko",
    # "https://news.google.com/rss/search?q=양곡+when:7d&hl=ko&gl=KR&ceid=KR:ko"
    "https://news.google.com/rss/search?q=TRQ+when:7d&hl=ko&gl=KR&ceid=KR:ko"
]

KEYWORDS = ["쌀", "벼", "곡물", "농업", "미곡", "미", "양곡", "정부", "비축", "TRQ", "수급", "식량", "물가"]
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

        print("entries:", len(feed.entries))

        for entry in feed.entries:
            articles.append({
                "title": entry.title,
                "summary": entry.get("summary", ""),
                "url": entry.link,
                "published": entry.get("published_parsed", None)
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
            if now - pub <= timedelta(days=7):
                result.append(a)
            else:
                removed.append(a["title"])
        else:
            removed.append(a["title"])

    print(f"\n📅 [DATE FILTER]")
    print(f"입력: {before} → 출력: {len(result)}")
    print(f"제거됨 ({len(removed)}개):")

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

    print(f"\n🚫 [BANNED WORDS FILTER]")
    print(f"입력: {before} → 출력: {len(result)}")
    print(f"제거됨 ({len(removed)}개):")
    for r in removed[:5]: # 너무 많을 수 있으니 5개만 출력
        print(" -", r)
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

    print(f"\n🔍 [KEYWORD FILTER]")
    print(f"입력: {before} → 출력: {len(result)}")
    print(f"제거됨 ({len(removed)}개):")
    for r in removed:
        print(" -", r)

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

    print(f"\n🧾 [EXISTING FILTER]")
    print(f"입력: {before} → 출력: {len(result)}")
    print(f"중복 제거 ({len(removed)}개):")
    for r in removed:
        print(" -", r)

    return result


# ---------------------------
# Embedding 중복 제거
# ---------------------------
def remove_duplicates_embedding(articles):
    before = len(articles)

    if not articles:
        return []

    titles = [a["title"] for a in articles]

    embeddings = client.embeddings.create(
        model="text-embedding-3-small",
        input=titles
    ).data

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

    print(f"\n🧠 [EMBEDDING DEDUP]")
    print(f"입력: {before} → 출력: {len(unique_articles)}")
    print(f"중복 제거 ({len(removed)}개):")
    for r in removed:
        print(" -", r)

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

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": """You are an expert Rcie news editor.
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

Output JSON only."""
            },
            {
                "role": "user",
                "content": json.dumps(compact_articles, ensure_ascii=False)
            }
        ]
    )

    content = response.choices[0].message.content

    if content.startswith("```"):
        content = content.split("```")[1]
        content = content.replace("json", "").strip()

    try:
        result = json.loads(content)
    except:
        print("❌ JSON 파싱 실패:", content)
        return []

    after = len(result)

    # GPT에서 살아남은 제목
    kept_titles = set([a["title"] for a in result])
    removed = [a["title"] for a in articles if a["title"] not in kept_titles]

    print(f"\n🤖 [GPT FILTER]")
    print(f"입력: {before} → 출력: {after}")
    print(f"제거됨 ({len(removed)}개):")
    for r in removed:
        print(" -", r)

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

    articles = remove_duplicates_embedding(articles)
    final_articles = ai_filter(articles)

    # 저장
    # new_titles = existing_titles + [a["title"] for a in final_articles]
    # save_existing(new_titles)

    return final_articles