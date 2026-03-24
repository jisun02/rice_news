import feedparser
import os
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

STORAGE_FILE = "sent_news.json"

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
            if sim > 0.8:
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
                "content": """You are a precise rice news filter.

Return ONLY JSON array.

INCLUDE if article is related to:
- Rice production, consumption, supply/demand
- Rice price, stockpile, government policy
- Rice import/export, trade (TRQ 포함)
- Rice varieties, cultivation, climate impact on rice
- Rice industry, distribution, exports, market expansion

EXCLUDE if:
- General agriculture policy not specific to rice
- Farmer welfare, education, events
- Government meetings or plans without rice relevance
- Non-food use of rice (cosmetics, beauty, etc.)

IMPORTANT:
- If "rice" is clearly mentioned and topic is meaningful → KEEP
- Be inclusive rather than overly strict

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
    articles = filter_keywords(articles)

    # existing_titles = load_existing()
    # articles = remove_existing(articles, existing_titles)

    articles = remove_duplicates_embedding(articles)
    final_articles = ai_filter(articles)

    # 저장
    # new_titles = existing_titles + [a["title"] for a in final_articles]
    # save_existing(new_titles)

    return final_articles