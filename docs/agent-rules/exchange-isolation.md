# 거래소 격리·비밀정보 규칙

## 책임 범위

이 문서는 빗썸·업비트 API 클라이언트, AI Provider, 환경 변수, 포트, 데이터·로그 경로, 거래 메모리와 민감정보 처리의 단일 규칙 원본이다.

## 거래소 경계

- 빗썸과 업비트의 환경 변수, API 클라이언트, 포트, 주문 상태, 데이터베이스·JSON 경로, 로그, 거래 메모리를 섞지 않는다.
- 거래소별 상태는 자신의 경로와 스코프에서만 읽고 쓴다. `BithumbAPI`와 `UpbitAPI`의 `exchange_name` 식별자나 오케스트레이터의 거래소 스코프 판별을 우회하지 않는다.
- `market_intelligence.json`을 포함한 영속 상태는 거래소별 경로를 유지한다.

## AI Provider와 키

- 빗썸 신규 BUY는 `BITHUMB_AI_PROVIDER=gemini`, `BITHUMB_GEMINI_API_KEY`와 Flash-Lite 순차 폴백(`gemini-3.5-flash-lite` 후 `gemini-3.1-flash-lite`)만 사용한다.
- 업비트 신규 BUY는 `UPBIT_GEMINI_API_KEY`와 같은 Flash-Lite 순차 폴백만 사용한다. 공용 `GEMINI_API_KEY`와 거래소 간 키 fallback은 사용하지 않는다.
- 거시 레짐 진단과 브리핑은 일반 Flash(`gemini-3.8-flash`)를 우선 시도하고 Flash-Lite 폴백을 허용한다.
- Groq 거시 시장 인텔리전스는 `BITHUMB_GROQ_API_KEY` 또는 `UPBIT_GROQ_API_KEY`만 사용하며 공용 `GROQ_API_KEY`는 fallback으로도 사용하지 않는다.
- 키 누락이나 Gemini 호출 실패는 신규 BUY를 fail-closed로 차단한다.

## 비밀정보와 외부 입력

- API 키, 시크릿, 토큰, 계좌 식별자, 주문 식별자는 코드, 문서, 테스트 픽스처, 로그, 응답에 노출하지 않는다.
- 예시에는 비밀값이 없는 더미 값만 사용한다.
- 외부 문자열을 대시보드에 표시할 때는 안전한 텍스트 렌더링과 마스킹을 적용한다.
