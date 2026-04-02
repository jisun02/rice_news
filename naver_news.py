import os
import requests
import re
import html
from email.utils import parsedate

# ---------------------------------------------------------
# 💡 테스트 전 주의사항!
# 실행하기 전에 터미널에서 환경변수를 설정하거나, 
# 아래 코드의 빈칸에 직접 ID와 Secret을 임시로 입력하세요.
# (테스트가 끝나면 꼭 지워주세요!)
# ---------------------------------------------------------

CLIENT_ID = "넣기"
CLIENT_SECRET = "넣기"

NAVER_KEYWORDS = [
    "쌀 수매", 
    "쌀값", 
    "쌀 작황",
    "양곡관리", 
    "미곡처리장", 
    "농산물 TRQ"
]

def test_fetch_naver_news():
    print("🚀 네이버 뉴스 검색 API 테스트를 시작합니다...\n")

    articles = []
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {
        "X-Naver-Client-Id": CLIENT_ID,
        "X-Naver-Client-Secret": CLIENT_SECRET
    }

    for keyword in NAVER_KEYWORDS:
        params = {
            "query": keyword,
            "display": 2,  # 테스트용이므로 키워드당 2개만 가져옵니다.
            "sort": "date"
        }
        
        try:
            print(f"[{keyword}] 키워드 검색 중...")
            # 🔥 verify=False 를 추가해서 사내망 SSL 에러를 무시하도록 설정했습니다.
            res = requests.get(url, headers=headers, params=params, verify=False, timeout=10)
            
            # 인증 에러(401) 등이 났을 때 상세 이유를 보기 위해 추가
            if res.status_code != 200:
                print(f" ❌ API 요청 실패 (상태 코드: {res.status_code}) - {res.text}")
                continue
                
            data = res.json()
            
            items = data.get("items", [])
            print(f" 👉 {len(items)}건 수집 완료")
            
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
            print(f" ❌ 에러 발생 ({keyword}): {e}")

    print("\n" + "="*50)
    print(f"🎉 총 {len(articles)}개의 기사를 성공적으로 가져왔습니다!")
    print("="*50 + "\n")

    # 결과 데이터 3개만 샘플로 이쁘게 출력해보기
    for i, article in enumerate(articles):
        print(f"🔹 기사 {i+1}")
        print(f" - 출처: {article['source']}")
        print(f" - 제목: {article['title']}")
        print(f" - 링크: {article['url']}")
        print(f" - 날짜: {article['published']}")
        print("-" * 50)

if __name__ == "__main__":
    test_fetch_naver_news()