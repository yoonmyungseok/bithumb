# Bithumb & Upbit AI Pro Quant Trading Bot (v5.0)

본 문서는 `c:\AI\bithumb` 디렉토리에 위치한 빗썸(Bithumb) 및 업비트(Upbit) 듀얼 거래소 지원 AI 퀀트 트레이딩 봇의 프로젝트 설명 및 아키텍처 설계서입니다. 이 문서는 다른 AI 에이전트 또는 개발자가 프로젝트의 전반적인 구조와 핵심 로직을 빠르고 명확하게 파악할 수 있도록 작성되었습니다.

> [!IMPORTANT]
> **개발 가이드라인**: 코드가 수정되거나 새로운 기능/모듈이 추가될 때마다 본 `PROJECT_DESIGN.md` 문서를 **반드시 함께 최신 상태로 갱신**해야 합니다.

---

## 1. 프로젝트 개요

이 프로젝트는 빗썸(Bithumb)과 업비트(Upbit) 거래소의 실시간 데이터와 Google Gemini AI를 결합하여, 유망한 단타/스윙 종목을 자동으로 탐색하고 매매를 수행하는 **듀얼 거래소 독립형 AI 퀀트 트레이딩 시스템**입니다. 

- **언어 및 환경**: Python 3, Windows 환경 (`.bat` 및 `.ps1` 스크립트 기반 구동)
- **핵심 기술**: 
  - 빗썸 REST API & WebSocket (v1/v2)
  - 업비트 REST API & WebSocket (Public: 시세/체결, Private: myOrder/myAsset, HS512 JWT + unencoded query string SHA-512 hash, `identifier` 멱등성)
  - Google Gemini API (Flash 모델군), Telegram API, Google Sheets API
- **주요 전략 및 아키텍처**: 
  - **다중 시간대(MTF) 분석**: 1시간봉 대세 추세 + 5분봉 정밀 타점 정렬
  - **거래대금 및 모멘텀 기반 동적 시장 스크리닝**: 24시간 거래대금 $\ge 10$억 원, 당일 상승률 +1.0%~+25.0%
  - **2중 호가 안전망 (Fail-Closed)**: 매수/매도 스프레드 $\le 0.35\%$, 상위 5호가 누적 매수 잔량 $\ge 2,000$만 원 검증
  - **결정론적 기술지표 엔진**: 볼린저 밴드(%B), RSI, ATR, MACD, EMA 기반 진입 게이트 (LLM 환각 차단)
  - **0.1초 초저지연 실시간 리스크 엔진**: WebSocket 틱 기반 0.1초 즉각 손절, 1차 50% 분할익절, 무한 트레일링 스탑
  - **자산 연동형 3단계 스마트 Auto-Scaling**: 계좌 총 자산 규모에 따른 보유 슬롯(2~4개) 및 비중(25~50%) 자동 전환
  - **장중 자금 입출금 자동 보정 (Cashflow Adjustment)**: 입출금 시 시작 기준자산을 자동 보정하여 순수 매매 수익률 보존
  - **완전한 거래소 물리적/논리적 격리**: 빗썸과 업비트의 환경변수, 데이터 디렉터리(`data/upbit/*`), 로그(`logs/trading_upbit.log`), 대시보드 포트(`7979` vs `7980`), 구글 시트, 실행 스크립트 분리
  - **7중 KRW-HOLO 수동 종목 절대 보호망**: 업비트 `KRW-HOLO`는 스크리닝, 주문, 긴급매도, 자산평가, 실시간 청산, 시트, 대시보드에서 100% 영구 제외

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
│   ├── bithumb/ (또는 data/)  # 빗썸 데이터 (daily_stats, order_journal, trade_memory 등)
│   └── upbit/                 # 업비트 전용 격리 데이터 폴더
│       ├── daily_stats.json       # 업비트 일일 손익 통계 및 킬스위치 상태
│       ├── position_state.json    # 업비트 포지션별 최고가 및 1차 익절 상태
│       ├── trade_memory.json      # 업비트 거래 내역 및 자가학습 메모리
│       ├── order_journal.json     # 업비트 멱등성 보장 주문 상태 저널
│       ├── cooldown_state.json    # 업비트 재진입 쿨다운 상태
│       └── paper_account.json     # 업비트 모의투자 가상 원장
├── config/               # 설정 파일 폴더
│   └── service_account.json   # Google Sheets 연동을 위한 GCP 서비스 계정 키
├── src/                  # 핵심 소스코드 디렉토리
│   ├── main.py                     # 빗썸 진입점 및 5분 사이클 오케스트레이터
│   ├── main_upbit.py               # 업비트 진입점 및 5분 사이클 오케스트레이터
│   ├── upbit_api.py                # 업비트 REST API 클라이언트 (HS512 JWT, query_hash, identifier)
│   ├── upbit_websocket.py          # 업비트 Public WebSocket 클라이언트 (0.1초 실시간 틱/호가/고래 체결)
│   ├── upbit_private_websocket.py  # 업비트 Private WebSocket 클라이언트 (myOrder, myAsset)
│   ├── bithumb_api.py              # 빗썸 REST API 클라이언트
│   ├── websocket_manager.py        # 빗썸 Public WebSocket 클라이언트
│   ├── private_websocket_manager.py# 빗썸 Private WebSocket 클라이언트
│   ├── risk_manager.py             # 일일 손익/입출금보정/킬스위치(`DailyRiskManager`), 포지션추적(`TrailingStopTracker`), 자산평가
│   ├── realtime_engine.py          # 0.1초 실시간 웹소켓 체결 틱 손절/익절 청산 엔진 (`RealtimeRiskEngine`), 미체결 정정/취소
│   ├── bot_controller.py           # 텔레그램 양방향 제어, 웹 대시보드 API 공급자 (거래소별 독립 인스턴스)
│   ├── order_safety.py             # 주문 저널(`OrderJournal`), 멱등성 집행(`SafeOrderExecutor`), 리스크 검증(`RiskGuard`)
│   ├── strategy_engine.py          # 표준 기술지표(RSI, BB, ATR, MACD, EMA) 계산, 결정론적 진입 게이트
│   ├── gemini_analyzer.py          # Gemini AI 퀀트 분석 및 시그널 생성 엔진
│   ├── market_screener.py          # 조건(거래대금, 상승률, 스프레드, 호가깊이) 시장 동적 탐색 (Fail-Closed, HOLO 제외)
│   ├── paper_broker.py             # 모의투자 어댑터 (거래소별 원장 격리 지원)
│   ├── trade_memory.py             # 트레이딩 기록, 통계 및 AI 자가학습 메모리 관리
│   ├── telegram_alert.py           # 텔레그램 양방향 원격 제어, 디바운싱 알림, 차트 전송
│   ├── sheets_manager.py           # Google Sheets 기반 매매 일지, 대시보드 및 Strategy 탭 동기화 (거래소 태깅)
│   ├── chart_renderer.py           # matplotlib 기반 매매 시점 캔들 차트 이미지 렌더링 (다크 테마)
│   ├── web_server.py               # 로컬 실시간 웹 대시보드 서버 (포트 7979/7980, 주문저널/거래이력 실시간 서빙)
│   ├── process_manager.py          # 빗썸/업비트 독립 프로세스 탐색, 종료, 상태, 로그 관리
│   ├── watchdog.py                 # 빗썸 워치독 프로세스
│   └── watchdog_upbit.py           # 업비트 워치독 프로세스
├── tests/                # 단위 테스트 디렉토리 (총 49개 테스트 스위트)
│   ├── test_upbit_api.py           # 업비트 API JWT 인증, SHA-512 query_hash, 호가단위, identifier 검증
│   ├── test_upbit_holo_guard.py    # KRW-HOLO 7중 방어선 (자산평가, 주문, 청산, 긴급매도, 시트 배제) 검증
│   ├── test_exchange_isolation.py  # 빗썸/업비트 데이터 및 프로세스 완전 분리 검증
│   ├── test_market_screener.py
│   ├── test_order_safety.py
│   ├── test_paper_broker.py
│   ├── test_strategy_engine.py
│   ├── test_realtime_risk.py
│   └── test_startup_integration.py
└── *.bat                 # 빗썸 및 업비트 실행, 중지, 재시작, 상태, 로그 조회 배치 스크립트
    ├── start_bot.bat / start_upbit_bot.bat (콘솔 창 실행)
    ├── start_bot_background.bat / start_upbit_bot_background.bat (완전 무창 백그라운드 24시간 가동)
    ├── stop_bot.bat / stop_upbit_bot.bat
    ├── restart_bot.bat / restart_upbit_bot.bat
    ├── status_bot.bat / status_upbit_bot.bat
    └── view_logs.bat / view_upbit_logs.bat
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
- **Private WebSocket (`UpbitPrivateWebSocketClient`)**: `wss://api.upbit.com/websocket/v1/private`에 JWT Authorization 헤더로 연결되어 `myOrder`(체결 이벤트) 및 `myAsset`(자산 변동)을 실시간 수신하고 `OrderJournal`에 즉각 반영합니다.

### 3.3. 7중 KRW-HOLO (홀로월드에이아이) 수동 종목 절대 보호망
사용자가 수동 매매하는 `KRW-HOLO`는 어떤 상황에서도 자동매매 시스템이 개입하지 못하도록 7중 방어선으로 완벽히 격리됩니다.
1. **환경설정/상수 기본값**: `UPBIT_EXCLUDED_MARKETS=KRW-HOLO` 기본값 강제 적용.
2. **시장 스크리닝**: `MarketScreener`가 시장 탐색 시 HOLO를 후보군에서 원천 제외.
3. **사전 리스크 가드**: `RiskGuard.validate_buy`에서 HOLO 매수 시도를 즉각 거부.
4. **안전 주문 집행기**: `SafeOrderExecutor.submit` 및 `UpbitAPI.create_order`에서 HOLO 주문 시 즉각 `ValueError` 발생.
5. **총 자산 및 보유목록 평가**: `calculate_total_equity`, `get_held_markets`, `build_positions_data`에서 계좌에 HOLO가 존재해도 평가금액을 0원으로 처리하고 목록에서 100% 제외.
6. **실시간 청산 및 긴급 전량매도 (Panic Sell)**: `RealtimeRiskEngine`의 틱 청산 및 `BotController.execute_panic_sell` 실행 시 HOLO는 매도 대상에서 영구 제외되어 사용자 수동 물량을 완벽히 보존.
7. **구글 시트 및 웹 대시보드**: Dashboard, Performance, Strategy, Trade_Log 및 웹 UI 어디에도 HOLO가 노출되거나 기록되지 않음.

### 3.4. 프로세스 및 실행 스크립트 완전 분리
- `src/process_manager.py`는 PowerShell을 통해 빗썸(`main.py`)과 업비트(`main_upbit.py`) 프로세스를 구분하여 PID를 추출합니다.
- `stop_upbit_bot.bat`을 실행해도 빗썸 봇은 정상 동작을 유지하며, 반대로 `stop_bot.bat`을 실행해도 업비트 봇은 간섭받지 않습니다.
- 빗썸 대시보드는 `http://localhost:7979`, 업비트 대시보드는 `http://localhost:7980`으로 동시 운영 가능합니다.

---

## 4. 변경 이력 및 개선 히스토리 (Changelog)

| 버전 | 일자 | 주요 변경 및 최적화 내역 |
| :---: | :---: | :--- |
| **v5.0** | 2026-08-25 | • **업비트(Upbit) API 기반 자동매매 시스템 신규 구축 및 듀얼 거래소 완전 분리 완료**<br>• **업비트 전용 REST API (`UpbitAPI`) & Public/Private WebSocket 클라이언트 탑재** (HS512 JWT, unencoded query string SHA-512 hash, `identifier` 멱등성 보장)<br>• **7중 KRW-HOLO(홀로월드에이아이) 수동 종목 절대 보호망 구축** (스크리닝, 주문, 청산, Panic Sell, 자산평가, 시트, 대시보드 배제)<br>• **거래소별 물리적 환경 분리** (`.env.upbit`, `data/upbit/*`, `logs/trading_upbit.log`, 웹 대시보드 포트 `7980`, 업비트 배치 스크립트 5종)<br>• **독립 프로세스 매니저 및 워치독 구축** (`process_manager.py` 듀얼 지원, `watchdog_upbit.py`)<br>• **총 49개 단위 테스트 스위트 구축 및 100% 통과 검증 완료** |
| **v4.5** | 2026-08-24 | • 웹 대시보드 최근 거래 및 주문 저널 최신순 정렬 최적화<br>• 모듈 분산 리팩토링 (`risk_manager.py`, `realtime_engine.py`, `bot_controller.py` 분리)<br>• 구글 시트 Strategy 탭 실시간 동기화 및 장중 자금 입출금 자동 보정(Cashflow Adjustment) |
| **v4.0** | 2026-08-24 | • 0.1초 실시간 웹소켓 즉각 손절/익절 엔진 탑재<br>• 결정론적 정량 진입 게이트 및 주문 저널(`OrderJournal`) 멱등성 구현 |

---

## 5. 유지보수 및 코드 수정 원칙 (For Developers & AI Agents)

1. **설계 문서 상시 동기화**: 코드 수정 또는 기능 추가 시 반드시 본 `PROJECT_DESIGN.md` 문서를 함께 최신 상태로 갱신합니다.
2. **거래소 격리 원칙**: 빗썸과 업비트의 데이터 경로, 로그 파일, 포트, 프로세스는 상호 간섭하지 않도록 엄격히 분리 유지합니다.
3. **수동 종목 보호 원칙**: `KRW-HOLO`는 어떠한 경우에도 자동 주문 또는 자산 평가에 포함되지 않아야 합니다.
4. **스레드 안전성 및 멱등성 준수**: 주문 저널 조작 시 `threading.Lock` 및 원자적 파일 쓰기를 유지하며, 주문 요청 시 반드시 고유 식별자(`identifier`)를 사용합니다.
5. **단위 테스트 무결성 유지**: 작업 완료 후 반드시 `python -m unittest discover tests`를 실행하여 49개 이상의 모든 단위 테스트 통과를 검증합니다.
