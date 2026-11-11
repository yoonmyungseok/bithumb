import os
import requests

upbit_gemini_key = ""
for line in open(".env.upbit"):
    if "UPBIT_GEMINI_API_KEY=" in line:
        upbit_gemini_key = line.strip().split("=", 1)[1].strip()
        break

prompt = """당신은 수석 퀀트 트레이딩 분석가입니다.
아래는 24시간 동안의 양 거래소(업비트, 빗썸) 매매 데이터 요약입니다.
- 거래 건수: 0건 (시장 저변동성 박스권 수렴)
- 비트코인 변동률: +0.15% (극단적 유동성 가뭄)
- 사용자 수동 보유 종목: RSR(+4.06%), US(-0.23%), HOLO(격리보호)

이 데이터를 바탕으로 다음 목차를 준수하여 한국어로 전문적이고 심층적인 분석 보고서의 [5. 데이터 기반 피드백 및 개선안] 섹션을 작성하세요:
### 1. 잘한 점 (데이터 기반 팩트)
### 2. 보완할 점 (데이터 기반 팩트)
### 3. 구체적인 파라미터 및 필터 개선안
"""

url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash:generateContent?key={upbit_gemini_key}"
r = requests.post(url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20.0)
print("Status:", r.status_code)
if r.status_code == 200:
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    print("Generated length:", len(text))
    print(text[:300])
else:
    print(r.text)
