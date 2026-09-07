# Bithumb & Upbit AI Pro Quant Trading Bot (v8.34)

본 문서는 `c:\AI\bithumb` 디렉토리에 위치한 빗썸(Bithumb) 및 업비트(Upbit) 듀얼 거래소 지원 AI 퀀트 트레이딩 봇의 프로젝트 설명 및 아키텍처 설계서입니다. 이 문서는 다른 AI 에이전트 또는 개발자가 프로젝트의 전반적인 구조와 핵심 로직을 빠르고 명확하게 파악할 수 있도록 작성되었습니다.


> [!IMPORTANT]
> **개발 가이드라인**: 코드가 수정되거나 새로운 기능/모듈이 추가될 때마다 본 `PROJECT_DESIGN.md` 문서를 **반드시 함께 최신 상태로 갱신**해야 합니다.

---

## 1. 프로젝트 개요

이 프로젝트는 빗썸(Bithumb)의 Groq AI와 업비트(Upbit)의 Google Gemini AI를 실시간 데이터와 결합하여, 유망한 단타/스윙 종목을 자동으로 탐색하고 매매를 수행하는 **듀얼 거래소 독립형 AI 퀀트 트레이딩 시스템**입니다.

- **언어 및 환경**: Python 3, Windows 환경 (`.bat` 및 `process_manager.py` 기반 구동)
- **핵심 기술**: 
  - 빗썸 REST API & WebSocket (v1/v2)
  - 업비트 REST API & WebSocket (Public: 시세/체결, Private: myOrder/myAsset, HS512 JWT + unencoded query string SHA-512 hash, `identifier` 멱등성)
  - 빗썸 Groq API / 업비트 Google Gemini API (Flash 모델군), Telegram API
- **주요 전략 및 아키텍처**: 
  - **다중 시간대(MTF) 분석**: 1시간봉 대세 추세 + 5분봉 정밀 타점 정렬
- **거래대금 및 모멘텀 기반 동적 시장 스크리닝**: 모멘텀 후보를 `EARLY`(당일 상승률 +3% 이하의 RS 확인 초입)와 `EXTENDED`(확장 후반) 단계로 기록한다. 신규 주문은 `EARLY`에서만 소액으로 허용하고, `EXTENDED`는 분석·감사만 수행해 후발 추격을 방지한다. 동일 5분 사이클에서는 모멘텀 신규 주문을 1건으로 제한한다.
  - **Dual-Track(단타 + 스윙) 병행 전략 (v8.34)**:
    - **단타 트랙(SCALP)**: 5분봉 기반 스캘핑, 1차 +3.5% 익절, 손절 -2.2%, 120분/180분 타임스탑을 통한 높은 회전율 및 리스크 방어 유지
    - **스윙 트랙(SWING)**: 대형 메이저(BTC/ETH/SOL/XRP) 및 24시간 거래대금 300억 원 이상 최상위 우량 코인 대상, 4H/1H 추세 지지 기반 장기 추세 추종
    - **스윙 전용 파라미터**: 손절 -5.5%, 절대 하드스탑 -8.0%, 1차 익절 +8.0%(40%), 2차 익절 +15.0%(30%), 트레일링 시작 +8.0%/드롭 4.0%, 본전보장 +1.5%
    - **스윙 타임스탑 면제**: 120분/180분 시간 청산을 면제하고, 4H EMA20 이탈 시에만 추세 청산(`evaluate_swing_trend_exit`) 집행
    - **슬롯 격리(`RiskGuard`)**: 단타 슬롯과 스윙 슬롯을 독립적으로 관리하여 한 트랙의 자금 잠김이 다른 트랙의 기회를 방해하지 않음
  - **2중 호가 안전망 (Fail-Closed)**: 매수/매도 스프레드 $\le 0.35\%$, 상위 5호가 누적 매수 잔량 $\ge 2,000$만 원 검증
  - **결정론적 기술지표 및 6중 리스크 안전 가드**: 볼린저 밴드(일반장 %B 0.20~0.72, `RISK_OFF`는 하드게이트 전체 통과 시 0.75까지), RSI 상한 72.0, 6중 AI 긴급 탈출 가드(15분 초기 보호, -1.20% 미세손실 완화, 1H MTF 우상향 보호), 7대 팩터 복합 알파 게이트 (LLM 환각 및 섣부른 조기 손절 차단)
  - **0.1초 초저지연 실시간 리스크 엔진**: WebSocket 틱 기반 0.1초 즉각 손절, 1차 50% 분할익절, 잔여 2차(25%) 및 가속 트레일링 스탑
  - **손익비 최적화 및 조기 본전 보장(Break-Even)**: 1차 분할익절 도달 즉시 잔여 수량 손절선을 평단가+0.3%(수수료 보장)로 락인
  - **대세 상승장(BULL_TREND) 파라미터 정상화**: 과다 손절 방어(손절 -2.0%), 선제적 조기 익절(+3.0%), 알파 65점 엄선, 타임스탑 120분 적용
  - **자산 연동형 3단계 스마트 Auto-Scaling**: 계좌 총 자산 규모에 따른 보유 슬롯(2~4개) 및 비중(25~50%) 자동 전환
  - **장중 자금 입출금 자동 보정 (Cashflow Adjustment)**: 입출금 시 시작 기준자산을 자동 보정하여 순수 매매 수익률 보존
  - **완전한 거래소 물리적/논리적 격리**: 빗썸과 업비트의 환경변수, 데이터 디렉터리(`data/upbit/*`), 로그(`logs/trading_upbit.log`), 대시보드 포트(`7979` vs `7980`), 구글 시트, 실행 스크립트 분리
  - **7중 KRW-HOLO 수동 종목 절대 보호망**: 업비트 `KRW-HOLO`는 스크리닝, 주문, 긴급매도, 자산평가, 실시간 청산, 시트, 대시보드에서 100% 영구 제외

### 빗썸 Groq / 업비트 Gemini AI Provider 분리 정책 (v8.27)

- 빗썸 AI 분석은 `BITHUMB_AI_PROVIDER=groq`, `BITHUMB_GROQ_API_KEY`, `BITHUMB_GROQ_FAST_MODEL`, `BITHUMB_GROQ_DEEP_MODEL`을 통해서만 활성화한다. 빗썸은 공용·업비트 Gemini 키를 읽거나 사용하지 않는다.
- 고정 모델 매핑은 신규 진입 분석·보유 포지션 평가·후보 랭킹·거시 레짐에 `FAST_TRADING=openai/gpt-oss-20b`, 빗썸 일일 시황 브리핑에 `DEEP_BRIEFING=openai/gpt-oss-120b`이다.
- `FAST_TRADING`의 429, 타임아웃, 5xx, 잘못된 JSON, JSON Schema 오류 또는 구성 누락 시 120B 승격·로컬 BUY 폴백 없이 빗썸 신규 BUY를 fail-closed로 차단한다. 기존 포지션의 주문·체결 대사·리스크 보호·청산은 계속 동작한다.
- `DEEP_BRIEFING` 실패에 한해서만 `FAST_TRADING` 20B로 요약 브리핑을 한 번 폴백할 수 있다. 브리핑 실패는 주문 정책을 바꾸지 않는다.
- 업비트는 기존 `UPBIT_GEMINI_API_KEY`와 Gemini Provider만 사용한다. 거래소 REST/Private WebSocket, 주문 실행, 주문 저널 및 확정 체결 대사 계약은 Provider 전환 범위 밖이다.
- `ai_provider.py`는 Groq strict JSON Schema와 로컬 공통 스키마 검증을 적용하며, Provider·모델·거래소별 호출량·성공/429/오류·평균 지연을 대시보드의 표시 전용 확장으로 분리 계측한다. 인증 키와 응답 원문은 계측·로그에 저장하지 않는다.
- 빗썸의 실행 로그·대시보드 제목·AI 분석 결과 표기는 `Groq`를 사용한다. 기존 `gemini_*` 대시보드 DOM/API 키와 `GeminiAnalyzer` 클래스명은 업비트 및 외부 응답 하위 호환을 위해 내부 계약으로만 유지하며, 빗썸 사용자 표시에는 노출하지 않는다.
- 빗썸 Groq 호출 계측은 `data/groq_telemetry.json`에 원자 저장·복원한다. 저장 시 프로세스 간 파일 잠금 아래 최신 디스크 통계와 현재 프로세스의 미반영 증분만 병합하므로, 재시작 또는 중복 프로세스가 호출량을 0으로 덮어쓰지 않는다. 매 응답의 `x-ratelimit-reset-requests` 헤더를 저장해 실제 RPD 리셋 예정 시각을 대시보드에 표시하며, KST 자정 추정으로 카운터를 초기화하지 않는다. 해당 헤더가 가리킨 시각이 지난 뒤에만 새 쿼터 창으로 전환한다. 계측 파일 오류는 주문·체결·기존 포지션 보호에 영향을 주지 않는다.
- `GroqProvider.SYSTEM_INSTRUCTION`은 20B의 거래·보유평가·후보랭킹·거시진단과 120B의 브리핑(20B 브리핑 폴백 포함) 모두에 system 메시지로 먼저 전달된다. 이 지침은 빗썸 전용 데이터 격리, 제공 수치만 사용, 분석 보조의 비주문 권한, ACK 비체결, 불확실 신규 BUY의 `HOLD`, 비밀정보 비출력, 요청별 JSON/한국어 형식 준수를 강제한다. 개별 분석 프롬프트는 이 공통 지침을 약화하거나 우회할 수 없다.

### 빗썸 Groq FAST 런타임 장애 신규 진입 차단 (v8.32)

- `FAST_TRADING`의 HTTP 4xx/429/5xx, 타임아웃, 네트워크 예외, 빈 응답, 잘못된 JSON 또는 스키마 오류는 `data/groq_telemetry.json`의 `entry_safety`에 원자 저장한다. 프로세스 재시작 뒤에도 마지막 FAST 실패 상태를 복원하며, 이전 계측 파일의 마지막 4xx/5xx도 보수적으로 신규 BUY 차단으로 승격한다.
- 이 상태는 표준 AI 진입뿐 아니라 `MOMENTUM_BREAKOUT` 직접 진입과 `RECOVERY_REBOUND`에도 공통으로 적용된다. 기존 보유 포지션의 REST 주문 대사, 체결 확인, 손절·트레일링·긴급 청산은 차단하지 않는다.
- 신규 BUY 차단은 정상적인 FAST JSON Schema 응답이 확인된 경우에만 해제한다. 120B 브리핑 성공·실패는 이 상태를 바꾸지 않으며, FAST 실패 시 120B 승격 또는 로컬 BUY 폴백은 허용하지 않는다.
- `build_bithumb_analyzer()`는 Groq **설정 오류**가 있을 때만 `None`을 반환한다. FAST 런타임 장애(`entry_safety`)는 `get_bithumb_ai_entry_block_reason()`과 주문 게이트에서만 신규 BUY를 차단하고, 분석기는 유지해 FAST 재시도로 자동 복구할 수 있게 한다.
- 운영 계측에는 마지막 오류의 목적, 모델, HTTP 상태 및 제한된 오류 코드/타입만 저장한다. API 키, 프롬프트, 오류 메시지 원문, 계좌·주문 식별자는 로그·대시보드·영속 파일에 저장하지 않는다. Groq 거시 진단이 실패하면 `CAUTION_PULLBACK` 방어 상태와 `AI_UNAVAILABLE` 표기를 사용해 정상 레짐으로 오인하지 않는다.

### 빗썸 Groq JSON Schema 안정화 및 진단 로깅 보강 (v8.35)

- Groq Structured Outputs의 strict 모드는 **object 루트**만 허용한다. 후보 랭킹 응답은 `{"rankings":[...]}` 래퍼 객체로 감싸고, Gemini 경로의 순수 배열 응답도 파싱 호환을 유지한다.
- Groq 인퍼런스 서버의 조기 HTTP 400(`json_validate_failed`) 드랍을 방지하기 위해, 신규 진입 `analyze_market()`을 포함한 모든 FAST JSON Schema 호출(`complete_json`)에 `strict=false` (best-effort 가이던스)를 일관되게 적용한다. 수신된 JSON은 로컬 `_parse_json_text()` 및 `_validate_schema()`를 통해 필수 필드와 타입을 엄격히 재검증하며, 검증 실패 시 fail-closed로 신규 BUY를 안전 차단한다.
- `_call_gemini_json()`의 빈 스키마 폴백은 Groq 400을 피하도록 `additionalProperties:false`와 `required:[]`를 포함한 object 스키마를 사용한다.
- Groq HTTP 4xx 오류 발생 시 `error.code` 외에 민감정보(API 키, 토큰, 계정정보 등)가 마스킹된 안전한 오류 요약(`_safe_error_summary`)을 로그에 함께 기록하여 신속한 원인 진단을 지원한다.

---

## 2. 디렉토리 구조 및 주요 파일

```text
c:\AI\bithumb\
├── .env / .env.bithumb   # 빗썸 환경변수 파일 (API 키, 텔레그램 토큰, 설정값 등)
├── .env.upbit.template   # 업비트 환경변수 템플릿 파일
├── .env.upbit            # 업비트 환경변수 파일
├── requirements.txt      # Python 의존성 패키지 목록
├── PROJECT_DESIGN.md     # 프로젝트 아키텍처 및 시스템 설계서 (상시 최신 동기화)
├── logs/                 # 일자별 트레이딩 및 시스템 로그 보관 (30일 보존)
│   ├── trading.log            # 빗썸 트레이딩 로그
│   └── trading_upbit.log      # 업비트 트레이딩 로그
├── data/                 # 로컬 영구/상태 데이터 저장 폴더
│   ├── trading.db             # 빗썸 전용 SQLite DB (주문·포지션·일일통계·거래메모리·전략 판단 이력)
│   ├── daily_stats.json       # 빗썸 일일 손익 통계 및 킬스위치 상태
│   ├── position_state.json    # 빗썸 포지션별 최고가·진입 시각·분할익절 상태
│   ├── trade_memory.json      # 빗썸 거래 내역 및 자가학습 메모리
│   ├── order_journal.json     # 빗썸 주문 의도·체결 대사 상태 저널
│   └── upbit/                 # 업비트 전용 격리 데이터 폴더
│       ├── trading.db             # 업비트 전용 SQLite DB (상위 data/trading.db와 분리, 전략 판단 이력 포함)
│       ├── daily_stats.json       # 업비트 일일 손익 통계 및 킬스위치 상태
│       ├── position_state.json    # 업비트 포지션별 최고가 및 1차 익절 상태
│       ├── trade_memory.json      # 업비트 거래 내역 및 자가학습 메모리
│       ├── order_journal.json     # 업비트 멱등성 보장 주문 상태 저널
│       ├── cooldown_state.json    # 업비트 재진입 쿨다운 상태
│       └── paper_account.json     # 업비트 모의투자 가상 원장
├── config/               # 설정 파일 폴더
├── src/                  # 핵심 소스코드 디렉토리
│   ├── main.py                     # 빗썸 진입점 (트레이딩 엔진 + 127.0.0.1:17979 내부 API)
│   ├── main_upbit.py               # 업비트 진입점 (트레이딩 엔진 + 127.0.0.1:17980 내부 API)
│   ├── dashboard_server.py         # [독립 프로세스] 통합 퀀트 대시보드 게이트웨이 서버 (포트 7979)
│   ├── upbit_api.py                # 업비트 REST API 클라이언트 (HS512 JWT, query_hash, identifier)
│   ├── upbit_websocket.py          # 업비트 Public WebSocket 클라이언트 (0.1초 실시간 틱/호가/고래 체결)
│   ├── upbit_private_websocket.py  # 업비트 Private WebSocket 클라이언트 (myOrder, myAsset)
│   ├── bithumb_api.py              # 빗썸 REST API 클라이언트
│   ├── websocket_manager.py        # 빗썸 Public WebSocket 클라이언트
│   ├── private_websocket_manager.py# 빗썸 Private WebSocket 클라이언트
│   ├── risk_manager.py             # 일일 손익/입출금보정/킬스위치(`DailyRiskManager`), 포지션추적(`TrailingStopTracker`), 자산평가
│   ├── realtime_engine.py          # 0.1초 실시간 웹소켓 체결 틱 손절/익절 청산 엔진 (`RealtimeRiskEngine`), 미체결 정정/취소
│   ├── bot_controller.py           # 텔레그램 양방향 제어, 웹 대시보드 API 공급자 (거래소별 독립 인스턴스)
│   ├── order_safety/               # 주문 저널·멱등성 집행·체결 처리 패키지 (`journal`, `executor`, `fill_processor`, `cooldown`, `orderbook`)
│   ├── gemini_telemetry.py           # Gemini API 호출·429·로컬 폴백·캐시 적중 관측
│   ├── operational_quality.py        # 호가 슬리피지 5거래일 관찰 준비도 리포트
│   ├── risk_controls.py            # 매수 리스크 가드·포지션 사이징·동적 티어 (`RiskGuard`, `calculate_risk_position_size`)
│   ├── state_store.py              # 원자적 JSON 영속 저장 (`write_json_atomically`, `load_json_with_backup_recovery`)
│   ├── strategy_engine.py          # 표준 기술지표(RSI, BB, ATR, MACD, EMA) 계산, 확인형·초기 돌파 결정론적 진입 게이트, StrategyPolicy SSOT
│   ├── gemini_analyzer.py          # Gemini AI 퀀트 분석 및 시그널 생성 엔진
│   ├── market_screener.py          # 확인형·초기 돌파 후보와 조건(거래대금, 상승률, 스프레드, 호가깊이) 시장 동적 탐색 (Fail-Closed, HOLO 제외)
│   ├── trading_runtime.py          # 5분 사이클 공통 오케스트레이션 (`TradingCycleEngine`, profile 기반)
│   ├── trading_bot_bootstrap.py    # 진입점 부트스트랩 공통화 (`TradingBotBootstrap`, 텔레그램·내부 API·WS·스케줄러·shutdown, 사이클 오프셋 분산)
│   ├── paper_broker.py             # 모의투자 어댑터 (거래소별 원장 격리 지원)
│   ├── trade_memory.py             # 트레이딩 기록, 통계 및 AI 자가학습 메모리 관리
│   ├── telegram_alert.py           # 텔레그램 양방향 원격 제어, 디바운싱 알림, 차트 전송
│   ├── chart_renderer.py           # matplotlib 기반 매매 시점 캔들 차트 이미지 렌더링 (다크 테마)
│   ├── web_server.py               # 로컬 경량 웹 API 서버 모듈 (is_api_only 지원)
│   ├── process_manager.py          # 빗썸/업비트/대시보드/전체 독립 프로세스 탐색, 종료, 상태, 로그 관리 CLI
│   ├── trading_watchdog.py         # 워치독 공통 엔진 (`TradingBotWatchdog`, 하트비트 감시·자동 재시작·crash-loop 방어)
│   ├── watchdog.py                 # 빗썸 워치독 진입점 (profile wiring)
│   └── watchdog_upbit.py           # 업비트 워치독 진입점 (profile wiring)
├── tests/                # 단위 테스트 디렉토리 (총 50개 테스트 스위트, 209개 테스트)
│   ├── test_dashboard_server.py    # 통합 대시보드 게이트웨이 및 멀티 거래소 집계/라우팅 검증
│   ├── test_upbit_api.py           # 업비트 API JWT 인증, SHA-512 query_hash, 호가단위, identifier 검증
│   ├── test_upbit_holo_guard.py    # KRW-HOLO 7중 방어선 (자산평가, 주문, 청산, 긴급매도, 시트 배제) 검증
│   ├── test_exchange_isolation.py  # 빗썸/업비트 데이터 및 프로세스 완전 분리 검증
│   ├── test_upbit_reconciliation_safety.py # 업비트 REST 대사 및 불완전 체결 안전망 검증
│   ├── test_storage_and_fill_boundaries.py # DB 경로 격리·확정 체결 뒤 쿨다운 검증
│   ├── test_p0_p1_readiness.py     # 확정봉, 호가 플로우, 데이터 무결성 검증
│   ├── test_strategy_ssot.py       # StrategyPolicy 단일 기준 일원화 검증
│   ├── test_market_screener.py
│   ├── test_order_safety.py
│   ├── test_paper_broker.py
│   ├── test_strategy_engine.py
│   ├── test_realtime_risk.py
│   └── test_startup_integration.py
└── *.bat                 # 프로세스 제어 배치 스크립트
    ├── start_all.bat               # 빗썸 + 업비트 + 통합 대시보드 3대 프로세스 일괄 가동
    ├── stop_all.bat                # 전체 일괄 종료
    ├── status_all.bat              # 전체 프로세스 및 하트비트 상태 점검
    ├── restart_bot.bat             # 빗썸 봇 재시작 스크립트
    ├── restart_upbit_bot.bat       # 업비트 봇 재시작 스크립트
    └── restart_dashboard.bat       # 통합 대시보드 재시작 스크립트
```

---

## 3. 핵심 모듈 설계 및 동작 원리

### 3.1. 업비트 REST API 모듈 (`src/upbit_api.py`)
- **JWT 인증**: `HS512` 알고리즘을 사용하며, 요청 파라미터가 있을 경우 `unquote(urlencode(params, doseq=True))`의 SHA-512 해시 `query_hash`를 JWT 페이로드에 포함하여 무결성을 보증합니다.
- **주문 멱등성 (`identifier`)**: 모든 주문 요청(`POST /orders`)에 고유 클라이언트 식별자(`identifier=client_order_id`)를 전송하여, 네트워크 타임아웃이나 재시도 시에도 중복 체결이 발생하지 않도록 차단합니다.
- **호가 단위 및 수량 정밀도**: 업비트 최신 호가단위 규칙(100~1,000원 1원 단위, 10~100원 0.1원 등)에 맞춰 주문가를 자동 보정하고 수량을 안전하게 포맷팅합니다.
- **Rate Limit 백오프**: 429(Too Many Requests) 및 5xx 응답 수신 시 지수 백오프를 통해 안전하게 재시도합니다.

### 3.2. 업비트 실시간 WebSocket (`src/upbit_websocket.py` & `src/upbit_private_websocket.py`)
- **Public WebSocket (`UpbitWebSocketClient`)**: `wss://api.upbit.com/websocket/v1`에 상시 연결되어 0.1초 틱 시세 및 3,000만 원 이상 고래 대량 체결을 실시간 스트리밍합니다.
- **Private WebSocket (`UpbitPrivateWebSocketClient`)**: `wss://api.upbit.com/websocket/v1/private`에 JWT Authorization 헤더로 연결되어 `myOrder` 및 `myAsset` 이벤트를 bounded queue로 수신합니다. 주문 이벤트는 메인 스레드에서 저널을 `RECONCILIATION_PENDING`으로 전환할 수 있으나, 체결량·평균가·수수료 확정과 손익 반영은 REST 대사 결과만 사용합니다.

### 3.3. 7중 KRW-HOLO (홀로월드에이아이) 수동 종목 절대 보호망
사용자가 수동 매매하는 `KRW-HOLO`는 어떤 상황에서도 자동매매 시스템이 개입하지 못하도록 7중 방어선으로 완벽히 격리됩니다.
1. **환경설정/상수 기본값**: `UPBIT_EXCLUDED_MARKETS=KRW-HOLO` 기본값 강제 적용.
2. **시장 스크리닝**: `MarketScreener`가 시장 탐색 시 HOLO를 후보군에서 원천 제외.
3. **사전 리스크 가드**: `RiskGuard.validate_buy`에서 HOLO 매수 시도를 즉각 거부.
4. **안전 주문 집행기**: `SafeOrderExecutor.submit` 및 `UpbitAPI.create_order`에서 HOLO 주문 시 즉각 `ValueError` 발생.
5. **총 자산 및 보유목록 평가**: `calculate_total_equity`, `get_held_markets`, `build_positions_data`에서 계좌에 HOLO가 존재해도 평가금액을 0원으로 처리하고 목록에서 100% 제외.
6. **실시간 청산 및 긴급 전량매도 (Panic Sell)**: `RealtimeRiskEngine`의 틱 청산 및 `BotController.execute_panic_sell` 실행 시 HOLO는 매도 대상에서 영구 제외되어 사용자 수동 물량을 완벽히 보존.
- **7대 팩터 앙상블 스코어러 (`calculate_composite_alpha_score`)**: MTF 1H(15점) + VWAP(15점) + MACD 가속도(15점) + RSI 골든존(15점) + 볼린저 밴드(15점) + 수급/호가잔량비(15점) + 볼륨 스파이크(10점)를 100점 만점으로 산출합니다. 실제 승인선은 `StrategyPolicy` 단일 기준을 사용하며, 일반장 NORMAL 60점·BULL_TREND 65점·RISK_OFF 70점, 심야 NORMAL/RISK_OFF 75점·BULL_TREND 70점입니다.
- **AI 매수 분석 프롬프트 동기화**: AI 요청에는 해당 사이클의 BTC 레짐·심야 여부·후보 유형·진입 경로와 `StrategyPolicy`로 계산한 현행 알파 기준을 함께 전달합니다. 빗썸 Groq는 공통 시스템 지침과 함께, 업비트 Gemini는 기존 분석 프롬프트 계약과 함께 동작합니다. AI는 판단 보조이며, 로컬 하드 게이트·REST 주문 대사·WebSocket 상태·쿨다운·주문 저널·리스크 한도·호가 영향 검증을 우회할 수 없습니다. 누락 또는 모순 데이터는 `HOLD`로 응답해야 하며, 응답 JSON은 `ALPHA_SCORE`와 근거를 포함합니다.

### 3.7. 체결 및 마이크로스트럭처 제어 엔진 (Execution & Microstructure Engine)
- **실시간 슬리피지(Slippage Bps) 정밀 추적기 (`OrderFillProcessor`)**: 주문 시점의 목표 가격(`expected_price`)과 실제 거래소 체결 단가(`effective_price`) 간의 편차를 bps 단위로 실시간 계산하고, 허용 한도(30bps) 초과 시 이상 슬리피지를 감지 및 기록합니다.
- **매수 호가 영향 사전 검증 (`evaluate_buy_orderbook_impact`)**: 신규 매수 직전에 매도 호가 잔량을 소진하는 가정으로 예상 VWAP·슬리피지를 산출합니다. 호가 잔량이 주문 금액에 못 미치거나 예상 슬리피지가 100bps를 넘으면 차단 후보로 기록합니다. 기존 포지션의 손절·청산 시장가 주문에는 적용하지 않습니다.
- **5거래일 관찰 모드와 활성화 경계**: 기본값 `ORDERBOOK_SLIPPAGE_ENFORCEMENT=false`에서는 차단 후보를 `strategy_decisions`의 `OBSERVED`/`ORDERBOOK_SLIPPAGE`로만 기록하고 실제 주문은 변경하지 않습니다. 일일 평가에서 종목·예상 슬리피지·실제 체결 결과를 5거래일 누적 검증한 뒤에만, 명시적으로 `true`로 변경해 신규 매수 차단을 활성화합니다.
- **스마트 메이커 지정가 라우터 (`SafeOrderExecutor`)**: 호가 스프레드가 촘촘할 때 Best Bid에 즉각 스냅(Tick Snap)하여 메이커 수수료 절감 및 체결율을 극대화합니다.
- **주문 경계 Fail-Closed**: 신규 매수는 주문 직전 거래소 최신가를 다시 검증하며, 조회 실패·0 이하 가격·미완료 대사에서는 제출하지 않습니다. 기존 포지션의 매도 보호는 이 매수 차단 조건과 분리합니다.
- **동적 최우선 호가 추적 재정정 (`RealtimeRiskEngine.requote_pending_orders`)**: 미체결 매수 주문이 시세 상승으로 뒤처질 때 유효 범위(+0.8% 이내) 내에서 최우선 매수 호가로 자동 정정하여 체결 기회 상실을 방지합니다.

### 3.8. 백테스팅 및 데이터 엄밀성 검증 체계 (Backtesting & Data Rigor Engine)
- **Walk-Forward 시계열 롤링 전진 검증 (`QuantBacktester.run_walk_forward_backtest`)**: 캔들 데이터를 N개 롤링 윈도우로 분할하여 In-Sample 훈련 및 Out-of-Sample 전진 검증을 반복함으로써 전략의 시계열 과최적화를 차단하고 견고성 지표(Robustness Score)를 측정합니다.
- **몬테카를로(Monte Carlo) 1,000회 부트스트랩 리샘플링 (`QuantBacktester.run_monte_carlo_simulation`)**: 체결 손익의 무작위 셔플링을 통해 95% 신뢰수준 최대 낙폭(MDD VaR 95%)과 최악의 시나리오 및 파산 위험률(Risk of Ruin)을 통계적으로 산출합니다.
- **파라미터 민감도 그리드 분석기 (`QuantBacktester.run_sensitivity_analysis`)**: 리스크 비율 및 청산 파라미터 변화에 따른 계좌 성능 민감도를 비교 평가합니다.

### 3.9. 본전 보장(Break-Even) 및 타임스탑 지지선 보호 엔진
- **본전 보장(Break-Even) 스탑**: 1차 분할 익절(+2.5% 달성 시 30~50% 청산) 완료 즉시 잔여 수량의 손절가를 매수가+수수료(+0.3~+0.5%) 수준의 안전 마진으로 상향 고정하여 원금 손실을 원천 차단합니다.
- **타임스탑(Time-Stop) 지지선 보호**: 최대 보유 기간(예: 12봉/60분) 초과 시 무차별적인 시장가 투매 대신, 현재 수익권일 경우 본전 보장선 및 트레일링 스탑에 청산을 위임하여 추세 지속 이익을 극대화합니다.

### 3.10. 재진입 쿨다운, 당일 손실 한도 및 청산 가격 필터링 (`CooldownManager`)
- **시간 및 가격 2중 쿨다운**: 포지션 청산의 **REST 확정 체결 증가분** 뒤에만 기록합니다. 따라서 주문 ACK·미체결·불완전 WebSocket 이벤트가 손익·쿨다운을 앞당기지 않으며, 타임스탑 횡보 및 트레일링 익절 후 고점 추격 휩쏘를 방어합니다.
- **손절 후 30분 쿨다운 및 떨어지는 칼날 방어**: 손절(STOP_LOSS, HARD_STOP, 손절 방어, AI 비상탈출) 청산 발생 시 기본 30분(1,800초)의 재진입 쿨다운을 적용합니다. 또한 쿨다운 만료 후라도 직전 손절가 대비 -1.5% 미만으로 추가 하락 중인 경우(칼날 잡기) 지지선 확인 전까지 진입을 차단합니다.
- **당일 동일 종목 2회 손절 시 당일 거래 완전 차단 (Daily Market Blacklist)**: 당일 특정 종목에서 손절이 2회 누적되면 금일 23:59:59 KST 자정까지 해당 종목에 대한 신규 진입을 전면 차단하여 FLOCK/CHIP 등의 연속 손실 누적을 원천 방어합니다.
- **Gemini AI 단기 과열 하드 가드레일**: AI가 `ACTION: BUY`를 제시하더라도 5분봉 기술 지표(RSI > 65.0, 볼린저 %B > 0.88, MA20 이격도 > 103.5%, 체결강도 > 350%) 과열 감지 시 시스템이 `HOLD`로 강제 오버라이드하여 상투 추격 매수를 원천 차단합니다.
- **트레일링 청산 뒤 회복 확인**: 트레일링 청산 뒤에는 청산가 대비 최소 +1.5% 회복 전까지 재진입을 차단합니다. 청산가보다 낮은 가격의 재진입도 하락 재개 추격으로 간주해 허용하지 않습니다.
- **영속 저장 및 메모리 동기화**: `cooldown_state.json`에 일일 손절 횟수 및 쿨다운 상태가 영구 기록되어 봇 재시작 시에도 지속 유지되며, KST 자정 시 일일 손절 카운트가 자동 초기화됩니다.
- **연속 손실 리스크 관리**: 연속 손절 시 `get_risk_scale_factor()`(1회: 0.8, 2회 이상: 0.5) 자본 디스케일링 및 종목별 당일 한도 통제가 유기적으로 결합되어 계좌 리스크를 철저히 방어합니다.
- **급락 후 반등 전용 진입**: 회복 구간에서 일반 `RISK_OFF` 신호가 미달한 후보는 `recovery_rebound_signal()`로 한 번 더 평가합니다. BTC `CRASH`·주문 대사 미완료·시세 스트림 불안정·종목별 쿨다운은 절대 우회하지 않습니다. 확정 5분봉 반등, 핵심 하드게이트, 알파 75점 이상, BTC 대비 RS +1.5% 이상, 최소 거래대금, EMA20 대비 1% 이내의 1시간봉 회복을 모두 만족할 때만 양 거래소에서 실제 주문을 허용합니다. 반등 주문은 일반 슬롯의 35%를 기본 비중으로 하며, 동일 회복 구간에는 한 주문만 제출합니다.
- **확정봉 모멘텀 돌파 진입**: 반등형과 별도로 최신 확정 5분봉이 직전 4개 확정봉 고점을 돌파하고, 거래량이 최근 20개 확정봉 평균의 1.3배 이상이며, 양봉·RSI 52~72·BTC 대비 RS +0.8%·1시간 EMA20 대비 1% 이내 조건을 동시에 만족할 때만 진입합니다. 특히 당일 상승률 +3% 이상 및 BTC 대비 RS +1.5% 이상인 독자 랠리 주도주는 스크리너에서 `MOMENTUM_BREAKOUT`으로 자동 분류되어 눌림목 필터(%B/저점거리)에 걸리지 않고 돌파 진입 기회를 포착합니다. `RISK_OFF`는 주간 70점·심야 75점의 모멘텀 알파 기준을 적용합니다. 최초 진입은 거래소별 최대 포지션 한도의 25%로 제한하며, BTC `CRASH`, 시세 스트림 이상, 주문 대사 미완료, 쿨다운은 절대 우회하지 않습니다.

### 3.11. 알림 최적화 및 체결 이벤트 집중 파이프라인
- **노이즈 억제**: 미체결 주문 접수(ACK/OPEN) 시점의 불필요한 중복 알림을 제거하고, 실제 거래소 체결(FILLED) 시점에만 체결가, 체결수량, 슬리피지, 실현손익 차트 정보를 집중 발송합니다.
- **거래소 자체 알림과의 조화**: 텔레그램 알림은 봇의 핵심 리스크 상태, 일일 요약, 수동 원격 제어 응답 위주로 고도화되었습니다.

### 3.12. 듀얼 거래소 데이터 격리 & REST 체결 대사(Reconciliation) 안전망
- **경로별 SQLite 인스턴스**: `DatabaseManager`는 정규화된 DB 경로별 인스턴스를 관리합니다. 빗썸은 `data/trading.db`, 업비트는 `data/upbit/trading.db`만 사용하므로 초기화 순서에 따른 상태 혼입을 방지합니다.
- **REST 체결 대사 우선**: ACK는 주문 수락일 뿐 체결이 아닙니다. Private WebSocket은 대사 대기 신호로만 사용하며, `reconcile_exchange_statuses`가 수량·잔량·평균가·수수료를 검증한 뒤에만 포지션 진입시각, 실현손익, 쿨다운, 거래 메모리를 갱신합니다. 대사 미완료·모순·실패 시 신규 매수는 계속 차단됩니다.
- **마이그레이션 및 감사**: 거래소별 JSON은 자기 거래소 레코드만 전용 DB로 적재합니다. 완료 표식과 `sqlite_migration.audit.json`을 거래소별로 남겨 재실행 중복 적재를 막고 결과를 추적합니다.
- **짧은 수명 캐시와 관측성**: 시장별 분석은 2초 잔고 스냅샷을 재사용하되, 포트폴리오 산정과 취소/재호가 뒤에는 강제 최신 조회합니다. 대시보드 안전 상태에는 WebSocket stale·재연결·큐 깊이, `RECONCILIATION_PENDING`, REST 대사 시작/완료 시각 및 갱신·실패 건수, 슬리피지·호가/VWAP 지표를 포함합니다.
- **운영 판단 대시보드**: 통합 화면은 한 거래소라도 안전 상태가 불확실하면 신규 매수를 차단으로 표시하고, 거래소별 차단 사유를 분리합니다. 포지션은 청산 진행·표시 손절가 근접 여부로 화면 전용 우선순위를 정렬하며, 이는 주문 조건을 바꾸지 않습니다. 후보군은 전역 안전 차단·전략 관망·진입 검토 가능을 구분하지만 최종 주문 전 엔진의 안전 게이트를 반드시 다시 통과해야 합니다.
- **주문 확정 및 사건 관측**: 주문 저널은 `요청 → 접수(ACK, 체결 아님) → REST 대사 → 체결 확정` 흐름을 표시합니다. ACK·Private WebSocket 이벤트만으로 포지션·손익을 갱신하지 않으며 REST/Private WebSocket 확인 뒤에만 확정합니다. WARNING 이상 로그는 원문을 안전한 텍스트로 표시하고, 반복 경고는 화면에서 사건 단위로 요약합니다.
- **전략 판단 감사 이력**: 각 거래소의 `strategy_decisions` 테이블은 사이클·종목별 `HOLD`, 안전 차단, 종목별 쿨다운, 리스크 가드, 매수 승인 및 주문 제출을 기록합니다. 후보 상대강도·거래대금·BTC 레짐·하드게이트 결과·반등 전용 체크리스트를 JSON으로 보존하며, 판단 이력만 30일 뒤 정리합니다. 주문 원장과 체결 이력은 이 정리 대상이 아닙니다.

### 3.13. API 일일 사용량 & 쿼터 텔레메트리 모니터링 체계 (`api_telemetry.py` & `gemini_telemetry.py`)
- **거래소 REST API 호출 계측 (`ExchangeApiTelemetry`)**: 빗썸과 업비트의 모든 REST 호출(GET 조회, POST/DELETE 주문)의 호출수, 상태코드(200/429/5xx), Rate limit(429) 발생 건수, 최근 엔드포인트를 스레드 안전하게 실시간 집계합니다.
- **업비트 실시간 잔여 쿼터 파싱**: 업비트 응답 헤더(`Remaining-Req`)의 `sec`(초당 잔여) 및 `min`(분당 잔여)을 자동 추출하여 Rate limit 임계 도달 여부를 실시간 추적합니다.
- **KST 자정 기준 자동 롤오버 & 디스크 영속화(Persistence)**: 봇 프로세스를 재시작하더라도 오늘 하루 누적 사용량이 초기화되지 않도록 원자적 파일 쓰기로 분리 보관(`data/api_telemetry.json`, `data/gemini_telemetry.json`, `data/upbit/*`)하며, 한국 표준시(KST) 매일 00:00:00 자정을 기준으로 일일 누적 카운터를 자동 초기화합니다.
- **Gemini AI 쿼터 및 캐시 절감 관측**: Google AI Studio와 동일하게 `generateContent`·`ListModels`의 **모든 HTTP 시도**(성공·429·401/404/타임아웃 포함)를 실제 모델 ID별(`models_by_id`) 및 쿼터 그룹별(`quota_buckets`)로 집계합니다.
- **무료 티어 85% 하드 컷오프(Hard Cutoff) 및 429 원천 방지 (v8.30)**:
  - 분당 15 RPM 한도 준수를 위해 최소 호출 간격을 3.5초에서 **6.0초(최대 10 RPM, 33% 안전 마진)**로 상향했습니다.
  - 모델별 실제 쿼터(Flash-Lite 500 RPD, 일반 Flash 20 RPD)에 비례하여 85%(평시: Flash-Lite 425회, 일반 Flash 17회 / 긴급: 95%) 도달 시 Google API 호출을 원천 차단(`get_candidate_models`가 빈 리스트 `[]` 반환)하고 100% 로컬 퀀트 알고리즘 엔진으로 무중단 자동 전환하여 Google AI Studio의 429 에러 발생을 원천 차단합니다.
  - 보유 종목 평가(`evaluate_holding_position`) 시 정상 횡보 구간(-2.0% ~ +2.0%)은 15분(900초) 캐시를 적용하고, -2.0% 이하 급락 위기 또는 +2.0% 이상 급등 랠리 시에만 60초 적응형 재진단을 수행하여 5분 사이클마다 불필요하게 AI가 호출되던 쿼터 누수를 완벽히 제거했습니다.
- **통합 대시보드 UI 연동**: SPA 대시보드(`index.html`, `app.js`)의 `api_usage_panel` 위젯에서 빗썸 Open API, 빗썸 Gemini AI, 업비트 Open API, 업비트 Gemini AI를 4열 독립 그리드로 시각화하며, 탭 전환(`combined`/`bithumb`/`upbit`)에 따라 해당 거래소 사용량을 동적으로 강조합니다.

---

## 4. 변경 이력 및 개선 히스토리 (Changelog)

> 성능 경계: 전략 입력의 일괄 ticker·호가 값은 최대 1초만 재사용하며, 주문 직전 검증·체결 대사에는 사용하지 않습니다. 신규 진입 차단 상태에서는 후보용 AI 호출을 생략하지만 보유 포지션 방어는 계속 수행합니다.

| **v8.34** | 2026-09-07 | • **모멘텀 확장 후반(EXTENDED) 과잉 매수 차단 해소 및 주도주 진입 정상화**<br>• **모멘텀 초입 상한선 상향**: `StrategyPolicy.MOMENTUM_EARLY_MAX_CHANGE_RATE`를 기존 3.0%에서 **5.0%**로 현실화하여 3~5%대 주도주 파동의 초입 진입 기회 확보 (환경 변수 `MOMENTUM_EARLY_MAX_CHANGE_RATE` 연동)<br>• **EXTENDED 고확신 확인형 진입 허용**: 5% 초과 확장 구간 종목이라도 단순 돌파 추격만 차단하고, 로컬 퀀트 하드게이트를 통과하고 AI가 고득점(알파 80점 이상)으로 승인한 건전한 눌림목/추세 확인형 셋업은 정상 매수 허용<br>• **후보 분류 정합성 복원**: 일반 스크리너 풀에서 0.3% 수준의 미세 변동 종목이 `MOMENTUM_BREAKOUT`으로 오분류되는 현상을 방지하고 정상적인 `CONFIRMED` 후보 경로 보존<br>• **검증**: `test_market_screener.py`(4% EARLY 및 6% EXTENDED 검증), `test_ai_authority.py`(80점 미만 차단 및 80점 이상 허용 검증), 전체 358개 단위 테스트 100% 통과 |

| **v8.33** | 2026-09-07 | • **빗썸 Groq 복구 deadlock 및 배치 Schema 400 수정**<br>• **분석기 유지·주문만 차단**: FAST 장애 시 `build_bithumb_analyzer()`는 계속 생성하고 `get_bithumb_ai_entry_block_reason()`만 신규 BUY를 차단해 FAST 재시도로 `entry_safety` 자동 복구<br>• **Groq object 루트 정합화**: 후보 랭킹을 `rankings` 래퍼 객체로 변경하고 거시·보유·랭킹 FAST는 best-effort schema + 로컬 재검증, 신규 진입만 strict 유지<br>• **검증**: 분석기 유지·macro 성공 복구·object 루트 schema 회귀 테스트 추가, live Groq macro/ranking 호출 200 확인 |

| **v8.32** | 2026-09-07 | • **빗썸 Groq FAST 런타임 fail-closed 확장**<br>• **공통 신규 BUY 차단**: 20B의 4xx/429/5xx·타임아웃·JSON/스키마 오류를 영속 안전 상태로 기록하고 표준 AI·`MOMENTUM_BREAKOUT`·`RECOVERY_REBOUND`를 모두 차단<br>• **안전한 오류 진단**: 목적·모델·HTTP 상태·제한된 오류 코드만 대시보드에 표시하고 키·프롬프트·오류 원문·주문 식별자는 제외<br>• **거시 실패 방어**: Groq 거시 진단 실패는 `CAUTION_PULLBACK`/`AI_UNAVAILABLE`로 표시하며 정상 레짐으로 위장하지 않음<br>• **기존 포지션 보호**: REST 대사와 체결 확인, 손절·트레일링·긴급 청산은 계속 수행<br>• **검증**: Groq 안전 상태 복원·400 마스킹·정상 응답 복구·모멘텀 직접 진입 차단 회귀 테스트 추가 |

| **v8.31** | 2026-09-07 | • **상승 초입 참여·후발 동시 추격 방지 (Momentum Phase Entry)**<br>• **후보 단계 분리**: 공용 스크리너가 상대강도 +0.8% 이상인 모멘텀 후보에 `EARLY`(당일 +3% 이하) 또는 `EXTENDED` 단계를 기록하며, 양 거래소의 외부 응답 계약은 유지<br>• **주문 권한 일원화**: 공용 런타임은 확정봉·거래량·RSI·MTF·주문 안전 게이트를 통과해도 `EXTENDED` 단계의 신규 매수를 `HOLD`로 전환. 초입 후보만 최대 포지션의 25% 소액 경로를 사용<br>• **상관 노출 제한**: 한 5분 사이클에서 모멘텀 신규 주문은 1건만 제출해 여러 상승 후반 종목의 동시 추격을 차단. 기존 포지션의 손절·익절·체결 대사 경로는 변경하지 않음<br>• **Gemini 폴백 정합화**: 정상 AI와 401/쿼터 로컬 폴백 모두 모멘텀 단계를 프롬프트·캐시 키에 포함하며, AI 판단은 `EXTENDED` 신규 주문 제한을 우회할 수 없음<br>• **검증**: `test_market_screener.py`, `test_ai_authority.py`, `test_gemini_prompt_contract.py`에 초입/확장·폴백 우회 차단 회귀 테스트 추가 |

| **v8.30** | 2026-09-07 | • **Google Gemini 무료 티어(Free Tier) 쿼터 가드 & 하드 컷오프 패치**<br>• **15 RPM 한도 준수 (최소 6.0초 스로틀링)**: 연속 호출 간격을 3.5초에서 6.0초로 상향하여 분당 최대 10회 이하로 제한, 15 RPM 대비 33% 안전 마진 확보<br>• **모델별 85% 선제 하드 컷오프(Hard Cutoff)**: `can_call_model`에서 일반 Flash(한도 20회) 및 Flash-Lite(한도 500회)의 실제 한도 대비 85%(일반 Flash 17회, Flash-Lite 425회) 도달 시 Google API 호출을 원천 차단하고 즉시 로컬 퀀트 엔진으로 100% 무중단 전환하여 429 에러 원천 방지<br>• **보유 종목 적응형 캐시 정밀화**: 평시 횡보 구간(-2.0% ~ +2.0%)은 15분(900초) 캐시 유지, 실제 급락(-2.0% 이하) 또는 급등(+2.0% 이상) 변동 시에만 60초 적응형 진단으로 전환하여 사이클당 불필요한 AI 호출 누수 제거<br>• **브리핑 쿼터 가드 연동**: `get_briefing_candidate_models`에 모델별 쿼터 검증을 적용하여 일반 Flash 20회 소진 시 안전하게 Flash-Lite로 순차 폴백<br>• **단위 및 회귀 검증**: `test_gemini_quota_guard.py` 신규 작성 및 전체 330개 테스트 100% 통과 |

| **v8.27** | 2026-09-06 | • **RISK_OFF 상단 반등 진입 미세 확장 (Narrow Upper-Band Recovery Entry)**<br>• **레짐별 볼린저 상한 분리**: `NORMAL` 및 `BULL_TREND`는 기존 `%B 0.72` 상한을 유지하고, `RISK_OFF`에서만 RSI·MTF·이격·윗꼬리·MA 정렬·확정 양봉 반등을 모두 통과한 후보에 한해 `%B 0.75`까지 허용<br>• **게이트 정합화**: 약세장 하드게이트와 눌림목 상한을 동일한 `0.75`로 맞춰, 하드게이트 통과 후 일반 진입 신호에서 재차 차단되는 정책 불일치를 제거<br>• **안전 경계 유지**: 알파 70점, AI 단독 진입 기본 차단, 주문 대사·시세 상태·종목 쿨다운·킬스위치·BTC `CRASH` 차단 및 초기 진입 비중은 변경하지 않음<br>• **검증**: RISK_OFF `%B 0.74` 승인, `%B 0.76` 차단, 일반장 `%B 0.74` 차단 회귀 테스트 추가 |
| **v8.29** | 2026-09-07 | • **Gemini 텔레메트리 Google 실사용량 정합화**<br>• **모든 HTTP 시도 집계**: 성공·429뿐 아니라 401/404/타임아웃·모델 폴백 재시도·브리핑 Flash 호출·ListModels 조회까지 실제 모델 ID별(`models_by_id`)로 계측<br>• **쿼터 가드 연동**: `can_call_model`이 쿼터 그룹별 누적 HTTP 시도 수를 기준으로 동작하도록 정합화<br>• **검증**: `test_api_telemetry.py`, `test_gemini_pt_rollover.py` 갱신 |
| **v8.28** | 2026-09-06 | • **실시간 콜백 병목 관측·완화 및 AI 단독 진입 경계 정합화**<br>• **종목별 최신 가격 콜백 병합**: 양 거래소 Public WebSocket은 동일 종목 가격 이벤트를 하나만 대기시키고, 처리 시 최신 캐시 가격을 사용해 큐 적체를 줄임. Private WebSocket 체결 큐와 REST 체결 대사 흐름은 변경하지 않음<br>• **콜백 텔레메트리 확대**: 처리 건수, 평균·최대 실행시간을 시세 상태에 포함해 `PROCESSING_DELAY` 원인 분석을 지원<br>• **업비트 관측성·고래 이벤트 정합화**: 비정상 메시지의 최소 진단 로그와 고래 체결 30초 종목별 쿨다운을 추가<br>• **AI 단독 진입 fail-closed**: 알파 점수 누락은 차단하고, 명시 활성화 시에도 심야·레짐 공통 알파 기준을 적용. 기본 비활성 상태 유지<br>• **문서 정합화**: 모멘텀 돌파 RSI 상한을 실제 정책값 `72`로 수정 |
| **v8.26** | 2026-09-06 | • **통합 웹 대시보드 Tailscale VPN 전용 바인딩 및 접근 제어 고도화 (Tailscale Host Binding & Network Isolation)**<br>• **호스트 바인딩 Tailscale IP 전용화 (`DASHBOARD_HOST=100.76.22.126`)**: 기존 `0.0.0.0` 전체 개방으로 인한 LAN/외부 노출 위험을 원천 차단하고, Tailscale 가상 VPN 어댑터 IP(`100.76.22.126`)에 단독 바인딩하여 안전한 원격 접속 환경 구축<br>• **환경 변수 및 CLI 지원 확장**: `.env`의 `DASHBOARD_HOST`, `DASHBOARD_PORT` 연동 및 `dashboard_server.py`의 `--host`, `--port` CLI 인자 추가로 무중단 유연성 확보<br>• **프로세스 관리 및 배치 연동**: `process_manager.py` 상태 조회 및 `restart_dashboard.bat`에서 실제 바인딩된 Tailscale 접속 주소(`http://100.76.22.126:7979`) 정확한 안내 제공<br>• **단위 및 회귀 검증**: `tests/test_dashboard_server.py`에 호스트 바인딩 및 환경변수 주입 검증 추가, 전체 단위/회귀 테스트 100% 통과 |
| **v8.25** | 2026-09-06 | • **실거래 데이터 기반 매매 전략 결함 개선 & 손익비 구조 정상화 (Strategy Policy Normalization & Risk-Reward Enhancement)**<br>• **BULL_TREND(대세 상승장) 파라미터 전면 정상화**: 9월 손실의 60%를 차지하던 파라미터 왜곡 수정 ➔ 손절선 축소(-3.2% ➜ -2.0%), 1차 익절선 선제화(+4.5% ➜ +3.0%), 알파 기준 강화(55점 ➜ 65점), 타임스탑 축소(6시간 ➜ 2시간)<br>• **1차 분할 익절 비중 확대 (50%) & 조기 Break-Even 락인**: 1차 익절 도달 시 매도 비중을 30%에서 50%로 상향하여 선제 수익 파이를 확보하고, 즉시 잔여 물량 손절선을 평단가+0.3%(수수료 마진)로 확정 락인하여 승률 36~46% 환경에서도 수학적 손익비(RRR) 대폭 개선<br>• **볼린저 밴드 상한 캡 강화 (%B 0.60)**: %B 상한을 0.65에서 0.60으로 강화하여 5분봉 상단권 급등 꼭지 상투 추격 매수 원천 차단<br>• **데이터 파이프라인 무결성 복구**: `trade_memory.py`와 `db_manager.py` 간 컬럼 매핑(`exit_reason`, `market_regime`, `entry_time`, `exit_time`, `hold_duration_min`)을 100% 일치시켜 SQLite DB 누락 문제 해결 및 AI 자가학습 파이프라인 정상화<br>• **단위 및 회귀 검증**: `tests/test_storage_and_fill_boundaries.py` 신규 컬럼 매핑 검증 추가, `test_strategy_engine.py`, `test_bull_trend_strategy.py` 갱신 및 314개 전체 테스트 100% 통과 |
| **v8.24** | 2026-09-06 | • **Gemini AI 단독 자율 승인(AI Direct Entry) 기본 비활성화 & AI 역할 정상화 (AI Role Realignment & Fail-Closed Guard)**<br>• **AI Direct Entry 기본 비활성화 (`ENABLE_AI_DIRECT_ENTRY = False`)**: 실거래 데이터(빗썸 승률 30.0%, 업비트 23.1%) 분석을 통해 누적 손실의 주원인으로 확인된 '로컬 퀀트 관망 ➜ AI 단독 매수 진입' 경로를 기본 차단(fail-closed). 환경 변수 `ENABLE_AI_DIRECT_ENTRY=true` 명시적 설정 시에만 허용<br>• **관망 종목 AI 호출 사전 차단 & 쿼터 보존**: AI 단독 진입이 꺼져 있는 경우 로컬 룰이 관망인 종목에 대해 불필요한 Gemini API 심층 분석을 호출하지 않고 사전 차단하여 일일 RPD/RPM 쿼터 대폭 절약<br>• **AI 핵심 역할 고도화 (검증자 & 포지션 가디언)**: 실거래에서 승률 67~69%, 손익비 2:1 이상을 입증한 '로컬 퀀트 1차 하드게이트 통과 종목 2차 컨펌' 및 '보유 포지션 긴급 탈출(`evaluate_holding_position`)' 역할에 AI 역량 집중<br>• **단위 및 회귀 검증**: `tests/test_ai_authority.py` 갱신 및 전체 단위/회귀 테스트 100% 통과 |
| **v8.23** | 2026-09-06 | • **실거래 데이터 기반 안전망 전면 강화 (AI 과열 가드레일 / 손절 쿨다운 복원 / 당일 2회 손절 종목 차단 / RISK_OFF 파라미터 현실화)**<br>• **Gemini AI 추격 매수 방지 하드 가드레일**: AI가 `ACTION: BUY`를 제시하더라도 5분봉 기술 지표(RSI > 65.0, 볼린저 %B > 0.88, MA20 이격도 > 103.5%, 체결강도 > 350%) 과열 감지 시 시스템이 즉시 `HOLD`로 강제 오버라이드하여 급등 꼭지 상투 추격 매수 원천 차단<br>• **손절 후 30분(1,800초) 쿨다운 및 떨어지는 칼날 방어**: 손절 발생 시 즉시 재진입으로 인한 연속 손절을 막기 위해 30분 쿨다운 복원 및 손절가 대비 -1.5% 미만 폭락 중일 때 바닥 미확인 칼날 잡기 차단<br>• **당일 동일 종목 2회 손절 시 당일 거래 완전 차단 (`Daily Market Blacklist`)**: 당일 동일 종목 손절 2회 발생 시 KST 자정까지 해당 종목 신규 진입을 전면 차단하여 FLOCK, CHIP, NEAR 등의 당일 다중 손절 누적 원천 차단<br>• **비트코인 약세장(`RISK_OFF`) 진입 허들 상향 & 비중 축소**: `StrategyPolicy.ALPHA_BUY_THRESHOLD_RISK_OFF`를 60점에서 75점으로 상향하고, 진입 비중을 60%에서 40%로 축소하여 약세장 리스크 노출 최소화<br>• **타임스탑 본전 버퍼 정상화**: `TIME_STOP_BREAKEVEN_MIN_PNL_PCT`를 +0.05%에서 +0.30%로 상향하여 미세 수익(+0.08~+0.20%)에서 조기 털림을 방지하고 최소한의 추세 형성 기회 확보<br>• **단위 및 회귀 검증**: `tests/test_trade_improvement_guards.py` 신규 작성 및 전체 단위/회귀 테스트 100% 통과 |
| **v8.20** | 2026-09-05 | • **Google AI Studio 모델별 독립 쿼터(Flash-Lite 1,000 RPD) 체계 확장 & 일반 Flash 영구 차단 (Dual-Model Rotation & 1,000 RPD Expansion)**<br>• **실제 구글 스튜디오 스펙 동기화 (계정당 1,000 RPD)**: 구글 AI 스튜디오의 무료 티어 쿼터가 모델별 500 RPD(`3.1 Flash Lite` 500회 + `3.5 Flash Lite` 500회)로 제공됨을 확인하고, 봇별 기본 할당량을 500회에서 1,000회(통합 2,000회)로 상향 동기화<br>• **일반 Flash(20 RPD 제한) 영구 배제**: 일일 20회 제한으로 429를 유발하던 일반 Flash(`gemini-3.7-flash` 등)를 모델 감지/라우팅에서 원천 차단하고 순수 `flash-lite` 계열만 교차 활용<br>• **적응형 쿼터 임계 단계 확장**: 1,000회 기준 70%(700회 도달 시 사이클당 1개 종목 축소), 90%(900회 도달 시 신규 매수 AI 차단 및 100% 로컬 전환), 98%(980회 도달 시 긴급 탈출도 차단)로 비례 연동<br>• **대시보드 UI/서버 동기화**: `dashboard_server.py`, `app.js`, `index.html`에서 빗썸 1,000회, 업비트 1,000회, 통합 2,000회로 표기 및 소진율 계산식 일치<br>• **단위 및 회귀 검증**: `test_quota_compression.py` 갱신 및 전체 Gemini/전략 단위 테스트 100% 통과 |
| **v8.19** | 2026-09-05 | • **Gemini AI 호출 최적화 & 클래스 레벨 캐시 복원 (AI Quota & Multi-Tier Cache Optimization)**<br>• **클래스 레벨 캐시 승격 (`_MACRO_DIAG_CACHE`, `_SCREENER_RANK_CACHE`, `_HOLDING_EVAL_CACHE`, `_ANALYSIS_CACHE`)**: 5분 사이클마다 `GeminiAnalyzer` 객체가 새로 인스턴스화되면서 캐시가 유실되던 누수를 원천 차단하고 프로세스 전역에서 AI 결과를 온전히 재사용<br>• **런타임 엔진 인스턴스 재사용**: `TradingCycleEngine` 생성 시 `self.analyzer`를 영속화하여 매 사이클마다 불필요하게 생성되던 객체 오버헤드 제거<br>• **캐시 TTL 및 적중 관측 확대**: 스크리너 랭킹 캐시를 60초에서 600초(10분)로 대폭 연장하고, 거시 시황(30분), 보유 포지션(15분), 개별 분석(9분) 캐시 적중 시 `GeminiTelemetry.record_cache_hit`을 100% 집계<br>• **로컬 퀀트 알파 스코어 기반 1차 게이팅(Pre-qualification) 강화**: 관망(`allow_buy=False`) 종목의 경우 최소 알파 스코어 50점 이상(정상장 승인선의 80% 이상)인 유망 종목만 AI 심층 분석을 요청하도록 제한하여 20~40점대 비적격 종목의 무분별한 쿼터 낭비 차단<br>• **단위 및 회귀 검증**: `tests/test_gemini_cache_optimization.py` 신규 작성(4개 테스트 통과) 및 전체 Gemini/AI 단위 테스트 100% 통과 |
| **v8.18** | 2026-09-04 | • **AI 긴급 탈출(EMERGENCY_EXIT) 5중 확정적 안전 가드 & 수동 매수 포지션 보호 완비 (Emergency Exit Safety Guard & Manual Position Isolation)**<br>• **가드 1 (수동 포지션 보호)**: 봇 주문 저널(`order_journal`)에 매수 이력이 없는 외부/수동 매수 잔고는 봇의 자동 매매 및 긴급 탈출 시장가 매도 대상에서 전면 제외(완전 격리 보호)<br>• **가드 2 (AI 신뢰도 검증)**: AI 평가의 `confidence`가 80점 미만인 경우 일시적 시장 불안으로 인한 섣부른 긴급 탈출을 불허하고 HOLD 유지<br>• **가드 3 (진입 초기 노이즈 보호)**: 진입 후 10분(600초) 미만 보유 포지션은 호가 스프레드/잔파동에 털리지 않도록 탈출을 차단(단, -2.0% 이하 급락 또는 BTC 크래시 제외)<br>• **가드 4 (약보합/경미 손실 구간 보호)**: 손익률이 -0.60% 이상인 약보합/수익 구간에서는 시장가 투매로 슬리피지/수수료 손실을 확정짓지 않고 `TIGHTEN_STOP`(방어 손절선)으로 완화<br>• **가드 5 (캔들 퀀트 검증)**: 최신 5분봉이 양봉 지지 중인 경우 단순 지표 왜곡으로 판정해 긴급 탈출 유보<br>• **프롬프트 보강**: `gemini_analyzer.py`의 `evaluate_holding_position` 프롬프트에 진입 초기 노이즈 및 -0.8% 이내 미세 조정 시 EMERGENCY_EXIT 남발 금지 지침 탑재<br>• **단위 및 회귀 검증**: `tests/test_emergency_exit_guard.py` 신규 작성(8개 테스트 100% 통과), `test_trading_runtime.py` 등 기존 회귀 테스트 100% 통과 |
| **v8.17** | 2026-09-04 | • **손절 시 신규 매수 차단 전면 해제 & 바닥 재매수 기회 보존 (Stop-Loss Reentry & Bottom Dip Buying Freedom)**<br>• **종목별 손절 쿨다운 0초 적용**: `src/order_safety/cooldown.py` 및 `StrategyPolicy.COOLDOWN_STOP_LOSS_SEC`의 손절 후 쿨다운 타이머(기존 20~45분)를 0초로 변경하여 손절 직후에도 쿨다운 대기 없이 신규 진입 즉시 허용<br>• **손절가 상방 돌파 갭 필터(+1.5%) 제거**: 기존에 손절가보다 낮은 바닥 구간에서 2시간 동안 재진입을 원천 차단하던 갭 필터를 손절 청산 건에 대해 완전히 제거. 손절 후 추가 하락한 바닥 가격에서도 퀀트/AI 매수 승인 시 즉각 매수 집행<br>• **연속 손절 30분 쿨다운 비활성화**: `src/risk_manager.py`의 `add_realized_trade()`에서 연속 2회 손절 시 30분간 신규 매수를 전면 차단하던 `cooldown_until_ts` 발동을 제거하고, 동적 자본 디스케일링(`get_risk_scale_factor`: 1회 0.8, 2회 이상 0.5)만 안전하게 유지하여 계좌 리스크는 방어하면서 바닥 반등 매수 기회 보존<br>• **단위 및 회귀 검증**: `tests/test_order_safety.py`에 손절 후 바닥 재매수 즉시 허용 단위 테스트 보강, 전체 286개 단위 테스트 100% 통과 |
| **v8.16** | 2026-09-04 | • **주문 상태 FAILED 전면 한글화 & 텔레메트리 테스트 데이터 격리 복구 (Dashboard Order Status Localization & Telemetry Isolation)**<br>• **주문 상태 FAILED 전면 한글화**: `dashboard/src/app.js` (`formatOrderStatusBadge`, `renderActionBadge`, `formatReason`), `src/dashboard_server.py` 인라인 HTML에 `FAILED` / `FAIL` / `ERROR` ➜ `❌ 주문 실패` 뱃지 및 한글 매핑 완비 (월드코인 등 주문 실패 종목의 영문 노출 원천 해소)<br>• **Gemini 텔레메트리 테스트 격리**: 단위 테스트 실행 시 `data/gemini_telemetry.json` 실운영 파일이 0회로 초기화되던 부수 효과를 차단하기 위해 `tests/test_api_telemetry.py`, `tests/test_quota_compression.py`, `tests/test_operational_quality.py`에 임시 디렉토리(`tempfile.mkdtemp()`) 격리 탑재 및 `GeminiTelemetry.reset(persist=False)` 안전 가드 적용<br>• **당일 빗썸 Gemini AI 사용량 복원**: `trading.log` 실시간 집계(성공 122회 + 429 70회 = 총 192회 호출)를 디스크에 복원하고, 대시보드 및 내부 API 연동 정상화<br>• **단위 및 회귀 검증**: 전체 285개 단위 테스트 100% 통과 |
| **v8.15** | 2026-09-04 | • **무료 티어(500 RPD) 철저 준수를 위한 AI 호출 압축 & 4단계 쿼터 가드 (Daily 500 RPD Budget Guard)**<br>• **사이클당 AI 분석 상한(최대 2개)**: 매 5분 루프마다 스크리너 종합 랭킹 상위 1~2개 종목만 AI 심층 분석 대상으로 선정하고, 나머지 종목은 로컬 퀀트 판정으로 처리하여 사이클당 AI 호출을 엄격히 통제<br>• **1차 유효성 게이팅(Pre-qualification)**: 장대음봉 급락 캔들(-0.4% 초과 하락)이나 RSI 과열/침체(35~75 벗어남), 1시간봉 급락 종목은 AI를 부르지 않고 로컬에서 사전 관망 여과<br>• **보유 포지션 AI 진단 스마트 캐싱**: 손익률 횡보 상태(-0.5% ~ +1.0%)에서는 15분(900초) 캐시를 적용하고, 급락/급등 시에만 실시간 5분 진단 가동<br>• **일일 쿼터 적응형 가드(Adaptive Budget Guard)**: 당일 호출량 350회(70%) 시 사이클당 1개로 축소, 450회(90%) 시 신규 매수 AI 전면 차단(100% 로컬 전환), 비상 탈출용 50회 쿼터 완벽 보존<br>• **단위 및 회귀 검증**: `tests/test_quota_compression.py` 신규 작성(5개 테스트 통과) 및 전체 285개 단위/회귀 테스트 100% 통과 |
| **v8.14** | 2026-09-04 | • **Gemini AI 무료 티어 쿼터 최적화 & Flash-Lite 전용 라우팅 (Free-Tier Quota & Rate Limit Shield)**<br>• **Flash-Lite 전용화**: 무료 티어 일일 20회(20 RPD) 제한으로 429를 폭발시키던 일반 Flash 모델군을 기본 Fallback 및 동적 ListModels 감지에서 완전 배제하고, 500 RPD 쿼터를 가진 순수 `flash-lite` 계열(`gemini-3.8-flash-lite`, `3.5`, `3.1`, `latest`, `2.5`)만 전담 라우팅<br>• **15 RPM 준수 안전 스로틀링 (`_wait_for_rate_limit`)**: 연속 API 호출 시 최소 3.5초 지연 대기를 강제하여 무료 티어 분당 15회 한도(15 RPM) 순간 초과 원천 방어<br>• **429 쿨다운 단축 (900초 ➜ 120초)**: 일시적 분당 RPM 한도 초과 시 15분간 묶이던 쿨다운을 2분(120초)으로 현실화하여 분당 쿼터 리셋 후 신속하게 정상 AI 분석 복귀<br>• **전 모델 쿨다운 시 경고 로그 폭발 억제**: 모든 모델 쿨다운 진입 시 종목마다 매 5분 루프마다 수십 번 찍히던 WARNING 로그를 3분(180초)당 최대 1회로 스로틀링<br>• **`GeminiTelemetry.reset()` 동기화 보정**: `_ensure_configured_locked()` 호출을 추가하여 디스크 저장소 바인딩 보장 및 텔레메트리 일관성 강화<br>• **단위 및 회귀 검증**: `test_gemini_dynamic_models.py` 갱신 및 전체 280개 단위 테스트 100% 통과 |
| **v8.13** | 2026-09-04 | • **거래소별 독립 Gemini API 키 격리 & 대시보드 사용량 분리 시각화 (Isolated Gemini Key & Split Dashboard Telemetry)**<br>• **독립 Gemini API 키 격리 지원 (`UPBIT_GEMINI_API_KEY` / `BITHUMB_GEMINI_API_KEY`)**: 구글 계정 2개를 분리 운영할 수 있도록 거래소별 전용 환경 변수 우선순위 지원. 빗썸과 업비트가 각각 독립된 호출 쿼터(계정당 15 RPM 등)를 온전히 확보하여 동시 분석 가동<br>• **대시보드 거래소별 Gemini AI 사용량 분리 UI (`gemini_bithumb`, `gemini_upbit`)**: 통합 대시보드 API 사용량 패널을 4열 그리드로 확장하여 빗썸 Open API, 빗썸 Gemini AI, 업비트 Open API, 업비트 Gemini AI를 1:1로 분리 시각화. 빗썸/업비트 탭 전환 시 해당 거래소의 API와 Gemini 카드만 선명하게 동적 연동<br>• **스케줄러 오프셋 설정 지원 (`CYCLE_OFFSET_SECONDS`)**: 독립 계정 가동에 맞춰 기본값을 0초로 설정하여 불필요한 대기 없이 즉시 가동하되, 필요 시 환경 변수로 오프셋 초를 자유롭게 조정할 수 있도록 메커니즘 유지<br>• **단위 테스트 및 회귀 검증**: `test_api_telemetry.py`에 거래소별 Gemini 분리 검증 추가, 전체 345개 단위 테스트 100% 통과 |
| **v8.12** | 2026-09-04 | • **AI 트레이딩 권한 전면 확대 & 손익비 구조 정상화 (AI Direct Entry & Holding Autonomy)**<br>• **AI 단독 자율 승인 진입 (`AI Direct Entry`)**: 로컬 하드게이트 룰의 세부 조건(반등확인 미달 등)으로 관망 처리되더라도, 기본 안전망(비트코인 급락 차단, WS 정상, 쿨다운 해제, 최소 거래대금)을 통과한 유효 후보 종목에 대해 Gemini AI 심층 분석 기회를 전면 부여하고, AI가 `BUY`를 승인하면 `[AI 단독 자율 승인]`으로 즉각 매수 집행 허용<br>• **AI 포지션 자율 홀딩권 및 타임스탑 유예 (`AI Holding Autonomy`)**: 포지션 보유 중 15분/30분 타임스탑 및 조기 모멘텀 탈출로 인한 잦은 손절(-0.9~-1.5%)을 방지하기 위해, AI가 `evaluate_holding_position`에서 `HOLD` 또는 `RUNNER_HOLD`로 진단한 경우 기계적 타임스탑 손절을 자동 유예. AI의 손절가 하향 이탈 또는 `EMERGENCY_EXIT` 발생 시에만 즉각 손절 처리<br>• **알트코인 트레일링 스탑 드롭폭 정상화**: 잔파동 노이즈에 털리던 기본 트레일링 드롭폭(0.012 -> 0.020, 2.0%)을 `StrategyPolicy`와 일치시켜 휩소 털림 방지 및 온전한 추세 향유 지원<br>• **단위 테스트 및 회귀 검증**: `tests/test_ai_authority.py` 신규 작성(3개 테스트 통과), 전체 273개 단위 및 회귀 테스트 100% 통과 |
| **v8.24** | 2026-09-06 | • **Gemini 신규 진입 프롬프트 증거 우선 판단 고도화**<br>• **7대 팩터 판정 감사 절차**: 각 팩터를 충족·미충족·판정불가로 먼저 분류하고, 누락·모순 데이터는 0점 및 `HOLD`로 처리하도록 명시해 근거 없는 고점수·추측성 매수를 억제<br>• **후보 경로 혼합 방지**: `CONFIRMED`, `MOMENTUM_BREAKOUT`, `RECOVERY_REBOUND`의 전용 조건을 다른 경로의 근거로 대체하지 못하게 명시<br>• **반증 및 무효화 조건 의무화**: `REASON`에 핵심 팩터와 가장 강한 반대 근거, 매수 가설 무효화 조건을 제공된 수치만으로 기록하도록 하며, 주문·체결·대사·리스크 하드 게이트 및 JSON API 계약은 변경하지 않음<br>• **프롬프트 회귀 검증**: `tests/test_gemini_prompt_contract.py`에서 API 전송 프롬프트의 증거 우선·경로 분리·JSON 계약을 검증 |
| **v8.21** | 2026-09-05 | • **Gemini API 쿼터 PT(태평양시) 자정 리셋 및 3.5/3.1 모델별 분리 텔레메트리 고도화**<br>• **PT 자정(00:00) 롤오버 기준 확립**: Google AI Studio 일일 쿼터(RPD) 초기화 시점인 미국 서부(America/Los_Angeles) 자정 기준 자동 감지 엔진 탑재 (서머타임 PDT 시 한국 16:00 KST, 해제 PST 시 한국 17:00 KST 자동 리셋 및 타이머 계산)<br>• **3.5 Flash-Lite & 3.1 Flash-Lite 모델별 쿼터 분리 (각 500 RPD)**: `GeminiTelemetry`에 모델별 호출량·성공·429 횟수 독립 계측 및 쿼터 임계치 가드(`can_call_model`) 적용<br>• **스마트 라우팅 밸런싱**: `gemini_analyzer.py`에서 450회(90%) 이상 도달 시 여유 있는 자매 모델을 1순위로 즉시 승격시키는 동적 로드 밸런싱 구현<br>• **통합 대시보드 UI 연동**: `dashboard/index.html` 및 `app.js`에서 3.5 및 3.1 모델별 개별 프로그레스 바 및 수치 표시, PT 리셋 기준 시각 및 잔여 시간 실시간 렌더링 지원<br>• **단위 및 회귀 검증**: `tests/test_gemini_pt_rollover.py` 신규 작성(5개 테스트 통과) 및 기존 단위 테스트 100% 통과 |
| **v8.22** | 2026-09-05 | • **Gemini API 호출 쿼터 최적화 & 불필요한 관망(HOLD) 호출 낭비 원천 차단 (Quota Conservation & Smart Cache Synthesis)**<br>• **1시간봉(MTF 1H) 대세 추세 사전 필터**: 1시간봉 EMA20 < EMA50 역배열 또는 EMA20 -1.5% 초과 하락 종목은 로컬에서 즉시 관망 처리하여 어차피 Gemini 7대 팩터 1번에서 HOLD될 종목의 AI 호출 낭비 원천 제거<br>• **관망 종목의 AI 심층 분석 최소 알파 스코어 임계치 상향(50점 ➜ 55점)**: 승인 기준 60점에 근접한 유망 종목만 엄선하여 20~40점대 비적격 종목의 불필요한 호출 방지<br>• **스크리너 랭킹 개별 종목 점수 캐시(`_MARKET_AI_SCORE_CACHE`, 15분 TTL) 및 캐시 합성 도입**: 상위 후보군 중 60% 이상이 최근 15분 내 평가된 경우 API 재호출 없이 기존 점수를 합성하여 순서 변동 및 미세 종목 교체 시의 잦은 5분 단위 API 재호출을 65% 이상 절감<br>• **`analyze` 5분 타임블록 캐시 키 안정화**: 실시간 틱 가격 변동으로 인한 5분 봉 내 중복 호출 원천 차단<br>• **사이클당 AI 심층 분석 한도 제어 (`MAX_AI_CANDIDATES_PER_CYCLE`)**: 환경 변수 연동 지원 (기본값: 2)<br>• **단위 및 회귀 검증**: `tests/test_gemini_call_optimization.py` 신규 작성(4개 테스트 통과) 및 전체 Gemini/전략 단위 테스트 100% 통과 |
| **v8.11** | 2026-09-04 | • **시그널/폴백/안전성 결함 수정 및 동시성 락 강화**<br>• **전략 엔진 딕셔너리 중복 키 제거**: `src/strategy_engine.py`의 `checklist_details` 내 중복 정의된 `momentum_breakout` 키 제거 및 린트 정리<br>• **Gemini 로컬 폴백 팩터 누락 보완**: `src/gemini_analyzer.py`에서 API 호출 실패 폴백 시 `vwap_info` 및 `macd_acc` 인자 전달 누락 수정 (팩터 왜곡 방지)<br>• **실시간 청산 쿨다운 조기 선반영 제거**: `src/realtime_engine.py`에서 주문 제출 직후 쿨다운을 기록하던 코드를 제거하고, `OrderFillProcessor` 체결 확인 시점에만 단일 기록하도록 거래 안전 수칙 정합화<br>• **트레일링 스탑 재조정 대상 보강**: `src/risk_manager.py`의 `TrailingStopTracker.reconcile_markets`에서 `runner_markets`와 `dynamic_targets` stale 정리 포함<br>• **GeminiAnalyzer 동시성 락 탑재**: 멀티스레드 환경에서 모델 쿨다운/블랙리스트 및 캐시 안전성을 위한 `RLock` 도입<br>• **코드 품질 개선**: `risk_controls.py`, `trade_memory.py` 내 단일행 복수문 및 모호한 변수명 정리 |
| **v8.10** | 2026-09-04 | • **대시보드 체결 사유 및 상태 UI 전면 한글화 (`formatReason`, `formatTradeSideBadge`, `renderActionBadge`, `formatFeed`)**<br>• **체결 사유 영문 코드 한글 변환**: `dashboard/src/app.js`에서 `AI_EMERGENCY_EXIT` ➜ `🚨 AI 긴급 비상탈출`, `AI_TIGHTENED_STOP` ➜ `🤖🛡️ AI 손절선 상향 대응`, `AI_EXIT` ➜ `🤖 AI 청산`, `EMERGENCY_EXIT` ➜ `🚨 긴급 비상탈출`, `TIGHTEN_STOP` ➜ `🛡️ 손절선 상향 방어`, `RUNNER_HOLD` ➜ `🏃 추세 추종 홀딩`, 돌파/차단/쿨다운 사유까지 직관적인 한글 표기 완료<br>• **거래 구분 뱃지 및 행동 뱃지 확장**: `formatTradeSideBadge` 및 `renderActionBadge`에 AI 비상탈출(`🚨 AI비상탈출`), AI 손절상향(`🛡️ AI손절상향`), 즉시 청산, 관망/유지 뱃지 추가<br>• **주문 체결 엔진 연동**: `src/order_safety/fill_processor.py`에서 `stored_exit_reason`이 `AI_EMERGENCY_EXIT` 또는 `AI_TIGHTENED_STOP`일 때 실현 손익에 따라 `AI 긴급 익절탈출` / `AI 긴급 본전탈출` / `AI 긴급 비상탈출`로 정제 저장<br>• **내장 웹서버 동기화**: `src/web_server.py`의 인라인 fallback HTML에서도 동일한 한글 매핑 지원<br>• **단위 및 회귀 검증**: `tests/test_dashboard_frontend_korean.py` 신규 작성(3개 테스트 통과), `test_storage_and_fill_boundaries.py` 테스트 보강, 전체 266개 단위 테스트 100% 통과 |
| **v8.9** | 2026-09-04 | • **Gemini AI 3대 영역 고도화 (보유 포지션 동적 관리 / 후보 종목 배치 랭킹 / 매크로 레짐 & 브리핑)**<br>• **1순위 기보유 포지션 동적 청산 및 러너 추세 추종 (`evaluate_holding_position`)**: 매 5분봉 완료 시 보유 포지션 수급·캔들 진단. 세력 덤핑 징후 시 `EMERGENCY_EXIT` 선제 시장가 전량 탈출, 강력한 돌파 랠리 시 `RUNNER_HOLD` 목표가 상향, 지지선 안착 시 `TIGHTEN_STOP` 손절선 조기 상향 및 원금/수익 보존<br>• **2순위 스크리너 1회 배치 AI 랭킹 (`rank_candidate_markets`)**: 1차 스프레드/거래대금 통과 후보들을 단 1회 프롬프트로 Gemini에 전송하여 세력 덤핑/설거지 여과 및 Tier 1, 2, 3 우선순위 랭킹 부여 (호출 쿼터 극대화 보존)<br>• **3순위 거시 BTC 매크로 레짐 AI 진단 및 텔레그램 시황 브리핑 (`diagnose_macro_regime`, `generate_market_briefing`)**: 1시간 단위 BTC 캔들+F&G 종합 진단(30분 캐시)으로 거시 레짐 및 위험도 점수 연동, 09:00 모닝 결산 텔레그램에 3줄 AI 종합 시황 브리핑 자동 첨부<br>• **단위 및 회귀 검증**: `tests/test_gemini_ai_expansion.py` 신규 작성(6개 테스트 통과) 및 전체 262개 단위 테스트 통과 |
| **v8.8** | 2026-09-04 | • **대세 상승장(`BULL_TREND`) 레짐 신설 및 메이저 코인 추세 추종 개방**<br>• **`BULL_TREND` 레짐 자동 분류**: `classify_btc_regime()`에 1시간봉 EMA20/EMA50 정배열 및 12시간 상승세 유지 시 대세 상승장(`BULL_TREND`) 감지 엔진 탑재<br>• **메이저 코인(BTC/ETH/SOL) 매매 풀 개방**: `market_screener.py`에서 `BULL_TREND` 시 비트코인, 이더리움, 솔라나를 단타 제외 목록에서 자동 해제하여 상승장 시장 주도주에 직접 자금 배분 허용<br>• **상승장 노이즈 휩소 방어 손절선 유연화**: 틱 노이즈 털림 방지를 위해 기본 손절선을 -2.2%에서 -3.2%로 확장하고 급락 판정 임계값을 -3.5%로 완화<br>• **다단계 익절 및 트레일링 스탑 여유화**: 1차 분할 익절 +4.5%, 2차 +8.0%, 트레일링 시작 +4.0%, 드롭폭 2.5%로 조정하여 대세 랠리 파동을 끝까지 추종<br>• **타임스탑 대폭 연장**: `BULL_TREND` 시 타임스탑을 기본 360분(6시간), 지지선 유지 시 최대 480분(8시간)으로 연장하고 조기 모멘텀 탈출을 비활성화하여 횡보 후 폭등 파동 보호<br>• **단위 및 회귀 검증**: `tests/test_bull_trend_strategy.py` 신규 작성(6개 테스트 통과) 및 57개 회귀 테스트 100% 통과 |
| **v8.7** | 2026-09-03 | • **방어 필터 현실화 & 모멘텀 포착력 강화 (3대 개선안 통합)**<br>• **모멘텀 주도주 스크리닝 경로 확장**: `market_screener.py`에서 당일 상승률 +3% 이상 및 BTC 대비 RS +1.5% 이상인 독자 랠리 주도주를 `MOMENTUM_BREAKOUT`으로 분류하여 고점 돌파 로직으로 진입 허용<br>• **`StrategyPolicy` RISK_OFF 파라미터 현실화**: 약세장 RSI 상한선 완화(58.0 ➜ 65.0), 볼린저밴드 %B 상한선(0.55 ➜ 0.70), 저점 허용 거리(2.5% ➜ 3.5%), MA20 최대 이격도(+2.5% ➜ +3.5%), 돌파 RSI 상한(70.0 ➜ 75.0) 조정<br>• **반등확인(`rebound_confirmed`) 유연화**: 양봉 전환(현재가 >= 시가)은 fail-closed로 유지하되, 직전 종가 회복은 0.2% 미세 버퍼 허용 및 알파 점수 우수 종목(>= 60) 양봉 인정 연동<br>• **단위 및 회귀 검증**: `tests/test_strategy_improvements.py` 신규 작성(4개 테스트 통과) 및 전체 32개 전략/런타임 단위 테스트 100% 통과 |
| **v8.6** | 2026-09-03 | • **Gemini 동적 모델 자동 감지 & 자율 수명주기(Self-Healing) 엔진 탑재**: `gemini_analyzer.py`에 Google Generative Language API(`ListModels`) 실시간 연동, 하드코딩 모델 목록 의존 탈피<br>• **Flash-Lite 절대적 최우선(Tier 1) 라우팅**: 초고속 레이턴시 및 높은 RPM/TPM 쿼터 효율을 위해 최신 `flash-lite` 계열(예: `3.8-flash-lite` > `3.7` > `3.5`)을 무조건 1순위로 선별하고 일반 `flash`는 차순위 비상망으로 배치<br>• **지원 종료 모델 24시간 블랙리스트 격리**: 404 Not Found 또는 지원 종료 에러 발생 시 동적 블랙리스트에 등록하여 재호출 원천 차단<br>• **타임아웃 단기 쿨다운(3분) & 타임아웃 25초 조정**: 일시적 구글 서버 응답 지연 발생 시 단기 쿨다운 후 차순위 모델로 즉시 전환<br>• **6시간 TTL 캐시 & 다중 안전망(Fail-Safe)**: ListModels API 실패 시 기본 Fallback 목록 및 100% 로컬 퀀트 알고리즘 엔진 무중단 전환 보장<br>• **단위 및 회귀 검증**: `tests/test_gemini_dynamic_models.py` 신규 작성(6개 테스트 통과) 및 기존 전략/리스크 회귀 테스트 통과 |
| **v8.5** | 2026-09-03 | • **API 일일 사용량 & 쿼터 텔레메트리 탑재**: `src/api_telemetry.py` 신설, 빗썸/업비트 REST 호출량(조회/주문 구분, 429 횟수, 업비트 초/분당 잔여 쿼터) 스레드 안전 계측<br>• **KST 자정 자동 롤오버**: 한국 표준시 자정 기준 거래소 및 Gemini 일일 카운터 자동 롤오버<br>• **Gemini 관측 강화**: 일일 한도(1,500 RPD) 프로그레스 바, 캐시 방어 횟수, 429 및 로컬 폴백 횟수 대시보드 연동<br>• **대시보드 UI 카드 신설**: `dashboard/index.html` 및 `app.js`에 실시간 API 사용량 & 쿼터 위젯 탑재, 거래소 탭별 동적 강조 지원<br>• **회귀 검증**: `tests/test_api_telemetry.py` 신규 작성 및 전체 239개 테스트 100% 통과 |
| **v8.4** | 2026-09-02 | • **PR-3D~3H Phase 3 잔여 완료**: `requirements.txt` 버전 고정, `pyproject.toml`+`requirements-dev.txt`로 ruff 도입, CI lint 단계 추가<br>• **PR-3G 슬리피지 관찰 자동화**: `operational_quality.build_slippage_enforcement_readiness()`로 5거래일 `ORDERBOOK_SLIPPAGE` 관찰 충족 여부 산출, `/diag`·대시보드 진단에 노출<br>• **PR-3H Gemini 관측**: `gemini_telemetry.py`로 API 성공·429·로컬 폴백·캐시 적중 집계, `GeminiAnalyzer` 및 `BotController` 진단 연동<br>• **PR-3F 예외 정리(부분)**: 신규 품질 모듈과 `CooldownManager` 저장 경로에 구체 예외 적용<br>• **회귀 검증**: `test_operational_quality.py` 추가 및 order_safety/진단 관련 테스트 통과 |
| **v8.3** | 2026-09-02 | • **PR-3C `order_safety` 패키지 분리**: 단일 `order_safety.py`를 `order_safety/` 패키지로 분리 (`types`, `journal`, `executor`, `fill_processor`, `cooldown`, `orderbook`, `markets`)<br>• **책임 경계 정리**: 리스크 가드·사이징은 `risk_controls.py`, JSON 영속 저장은 `state_store.py`로 이미 분리된 경계를 `order_safety.__init__`에서 re-export해 기존 import 경로 유지<br>• **거래 안전 보존**: 주문 저널·REST 대사·체결 증가분 멱등성·HOLO 차단·쿨다운 로직 변경 없음<br>• **회귀 검증**: `test_order_safety`, `test_execution_safety`, `test_storage_and_fill_boundaries` 통과 |
| **v8.2** | 2026-09-02 | • **PR-3B 워치독 통합**: `TradingBotWatchdog`로 하트비트 감시·프로세스 재시작·crash-loop 방어·텔레그램 알림을 `src/trading_watchdog.py`로 공통화<br>• **`ExchangeWatchdogProfile`**: 거래소별 data 경로·main 스크립트·로그/알림 문구·중복 잠금 대기 시간 분리<br>• **entry point 슬림화**: `watchdog.py`·`watchdog_upbit.py`는 로깅·env·profile wiring + `watchdog.run()`만 유지<br>• **거래 안전 보존**: `data/` vs `data/upbit/` 하트비트·lock·pid 경로, 120초 grace / 600초 stale 임계값, 5회 crash-loop 방어 동작 변경 없음<br>• **회귀 검증**: `tests/test_trading_watchdog.py` 추가 |
| **v8.1** | 2026-09-02 | • **PR-3A `TradingBotBootstrap` 도입**: `main()` 부트스트랩(텔레그램·내부 API·WS·전략 캐시 복원·APScheduler·graceful shutdown·하트비트 루프)을 `src/trading_bot_bootstrap.py`로 추출<br>• **`ExchangeBootstrapProfile`**: 거래소별 포트·경로·job ID·로그 문구·메인 루프 예외 처리 플래그 분리<br>• **entry point 슬림화**: `main.py`·`main_upbit.py`는 profile + `cycle_engine` wiring + `bootstrap.run()`만 유지<br>• **거래 안전 보존**: data/ 경로·내부 API 포트·HOLO·REST 대사·WS drain 직렬화 동작 변경 없음<br>• **회귀 검증**: `tests/test_trading_bot_bootstrap.py` 추가 및 startup/isolation 테스트 통과 |
| **v8.0** | 2026-09-02 | • **PR-2E `run_cycle()` 완결**: `TradingCycleEngine.run_cycle()`로 prefix·마켓 루프·suffix(전략 캐시 정리)를 일원화하고 entry point는 `cycle_engine.run_cycle()`만 호출<br>• **`ExchangeCycleProfile` 확장**: 마켓 분석 로그 문구, HOLO 루프 스킵, 사이클 오류 로그 접두사를 profile로 분리<br>• **거래 안전 보존**: 업비트 제외 종목 스킵·거래소별 strategy cache 경로·fail-closed 동작 변경 없음<br>• **회귀 검증**: suffix 정리·제외 종목 스킵 테스트 추가 |
| **v7.9** | 2026-09-02 | • **PR-2D 손절·신규 매수 실행 공통화**: `process_cycle_stop_loss()`·`process_buy_execution()`으로 5분 사이클 손절 검사와 매수 주문 제출을 빗썸·업비트 공통 처리<br>• **`ExchangeBuyProfile` 도입**: 슬롯/비중 예산, 선행 safety 재검증, BTC 급락 알트 차단, 로그 문구 등 거래소별 차이를 profile로 분리<br>• **거래 안전 보존**: exit lock·REST 대사 전 손익 미반영·호가 슬리피지 관찰/차단·업비트 기존 매수 경로 동작 유지(업비트는 5분 사이클 손절 비활성)<br>• **회귀 검증**: `tests/test_trading_runtime.py` 손절·미해결 주문 차단 테스트 추가 |
| **v7.8** | 2026-09-02 | • **PR-2C 진입/AI 게이팅 공통화**: `TradingCycleEngine.process_entry_gating()`로 확정봉·entry_signal·반등/모멘텀·AI 전략·사이징·`LATEST_STRATEGIES`·audit을 빗썸·업비트 공통 처리<br>• **`ExchangeEntryProfile` 도입**: 업비트 선행 safety/reentry/candles 검사, hold 가격 fallback, whale flow capability, 빗썸 inactive status continue 등 거래소별 차이를 profile로 분리<br>• **버그 수정**: prefix에서 `is_extreme_fear`를 전달해 업비트 `fng` 미정의 참조 제거<br>• **회귀 검증**: `tests/test_trading_runtime.py` 진입 게이트 경계 테스트 추가 |
| **v7.7** | 2026-09-02 | • **PR-2B 최우선 청산 공통화**: `TradingCycleEngine.process_priority_exits()`로 분할익절·트레일링 스탑·타임스탑·모멘텀 조기 탈출을 빗썸·업비트 공통 처리<br>• **`ExchangeExitProfile` 도입**: 거래소별 분할익절 비율·로그 문구·트레일링 차트 렌더·타임스탑 재검증 차이를 profile로 분리<br>• **거래 안전 보존**: exit lock·REST 대사 전 손익 미반영·업비트 HOLO 제외·빗썸 2차 분할(30/70) 비율 유지<br>• **회귀 검증**: `tests/test_trading_runtime.py` 청산 경계 테스트 추가 |
| **v7.6** | 2026-09-02 | • **PR-2A `TradingRuntime` 골격 도입**: `src/trading_runtime.py`에 `TradingCycleEngine.run_cycle_prefix()`를 추가해 환경 로드·REST 대사·포트폴리오 갱신·BTC 레짐·스크리닝·WS 구독·audit 설정을 빗썸·업비트 공통화<br>• **entry point 슬림화**: `main.py`·`main_upbit.py`의 `run_cycle()` 전반부만 공통 엔진 호출로 교체하고, 청산·진입 루프는 기존 위치 유지<br>• **거래 안전 보존**: 거래소별 env·DB·제외 종목·로그 문구는 `ExchangeCycleProfile`로 분리, HOLO 제외·REST 대사·fail-closed BTC 레짐 동작 변경 없음<br>• **회귀 검증**: `tests/test_trading_runtime.py` 추가 및 startup/isolation/adapter 계약 테스트 통과 |
| **v7.5** | 2026-09-02 | • **`TRAILING_STOP` 재진입 분기 수정**: `CooldownManager.check_reentry_allowed()`에서 `TRAILING` 분기를 `STOP`보다 먼저 평가해 트레일링 청산 후 회복 조건이 손절 로직에 흡수되지 않도록 보완<br>• **Windows 테스트 SQLite 잠금 해소**: 임시 DB는 `DELETE` 저널 모드, `DatabaseManager.dispose()`·`reset_db_manager_cache()` 및 `tests/db_test_cleanup.py` 훅으로 tearDown 시 파일 잠금 방지<br>• **HOLO 스크리너 테스트 정합화**: 메이저 제외 풀 반영 후 `KRW-DOGE` 후보 기준으로 HOLO 제외만 검증<br>• **CI 추가**: `.github/workflows/test.yml`에서 `unittest discover` 자동 실행<br>• **회귀 검증**: 전체 209개 단위 테스트 통과 |
| **v7.4** | 2026-09-02 | • **트레일링 청산 후 재진입 방어 강화**: `CooldownManager`가 트레일링 청산가보다 낮거나 회복폭 +1.5% 미만인 가격의 재진입을 차단하도록 보완<br>• **매수 전 호가 영향 검증 추가**: 양 거래소 신규 매수 직전에 매도 호가 잔량 기반 예상 VWAP·슬리피지를 계산하고, 잔량 부족 또는 100bps 초과를 `ORDERBOOK_SLIPPAGE` 감사 기록으로 남김<br>• **공격형 확정봉 모멘텀 돌파**: 빗썸·업비트에 최신 확정 5분봉 고점 돌파·거래량 1.3배·양봉·RSI 52~70·RS +0.8%를 함께 확인하는 `MOMENTUM_BREAKOUT` 경로를 추가하고, 최초 비중을 최대 포지션의 25%로 제한<br>• **RISK_OFF 진입 완화**: 일반 알파 기준을 주간 60점·심야 70점으로 조정하되, BTC `CRASH`, 호가·시세 스트림 이상, 주문 대사 미완료, 쿨다운 차단은 유지<br>• **회귀 검증**: 트레일링 청산 후 하락 재진입 차단, 호가 잔량·예상 슬리피지 경계, 모멘텀 돌파 승인·차단 경계 테스트 추가 |
| **v7.3** | 2026-09-01 | • **심야 알파 진입 기준 SSOT 정합화**: `get_alpha_buy_threshold()`로 주간 NORMAL 60점·주간 `RISK_OFF` 70점·심야 NORMAL 75점·심야 `RISK_OFF` 80점을 단일화하고, 7대 알파 점수 표시와 최종 `entry_signal()` 주문 게이트가 같은 기준을 사용하도록 수정<br>• **반등 전용 경로 심야 우회 차단**: `recovery_rebound_signal()`은 반등 기본 75점과 현재 세션 기준 중 높은 점수를 적용해, 심야 `RISK_OFF`에서 75~79점 신호가 80점 기준을 우회하지 못하도록 보강<br>• **회귀 검증**: 심야 NORMAL 74/75점 및 심야 `RISK_OFF` 79/80점 경계값의 최종 진입 차단·승인 테스트 추가 |
| **v7.2** | 2026-09-01 | • **급락 후 반등 전용 실거래 경로**: 빗썸·업비트 공통 `recovery_rebound_signal()`을 추가해 일반 `RISK_OFF` 신호가 미달한 회복 후보를 확정봉·알파·RS·유동성·MTF 조건으로 엄격히 재평가하고, 통과 시 일반 슬롯의 35% 축소 비중으로 실제 주문 경로에 연결<br>• **쿨다운 의미 명확화**: 연속 2회 손절은 30분 회복 모드와 리스크 규모 축소를 적용하며, 종목별 재진입 차단과 분리됨을 명시. 반등 전용 경로도 BTC 급락·시세 불안정·주문 대사 미완료·종목별 쿨다운을 우회하지 않음<br>• **전략 판단 이력**: 거래소별 SQLite `strategy_decisions`에 매수·관망·차단·주문 제출과 정량 근거를 30일 보존하고, 주문 원장·체결 이력과 분리<br>• **회귀 검증**: 반등 승인·차단, CRASH 우회 금지, 거래소별 회복 주문 제한 검증 추가 |
| **v7.1** | 2026-08-31 | • **경로별 SQLite DB 격리**: 빗썸 `data/trading.db`, 업비트 `data/upbit/trading.db`를 `TradeMemoryManager`·`OrderJournal`·리스크 상태 저장소에 통일<br>• **체결 확정 경계 강화**: ACK 뒤 50ms 단건 조회 제거, Private WS는 대사 대기만 표시, REST 검증 체결 뒤에만 손익·보유시간·쿨다운·거래 메모리 갱신<br>• **신규 매수 Fail-Closed 및 REST 절감**: 주문 직전 최신가 검증, 대사 미완료·불확실 데이터 차단, 2초 잔고 스냅샷 재사용과 강제 최신 조회 경계 명시<br>• **상태 저장·관측성 강화**: 고빈도 최고가 저장 디바운스, 주문 대사 백필 지표를 안전 상태 API에 추가<br>• **회귀 테스트 추가**: DB 경로 캐시와 확정 체결 뒤 쿨다운 경계 검증 |
| **v7.0** | 2026-08-30 | • **타임스탑 및 본전 보장(Break-Even) 지지선 보호 로직 고도화** (`main.py`, `main_upbit.py`, `realtime_engine.py`)<br>• **재진입 쿨다운 관리 개선 및 청산 가격 기반 재진입 검증 탑재** (`CooldownManager`)<br>• **모멘텀 및 기술적 청산 사유 레이블 세분화 및 디스크 캐시 저장 로직 개선**<br>• **텔레그램 알림 노이즈 최적화**: 주문 접수(ACK) 단계 중복 알림 제거 및 실체결(`OrderFillProcessor`) 집중화<br>• **Upbit REST 체결 대사(Reconciliation) 안전망 및 거래 메모리 마이그레이션 격리 완성**<br>• **배치 스크립트 및 프로세스 관리 간소화** (`restart_bot.bat`, `restart_upbit_bot.bat`, `restart_dashboard.bat`, `process_manager.py`)<br>• **단위 테스트 스위트 확장**: 총 50개 테스트 모듈, 178개 단위 테스트 100% 통과 |
| **v6.3** | 2026-08-25 | • **실거래-백테스트 전략 단일 기준(SSOT) 및 확정봉 진입 체계 완결 (과제 A~F)**<br>• **`StrategyPolicy` 단일 진실 공급원 일원화**: 하드코딩 오프셋 제거, 목표가/손절가/트레일링/쿨다운 실거래 및 백테스트 100% 동기화<br>• **하드 안전 게이트(`Hard Safety Gates`) vs 소프트 알파 점수 분리**: RSI 극초과열(>75) 및 볼린저 이탈 시 알파점수 우회 원천 차단<br>• **확정봉(Completed Bar) 기준 신호 생성 체계 확립**: 5분 주기에서 미완성 봉(`candles[0]`) 지표 흔들림 배제 및 `candles[1:]` 확정봉 기준 판정<br>• **시장 레짐별(NORMAL / RISK_OFF) 백테스트 성과 분리 분석기 및 호가 롤링 완충기(`OrderbookFlowTracker`) 탑재**<br>• **매매 복기 메모리(`TradeMemoryManager`) 퀀트 메타데이터 정량 태깅 및 레짐/알파티어 분석 엔진 탑재**<br>• **신규 전략 SSOT 및 하드게이트 단위 테스트 추가 (단위 테스트 100% 통과)** |
| **v6.2** | 2026-08-25 | • **실거래 안전성 핵심 2대 과제 완벽 완결 (Private WS ➜ FillProcessor & Directional Tick Rounding)**<br>• **Private WebSocket 체결 이벤트를 공통 체결 처리기(`OrderFillProcessor`)에 100% 연결** (`main.py`, `main_upbit.py`)<br>• **REST 체결 재조정(`reconcile_exchange_statuses`) 5분 사이클 연동으로 웹소켓 단선 시 미체결 복구망 구축**<br>• **주문 방향별 호가 보정 분리** (매수: Floor 내림, 매도: Ceil 올림, `get_tick_size` 및 `adjust_price_to_tick`) |
| **v6.1** | 2026-08-25 | • **트레일링 스탑 최소 안전 마진 상향 (+0.2% ➜ +0.5%)** (`TrailingStopTracker.min_guaranteed_profit`)<br>• **실현 손익 기반 청산 사유 레이블 직관화** (이익: `트레일링 익절`, 본전: `트레일링 본전방어`, 손실: `트레일링 방어매도`)<br>• **텔레그램, 구글 시트, 웹 대시보드, 매매 메모리 청산 레이블 일원화** |
| **v6.0** | 2026-08-25 | • **시스템 자체 종합 평가 100 / 100 점 만점 완성 💯**<br>• **운영 편의성 및 모니터링 10/10 만점 고도화 완료**<br>• **실시간 시스템 정밀 진단 텔레메트리 탑재** (`BotController.get_diagnostics_data`)<br>• **텔레그램 원격 진단 명령어(`/diag`, `/health`) 및 체결 품질 조회(`/trades`) 신설**<br>• **듀얼 거래소 하트비트 진단 CLI (`process_manager.py status`) 강화** |
| **v5.5** | 2026-08-25 | • **백테스팅 및 데이터 엄밀성 10/10 만점 고도화 완료**<br>• **Walk-Forward Cross-Validation (시계열 롤링 전진 검증) 엔진 신설** (`QuantBacktester.run_walk_forward_backtest`)<br>• **1,000회 몬테카를로(Monte Carlo) 부트스트랩 리샘플링 스트레스 테스터 탑재** (`QuantBacktester.run_monte_carlo_simulation`, MDD VaR 95% 산출)<br>• **파라미터 리스크 민감도 그리드 분석기 구현** (`QuantBacktester.run_sensitivity_analysis`) |
| **v5.4** | 2026-08-25 | • **체결 및 마이크로스트럭처 제어 15/15 만점 고도화 완료**<br>• **실시간 체결 슬리피지(Slippage Bps) 추적 및 30bps 초과 감지 엔진 탑재** (`OrderFillProcessor`, `OrderJournal`)<br>• **동적 최우선 호가 추적 재정정(Pegged Re-quoter) 고도화** (`RealtimeRiskEngine`)<br>• **주문 제출 및 체결 전 구간 `expected_price` 슬리피지 파이프라인 완성** |
| **v5.3** | 2026-08-25 | • **전략 및 알파 창출력 20/20 만점 고도화 완료**<br>• **VWAP (거래량 가중 평균가) 기관 수급 분석 지표 신설** (`calculate_vwap`)<br>• **MACD 히스토그램 모멘텀 가속도(Slope & Expansion) 지표 연산 탑재** (`calculate_macd_acceleration`)<br>• **7대 복합 팩터 앙상블 알파 스코어러(100점 만점 중 65점 이상 진입) 체계 일원화** (`calculate_composite_alpha_score`)<br>• **AI 프롬프트 및 로컬 퀀트 폴백 엔진 앙상블 알파 연동** |
| **v5.2** | 2026-08-25 | • **리스크 관리 및 방어망 25/25 만점 고도화 완료**<br>• **거시 BTC 급락 시 전 포지션 익절선 초밀착 타이트닝 비상 방어 모드 탑재** (`TrailingStopTracker.set_macro_defensive_mode`)<br>• **단일 종목 절대 손실 하드 스탑(-4.5%) 즉시 청산망 구축** (`RealtimeRiskEngine` Hard-Stop)<br>• **연속 손실 기반 동적 자본 디스케일링(100% ➜ 80% ➜ 50%) 연동** (`calculate_risk_position_size` scale factor) |
| **v5.1** | 2026-08-25 | • **아키텍처 및 시스템 안정성 20/20 만점 고도화 완료**<br>• **원자적 파일 쓰기(`.bak` 백업 동기화) & JSON 자가 치유(Self-Healing) 복구 엔진 탑재**<br>• **완벽한 Graceful Shutdown 자원 해제 라이프사이클 구축** (`TelegramAlert.stop()`, WebServer, WebSocket, APScheduler, SIGBREAK 지원)<br>• **공유 상태(State) 스레드 안전성(RLock) 전면 강화** (`DailyRiskManager`, `TrailingStopTracker`, `CooldownManager`, `TradeMemoryManager`)<br>• **워치독(Watchdog) 하트비트(Heartbeat) 기반 10분 무응답(Hang/Deadlock) 자동 복구 시스템 탑재** |
| **v5.0** | 2026-08-25 | • **업비트(Upbit) API 기반 자동매매 시스템 신규 구축 및 듀얼 거래소 완전 분리 완료**<br>• **업비트 전용 REST API (`UpbitAPI`) & Public/Private WebSocket 클라이언트 탑재** (HS512 JWT, unencoded query string SHA-512 hash, `identifier` 멱등성 보장)<br>• **7중 KRW-HOLO(홀로월드에이아이) 수동 종목 절대 보호망 구축** (스크리닝, 주문, 청산, Panic Sell, 자산평가, 시트, 대시보드 배제)<br>• **거래소별 물리적 환경 분리** (`.env.upbit`, `data/upbit/*`, `logs/trading_upbit.log`, 웹 대시보드 포트 `7980`, 업비트 배치 스크립트 5종)<br>• **독립 프로세스 매니저 및 워치독 구축** (`process_manager.py` 듀얼 지원, `watchdog_upbit.py`) |
| **v4.5** | 2026-08-24 | • 웹 대시보드 최근 거래 및 주문 저널 최신순 정렬 최적화<br>• 모듈 분산 리팩토링 (`risk_manager.py`, `realtime_engine.py`, `bot_controller.py` 분리)<br>• 구글 시트 Strategy 탭 실시간 동기화 및 장중 자금 입출금 자동 보정(Cashflow Adjustment) |
| **v4.0** | 2026-08-24 | • 0.1초 실시간 웹소켓 즉각 손절/익절 엔진 탑재<br>• 결정론적 정량 진입 게이트 및 주문 저널(`OrderJournal`) 멱등성 구현 |

---

## 5. 유지보수 및 코드 수정 원칙 (For Developers & AI Agents)

1. **설계 문서 상시 동기화**: 코드 수정 또는 기능 추가 시 반드시 본 `PROJECT_DESIGN.md` 문서를 함께 최신 상태로 갱신합니다.
2. **전략-명령 프롬프트 동기화**: 전략을 추가·수정·삭제(진입·청산·필터·임계값·비중·레짐 경로 포함)할 때는 `src/gemini_analyzer.py`의 AI 매수 분석 명령 프롬프트도 반드시 같은 변경에서 최신화합니다. 프롬프트의 레짐·세션·후보 경로·승인 기준·안전 차단 조건·JSON 응답 스키마는 실제 `StrategyPolicy`와 실행 경로에 맞춰야 하며, 프롬프트 회귀 테스트도 함께 갱신합니다.
3. **거래소 격리 원칙**: 빗썸과 업비트의 데이터 경로, 로그 파일, 포트, 프로세스는 상호 간섭하지 않도록 엄격히 분리 유지합니다.
4. **수동 종목 보호 원칙**: `KRW-HOLO`는 어떠한 경우에도 자동 주문 또는 자산 평가에 포함되지 않아야 합니다.
5. **스레드 안전성 및 멱등성 준수**: 주문 저널 조작 시 `threading.Lock`/`RLock` 및 원자적 파일 쓰기/`.bak` 백업을 유지하며, 주문 요청 시 반드시 고유 식별자(`identifier`)를 사용합니다.
6. **단위 테스트 무결성 유지**: 작업 완료 후 반드시 `python -m unittest discover tests`를 실행하여 178개 이상의 모든 단위 테스트 통과를 검증합니다.





