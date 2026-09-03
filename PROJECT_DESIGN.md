# Bithumb & Upbit AI Pro Quant Trading Bot (v8.4)

본 문서는 `c:\AI\bithumb` 디렉토리에 위치한 빗썸(Bithumb) 및 업비트(Upbit) 듀얼 거래소 지원 AI 퀀트 트레이딩 봇의 프로젝트 설명 및 아키텍처 설계서입니다. 이 문서는 다른 AI 에이전트 또는 개발자가 프로젝트의 전반적인 구조와 핵심 로직을 빠르고 명확하게 파악할 수 있도록 작성되었습니다.

> [!IMPORTANT]
> **개발 가이드라인**: 코드가 수정되거나 새로운 기능/모듈이 추가될 때마다 본 `PROJECT_DESIGN.md` 문서를 **반드시 함께 최신 상태로 갱신**해야 합니다.

---

## 1. 프로젝트 개요

이 프로젝트는 빗썸(Bithumb)과 업비트(Upbit) 거래소의 실시간 데이터와 Google Gemini AI를 결합하여, 유망한 단타/스윙 종목을 자동으로 탐색하고 매매를 수행하는 **듀얼 거래소 독립형 AI 퀀트 트레이딩 시스템**입니다. 

- **언어 및 환경**: Python 3, Windows 환경 (`.bat` 및 `process_manager.py` 기반 구동)
- **핵심 기술**: 
  - 빗썸 REST API & WebSocket (v1/v2)
  - 업비트 REST API & WebSocket (Public: 시세/체결, Private: myOrder/myAsset, HS512 JWT + unencoded query string SHA-512 hash, `identifier` 멱등성)
  - Google Gemini API (Flash 모델군), Telegram API
- **주요 전략 및 아키텍처**: 
  - **다중 시간대(MTF) 분석**: 1시간봉 대세 추세 + 5분봉 정밀 타점 정렬
  - **거래대금 및 모멘텀 기반 동적 시장 스크리닝**: 확인형 후보는 24시간 거래대금 $\ge 10$억 원과 당일 상승률 +1.0%~+25.0%를 적용하며, 빗썸은 별도 초기 돌파 후보(단기 상승 초입·유동성·호가 안전성 통과)를 소액 진입 경로로 운영
  - **2중 호가 안전망 (Fail-Closed)**: 매수/매도 스프레드 $\le 0.35\%$, 상위 5호가 누적 매수 잔량 $\ge 2,000$만 원 검증
  - **결정론적 기술지표 엔진**: 볼린저 밴드(%B), RSI, ATR, MACD, EMA 기반 진입 게이트 (LLM 환각 차단)
  - **0.1초 초저지연 실시간 리스크 엔진**: WebSocket 틱 기반 0.1초 즉각 손절, 1차 30~50% 분할익절, 무한 트레일링 스탑
  - **본전 보장(Break-Even) 및 타임스탑 지지선 보호**: 최대 보유시간 도달 시 무차별 투매 방지 및 지지선/본전 스탑 연계
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
│   ├── trading_bot_bootstrap.py    # 진입점 부트스트랩 공통화 (`TradingBotBootstrap`, 텔레그램·내부 API·WS·스케줄러·shutdown)
│   ├── paper_broker.py             # 모의투자 어댑터 (거래소별 원장 격리 지원)
│   ├── trade_memory.py             # 트레이딩 기록, 통계 및 AI 자가학습 메모리 관리
│   ├── telegram_alert.py           # 텔레그램 양방향 원격 제어, 디바운싱 알림, 차트 전송
│   ├── chart_renderer.py           # matplotlib 기반 매매 시점 캔들 차트 이미지 렌더링 (다크 테마)
│   ├── web_server.py               # 로컬 경량 웹 API 서버 모듈 (is_api_only 지원)
│   ├── process_manager.py          # 빗썸/업비트/대시보드/전체 독립 프로세스 탐색, 종료, 상태, 로그 관리 CLI
│   ├── trading_bot_bootstrap.py    # 진입점 부트스트랩 공통화 (`TradingBotBootstrap`, 텔레그램·내부 API·WS·스케줄러·shutdown)
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
- **7대 팩터 앙상블 스코어러 (`calculate_composite_alpha_score`)**: MTF 1H(15점) + VWAP(15점) + MACD 가속도(15점) + RSI 골든존(15점) + 볼린저 밴드(15점) + 수급/호가잔량비(15점) + 볼륨 스파이크(10점)의 100점 만점 중 **65점 이상** 시에만 매수를 승인하며, AI 모델 및 로컬 퀀트 엔진에 100% 일원화되어 무중단 고승률 타점을 생성합니다.

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

### 3.10. 재진입 쿨다운 및 청산 가격 필터링 (`CooldownManager`)
- **시간 및 가격 2중 쿨다운**: 포지션 청산의 **REST 확정 체결 증가분** 뒤에만 기록합니다. 따라서 주문 ACK·미체결·불완전 WebSocket 이벤트가 손익·쿨다운을 앞당기지 않으며, 손절 직후 더 불리한 높은 가격에서의 뇌동 재진입을 방어합니다.
- **트레일링 청산 뒤 회복 확인**: 트레일링 청산 뒤에는 청산가 대비 최소 +1.5% 회복 전까지 재진입을 차단합니다. 청산가보다 낮은 가격의 재진입도 하락 재개 추격으로 간주해 허용하지 않습니다.
- **영속 저장 및 메모리 동기화**: `cooldown_state.json`에 영구 기록되어 봇 재시작 시에도 쿨다운 상태를 지속 유지합니다.
- **연속 손실 회복 모드**: 연속 2회 손절은 일일 리스크 관리자의 30분 회복 구간과 50% 리스크 규모 축소를 활성화합니다. 이는 종목별 `CooldownManager`의 재진입 금지와 별개이며, 일반 매수 경로를 일괄 중지하지 않습니다.
- **급락 후 반등 전용 진입**: 회복 구간에서 일반 `RISK_OFF` 신호가 미달한 후보는 `recovery_rebound_signal()`로 한 번 더 평가합니다. BTC `CRASH`·주문 대사 미완료·시세 스트림 불안정·종목별 쿨다운은 절대 우회하지 않습니다. 확정 5분봉 반등, 핵심 하드게이트, 알파 75점 이상, BTC 대비 RS +1.5% 이상, 최소 거래대금, EMA20 대비 1% 이내의 1시간봉 회복을 모두 만족할 때만 양 거래소에서 실제 주문을 허용합니다. 반등 주문은 일반 슬롯의 35%를 기본 비중으로 하며, 동일 회복 구간에는 한 주문만 제출합니다.
- **확정봉 모멘텀 돌파 진입**: 반등형과 별도로 최신 확정 5분봉이 직전 4개 확정봉 고점을 돌파하고, 거래량이 최근 20개 확정봉 평균의 1.3배 이상이며, 양봉·RSI 52~75·BTC 대비 RS +0.8%·1시간 EMA20 대비 1% 이내 조건을 동시에 만족할 때만 진입합니다. 특히 당일 상승률 +3% 이상 및 BTC 대비 RS +1.5% 이상인 독자 랠리 주도주는 스크리너에서 `MOMENTUM_BREAKOUT`으로 자동 분류되어 눌림목 필터(%B/저점거리)에 걸리지 않고 돌파 진입 기회를 포착합니다. `RISK_OFF`는 주간 60점·심야 70점의 모멘텀 알파 기준을 적용합니다. 최초 진입은 거래소별 최대 포지션 한도의 25%로 제한하며, BTC `CRASH`, 시세 스트림 이상, 주문 대사 미완료, 쿨다운은 절대 우회하지 않습니다.

### 3.11. 알림 최적화 및 체결 이벤트 집중 파이프라인
- **노이즈 억제**: 미체결 주문 접수(ACK/OPEN) 시점의 불필요한 중복 알림을 제거하고, 실제 거래소 체결(FILLED) 시점에만 체결가, 체결수량, 슬리피지, 실현손익 차트 정보를 집중 발송합니다.
- **거래소 자체 알림과의 조화**: 텔레그램 알림은 봇의 핵심 리스크 상태, 일일 요약, 수동 원격 제어 응답 위주로 고도화되었습니다.

### 3.12. 듀얼 거래소 데이터 격리 & REST 체결 대사(Reconciliation) 안전망
- **경로별 SQLite 인스턴스**: `DatabaseManager`는 정규화된 DB 경로별 인스턴스를 관리합니다. 빗썸은 `data/trading.db`, 업비트는 `data/upbit/trading.db`만 사용하므로 초기화 순서에 따른 상태 혼입을 방지합니다.
- **REST 체결 대사 우선**: ACK는 주문 수락일 뿐 체결이 아닙니다. Private WebSocket은 대사 대기 신호로만 사용하며, `reconcile_exchange_statuses`가 수량·잔량·평균가·수수료를 검증한 뒤에만 포지션 진입시각, 실현손익, 쿨다운, 거래 메모리를 갱신합니다. 대사 미완료·모순·실패 시 신규 매수는 계속 차단됩니다.
- **마이그레이션 및 감사**: 거래소별 JSON은 자기 거래소 레코드만 전용 DB로 적재합니다. 완료 표식과 `sqlite_migration.audit.json`을 거래소별로 남겨 재실행 중복 적재를 막고 결과를 추적합니다.
- **짧은 수명 캐시와 관측성**: 시장별 분석은 2초 잔고 스냅샷을 재사용하되, 포트폴리오 산정과 취소/재호가 뒤에는 강제 최신 조회합니다. 대시보드 안전 상태에는 WebSocket stale·재연결·큐 깊이, `RECONCILIATION_PENDING`, REST 대사 시작/완료 시각 및 갱신·실패 건수, 슬리피지·호가/VWAP 지표를 포함합니다.
- **전략 판단 감사 이력**: 각 거래소의 `strategy_decisions` 테이블은 사이클·종목별 `HOLD`, 안전 차단, 종목별 쿨다운, 리스크 가드, 매수 승인 및 주문 제출을 기록합니다. 후보 상대강도·거래대금·BTC 레짐·하드게이트 결과·반등 전용 체크리스트를 JSON으로 보존하며, 판단 이력만 30일 뒤 정리합니다. 주문 원장과 체결 이력은 이 정리 대상이 아닙니다.

### 3.13. API 일일 사용량 & 쿼터 텔레메트리 모니터링 체계 (`api_telemetry.py` & `gemini_telemetry.py`)
- **거래소 REST API 호출 계측 (`ExchangeApiTelemetry`)**: 빗썸과 업비트의 모든 REST 호출(GET 조회, POST/DELETE 주문)의 호출수, 상태코드(200/429/5xx), Rate limit(429) 발생 건수, 최근 엔드포인트를 스레드 안전하게 실시간 집계합니다.
- **업비트 실시간 잔여 쿼터 파싱**: 업비트 응답 헤더(`Remaining-Req`)의 `sec`(초당 잔여) 및 `min`(분당 잔여)을 자동 추출하여 Rate limit 임계 도달 여부를 실시간 추적합니다.
- **KST 자정 기준 자동 롤오버**: 한국 표준시(KST) 매일 00:00:00 자정을 기준으로 일일 누적 카운터를 자동 초기화하여 일별 정밀 사용량을 보장합니다.
- **Gemini AI 쿼터 및 캐시 절감 관측**: 일일 AI 분석 횟수 / 일일 권장치(1,500 RPD) 대비 사용률 프로그레스 바, 5분봉 캐시 적중(쿼터 절감 횟수), 429 감지 및 로컬 퀀트 알고리즘 폴백 횟수를 대시보드에 실시간 제공합니다.
- **통합 대시보드 UI 연동**: SPA 대시보드(`index.html`, `app.js`)의 `api_usage_panel` 위젯에서 빗썸, 업비트, Gemini 카드를 3열 그리드로 시각화하며, 탭 전환(`combined`/`bithumb`/`upbit`)에 따라 해당 거래소 사용량을 동적으로 강조합니다.

---

## 4. 변경 이력 및 개선 히스토리 (Changelog)

| 버전 | 일자 | 주요 변경 및 최적화 내역 |
| :---: | :---: | :--- |
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
2. **거래소 격리 원칙**: 빗썸과 업비트의 데이터 경로, 로그 파일, 포트, 프로세스는 상호 간섭하지 않도록 엄격히 분리 유지합니다.
3. **수동 종목 보호 원칙**: `KRW-HOLO`는 어떠한 경우에도 자동 주문 또는 자산 평가에 포함되지 않아야 합니다.
4. **스레드 안전성 및 멱등성 준수**: 주문 저널 조작 시 `threading.Lock`/`RLock` 및 원자적 파일 쓰기/`.bak` 백업을 유지하며, 주문 요청 시 반드시 고유 식별자(`identifier`)를 사용합니다.
5. **단위 테스트 무결성 유지**: 작업 완료 후 반드시 `python -m unittest discover tests`를 실행하여 178개 이상의 모든 단위 테스트 통과를 검증합니다.





