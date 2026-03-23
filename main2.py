import feedparser
import os
from datetime import datetime, timedelta
from fastapi import FastAPI
from openai import OpenAI
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
import json

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ---------------------------
# 설정
# ---------------------------
RSS_LIST = [
    "http://www.newsfarm.co.kr/rss/allArticle.xml",
    # "http://www.farminsight.net/rss/allArticle.xml",
    # "https://news.google.com/rss/search?q=쌀+when:7d&hl=ko&gl=KR&ceid=KR:ko",
    # "https://news.google.com/rss/search?q=양곡+when:7d&hl=ko&gl=KR&ceid=KR:ko",
    # "https://news.google.com/rss/search?q=TRQ+when:7d&hl=ko&gl=KR&ceid=KR:ko"
]

KEYWORDS = ["쌀", "벼", "미", "곡물", "농업", "미곡", "양곡", "정부", "비축", "TRQ", "수급", "식량", "물가"]

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
    for url in RSS_LIST:
        feed = feedparser.parse(url)
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
    now = datetime.utcnow()
    return [
        a for a in articles
        if a["published"] and (now - datetime(*a["published"][:6]) <= timedelta(days=7))
    ]

def filter_keywords(articles):
    return [
        a for a in articles
        if any(k in a["title"] for k in KEYWORDS)
    ]

def remove_existing(articles, existing_titles):
    return [a for a in articles if a["title"] not in existing_titles]

# ---------------------------
# Embedding 중복 제거
# ---------------------------
def remove_duplicates_embedding(articles):
    titles = [a["title"] for a in articles]

    embeddings = client.embeddings.create(
        model="text-embedding-3-small",
        input=titles
    ).data

    vectors = [e.embedding for e in embeddings]

    unique_articles = []
    used = set()

    for i in range(len(vectors)):
        if i in used:
            continue
        unique_articles.append(articles[i])
        for j in range(i+1, len(vectors)):
            sim = cosine_similarity([vectors[i]], [vectors[j]])[0][0]
            if sim > 0.85:
                used.add(j)

    return unique_articles

# ---------------------------
# GPT 필터
# ---------------------------
def ai_filter(articles):
    if not articles:
        return []
    
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
                "content": """Filter news.

Keep only rice/agriculture policy related.
Remove duplicates.

Return ONLY raw JSON.
Do NOT use markdown.
Do NOT wrap in ```."""
            },
            {
                "role": "user",
                "content": json.dumps(compact_articles, ensure_ascii=False)
            }
        ]
    )
    content = response.choices[0].message.content

    # 🔥 핵심: 코드블록 제거
    if content.startswith("```"):
        content = content.split("```")[1]  # json 부분
        content = content.replace("json", "").strip()

    try:
        return json.loads(content)
    except:
        print("❌ JSON 파싱 실패:", content)
        return []

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