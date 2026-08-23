import os
import sys

import requests
from dotenv import load_dotenv

# 윈도우 인코딩 설정 (UTF-8 표준화)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# .env 로드
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY", "").strip()

print("=" * 60)
print(" 🔍 [Google Gemini API 사용 가능 모델 목록 조회] ")
print("=" * 60)

if not api_key:
    print("❌ .env 파일에 GEMINI_API_KEY가 설정되어 있지 않습니다.")
    sys.exit(1)

endpoint = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"

try:
    res = requests.get(endpoint, timeout=10)
    if res.status_code != 200:
        print(f"❌ API 호출 실패 [{res.status_code}]: {res.text}")
        sys.exit(1)

    models_data = res.json().get("models", [])

    # 텍스트/퀀트 분석 생성(generateContent) 가능한 모델 필터링
    gen_models: list[tuple[str, str]] = []
    for m in models_data:
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            name = m.get("name", "").replace("models/", "")
            display_name = m.get("displayName", name)
            gen_models.append((name, display_name))

    print(f"\n✅ 총 {len(gen_models)}개의 사용 가능한 Gemini 모델을 찾았습니다:\n")
    print(f"{'No.':<4} | {'모델 식별자 (API Model Name)':<35} | {'표시 이름'}")
    print("-" * 65)

    for idx, (name, dname) in enumerate(gen_models, 1):
        star = " 🌟 [추천]" if "flash" in name.lower() else ""
        print(f"{idx:<4} | {name:<35} | {dname}{star}")

    print("-" * 65)
    print("\n💡 퀀트 자동매매에는 속도가 빠르고 1일 1,500회 무료인 [flash-lite / flash] 계열을 추천합니다.")

except (requests.exceptions.RequestException, KeyError, ValueError) as e:
    print(f"❌ 오류 발생: {e}")
