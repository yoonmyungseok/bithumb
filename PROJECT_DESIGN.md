# Bithumb AI Pro Quant Trading Bot (v4.5)

본 문서는 `c:\AI\bithumb` 디렉토리에 위치한 Bithumb 기반 AI 퀀트 트레이딩 봇의 프로젝트 설명 및 아키텍처 설계서입니다. 이 문서는 다른 AI 에이전트 또는 개발자가 프로젝트의 전반적인 구조와 핵심 로직을 빠르고 명확하게 파악할 수 있도록 작성되었습니다.

> [!IMPORTANT]
> **개발 가이드라인**: 코드가 수정되거나 새로운 기능/모듈이 추가될 때마다 본 `PROJECT_DESIGN.md` 문서를 **반드시 함께 최신 상태로 갱신**해야 합니다.

---

## 1. 프로젝트 개요

이 프로젝트는 빗썸(Bithumb) 거래소의 실시간 데이터와 Google Gemini AI를 결합하여, 유망한 단타/스윙 종목을 자동으로 탐색하고 매매를 수행하는 **AI 기반 퀀트 트레이딩 시스템**입니다. 

- **언어 및 환경**: Python 3, Windows 환경 (`.bat` 및 `.ps1` 스크립트 기반 구동)
- **핵심 기술**: Bithumb REST API & WebSocket (v1/v2), Google Gemini API (Flash 모델군), Telegram API, Google Sheets API
- **주요 전략 및 아키텍처**: 
  - **다중 시간대(MTF) 분석**: 1시간봉 대세 추세 + 5분봉 정밀 타점 정렬
  - **거래대금 및 모멘텀 기반 동적 시장 스크리닝**: 24시간 거래대금 $\ge 10$억 원, 당일 상승률 +1.0%~+25.0%
  - **2중 호가 안전망 (Fail-Closed)**: 매수/매도 스프레드 $\le 0.35\%$, 상위 5호가 누적 매수 잔량 $\ge 2,000$만 원 검증
  - **결정론적 기술지표 엔진**: 볼린저 밴드(%B), RSI, ATR, MACD, EMA 기반 진입 게이트 (LLM 환각 차단)
  - **0.1초 초저지연 실시간 리스크 엔진**: WebSocket 틱 기반 0.1초 즉각 손절, 1차 50% 분할익절, 무한 트레일링 스탑
  - **자산 연동형 3단계 스마트 Auto-Scaling**: 계좌 총 자산 규모에 따른 보유 슬롯(2~4개) 및 비중(25~50%) 자동 전환
  - **장중 자금 입출금 자동 보정 (Cashflow Adjustment)**: 입출금 시 시작 기준자산을 자동 보정하여 순수 매매 수익률 보존
  - **통신 및 리소스 최적화**: HTTP Keep-Alive 커넥션 풀링(RTT 30% 단축), 슬롯 만석 시 AI 호출 생략(사이클 1초대), 텔레그램 알림 디바운싱
  - **주문 무결성 및 소유권 보호**: 멱등성 저널(`OrderJournal`), 봇 주문 식별(`is_managed_order`), 일원화 취소(`cancel_bot_open_orders`)

---

## 2. 디렉토리 구조 및 주요 파일

```text
c:\AI\bithumb\
├── .env                  # 환경변수 파일 (API 키, 텔레그램 토큰, 설정값 등)
├── requirements.txt      # Python 의존성 패키지 목록
├── PROJECT_DESIGN.md     # 프로젝트 아키텍처 및 시스템 설계서 (상시 최신 동기화)
├── logs/                 # 일자별 트레이딩 및 시스템 로그 보관 (30일 보존)
├── data/                 # 로컬 영구/상태 데이터 저장 폴더
│   ├── daily_stats.json       # 일일 손익 통계, 시작 기준자산 및 킬스위치 상태 (00:00 KST 자정 리셋)
│   ├── position_state.json    # 포지션별 최고가, 진입 시간 및 1차 익절 상태
│   ├── trade_memory.json      # 완료된 거래 내역 및 자가학습 피드백 메모리
│   ├── order_journal.json     # 멱등성 보장 주문 상태 추적 저널 (원자적 저장)
│   ├── cooldown_state.json    # 재진입 쿨다운 상태 영속화 (손절 45분, 익절 15분)
│   └── paper_account.json     # 모의투자(Paper Trading) 가상 원장 잔고
├── config/               # 설정 파일 폴더
│   └── service_account.json   # Google Sheets 연동을 위한 GCP 서비스 계정 키
├── src/                  # 핵심 소스코드 디렉토리
│   ├── main.py                     # 봇의 진입점(Entry Point), 스케줄러, 5분 트레이딩 사이클 오케스트레이터 (~450줄)
│   ├── risk_manager.py             # 일일 손익/입출금보정/킬스위치(`DailyRiskManager`), 포지션추적(`TrailingStopTracker`), 공포탐욕지수
│   ├── realtime_engine.py          # 0.1초 실시간 웹소켓 체결 틱 손절/익절 청산 엔진 (`RealtimeRiskEngine`), 미체결 정정/취소
│   ├── bot_controller.py           # 텔레그램 양방향 제어(/status, /panic, /pause 등), 웹 대시보드 API 데이터 공급 (`BotController`)
│   ├── bithumb_api.py              # 빗썸 REST API 클라이언트 (Keep-Alive 세션 풀, v1/v2 호환, JWT 인증, 자동 재시도)
│   ├── websocket_manager.py        # 빗썸 Public WebSocket 클라이언트 (0.1초 실시간 틱/호가/고래 체결 스트리밍, Heartbeat 안정화)
│   ├── private_websocket_manager.py# 빗썸 Private WebSocket 클라이언트 (MyOrder, MyAsset 체결 스트리밍)
│   ├── order_safety.py             # 주문 저널(`OrderJournal`), 멱등성 집행(`SafeOrderExecutor`), 리스크 검증(`RiskGuard`), 동적 티어
│   ├── strategy_engine.py          # 표준 기술지표(RSI, BB, ATR, MACD, EMA) 계산, 샹들리에 스탑, 결정론적 진입 게이트
│   ├── gemini_analyzer.py          # Gemini AI 퀀트 분석 및 시그널 생성 엔진 (안정형 모델 라우팅)
│   ├── market_screener.py          # 조건(거래대금, 상승률, 스프레드, 호가깊이) 시장 동적 탐색 (Fail-Closed)
│   ├── paper_broker.py             # 모의투자 어댑터 (공용 시세 기반 가상 체결 및 원장 관리)
│   ├── trade_memory.py             # 트레이딩 기록, 통계 및 AI 자가학습 메모리 관리
│   ├── telegram_alert.py           # 텔레그램 양방향 원격 제어, 디바운싱 알림, 차트 전송
│   ├── sheets_manager.py           # Google Sheets 기반 매매 일지, 대시보드 및 Strategy 탭 동기화/정리
│   ├── chart_renderer.py           # matplotlib 기반 매매 시점 캔들 차트 이미지 렌더링 (다크 테마)
│   ├── web_server.py               # 로컬 실시간 웹 대시보드 서버 (포트 7979, 주문저널/거래이력 실시간 서빙)
│   └── backtest.py                 # 과거 데이터 기반 퀀트 전략 백테스터 (Next-Bar Open, 인트라바 편향 제거, 페이징)
├── tests/                # 단위 테스트 디렉토리 (총 25개 테스트 스위트)
│   ├── test_market_screener.py
│   ├── test_order_safety.py
│   ├── test_paper_broker.py
│   ├── test_strategy_engine.py
│   └── test_realtime_risk.py
└── *.bat, *.ps1          # 봇 실행, 중지, 상태 확인, 로그 조회를 위한 스크립트
```

---

## 3. 핵심 모듈 설계 및 상세 동작 원리

### 3.1. `main.py` (시스템 오케스트레이션 & 진입점)
- **주기적 실행 (`run_cycle`)**: 5분마다 실행되며, 자산 티어 판정 $\rightarrow$ 스크리닝 $\rightarrow$ 정량 지표 및 AI 분석 $\rightarrow$ 리스크 가드 검증 $\rightarrow$ 안전 주문 집행을 오케스트레이션합니다.
- **슬림화된 아키텍처**: 기존 1,850줄의 방대한 코드를 전문 모듈(`risk_manager`, `realtime_engine`, `bot_controller`)로 분산하여 약 450줄의 고도로 정돈된 컨트롤러로 최적화되었습니다.

### 3.2. `risk_manager.py` (일일 손익, 자금 관리 및 트레일링 추적)
- **`DailyRiskManager`**: 일일 손익 계산, 매일 00:00 KST 리셋, 킬스위치, 장중 10,000원 이상 자금 입출금 자동 보정(Cashflow Adjustment), 연속 2회 손절 시 30분 쿨다운을 영속화합니다.
- **`TrailingStopTracker`**: 1차 +2.5% 분할익절 상태, 최고가 추적, 단계별 가속 트레일링 스탑(5% 돌파 시 0.8%, 10% 돌파 시 0.5% 초밀착) 및 60분 횡보 타임스탑 진입 시점을 관리합니다.
- **`get_fear_and_greed_index` / `calculate_total_equity`**: 크립토 공포/탐욕 지수 및 원화+코인 통합 자산 평가를 담당합니다.

### 3.3. `realtime_engine.py` (0.1초 실시간 웹소켓 청산 엔진)
- **`RealtimeRiskEngine`**: WebSocket 가격 틱 수신 시 **0.1초 만에 즉각 손절가 터치, 1차 50% 분할 익절, 가속 트레일링 스탑 청산**을 실행합니다.
- **`clean_stale_orders` / `requote_pending_orders`**: 3분 경과 미체결 봇 주문 자동 취소 및 최우선 호가 스마트 재정정을 담당합니다.

### 3.4. `bot_controller.py` (원격 제어 & 웹 대시보드 API)
- **`BotController`**: 텔레그램 `/status`, `/balance`, `/panic`, `/pause`, `/resume` 명령어 응답 및 웹 대시보드(포트 7979)의 실시간 상태 JSON 데이터를 빌드하여 서빙합니다.

### 3.5. `order_safety.py` (주문 안전성 & 리스크 가드)
- **`OrderJournal`**: 원자적 파일 쓰기(`write_json_atomically`)와 `threading.Lock`을 통해 크래시 복구 및 멀티스레드 안전성을 보장합니다. `is_managed_order()`를 통해 봇 주문과 사용자의 수동 주문을 완벽히 분리합니다.
- **`SafeOrderExecutor`**: 주문 전송 전 `client_order_id`를 저널에 기록하고, 네트워크 타임아웃 발생 시 상태를 `UNKNOWN`으로 보존하여 중복 주문을 차단합니다.
- **`RiskGuard`**: 1회 최대 주문액, 종목당 최대 비중, 총 투자 비중, 최대 동시 보유 포지션 수를 사전에 검증하며, `update_limits()`를 통해 동적 자산 티어와 실시간 동기화됩니다.

### 3.6. `strategy_engine.py` (결정론적 기술지표 엔진)
- RSI(Wilder's Smoothing), 볼린저 밴드(%B), ATR, MACD(9 EMA Signal), EMA 등 모든 기술적 지표 계산 로직을 단일화하여 제공합니다.
- **결정론적 진입 게이트 (`entry_signal`)**: MA5 > MA20, RSI 45~65, 볼린저 %B 0.35~0.75, 1시간봉 MTF 추세 일치, BTC 시장 레짐(`NORMAL / RISK_OFF / CRASH`)을 검증합니다.

### 3.7. `market_screener.py` (시장 동적 탐색 & 2중 호가벽)
- 빗썸 전체 KRW 마켓에서 거래대금 $\ge 10$억 원, 당일 상승률 +1.0%~+25.0% 필터를 적용합니다.
- **2중 호가 안전망 (Fail-Closed)**: 매수/매도 스프레드 $\le 0.35\%$, 상위 5호가 누적 매수 잔량 $\ge 2,000$만 원 검증.

---

## 4. 매매 파이프라인 (Trading Workflow)

```mermaid
graph TD
    A[0.1초 실시간 WebSocket 스트리밍] -->|시세 틱 수신| B[RealtimeRiskEngine 0.1초 즉시 청산]
    
    D[5분 주기 스케줄러 run_cycle] --> E[자산 티어 자동 판정 Auto-Scaling]
    E --> F{보유 슬롯 만석 여부}
    F -->|만석| G[신규 스크리닝 생략 & 보유 코인 감시 집중]
    F -->|여유 있음| H[MarketScreener 2중 호가벽 검증]
    H --> I[결정론적 진입 게이트 StrategyEngine]
    I --> J[Gemini AI 퀀트 분석 종합]
    J --> K[사전 리스크 가드 RiskGuard]
    K -->|승인 시| L[안전 주문 집행 SafeOrderExecutor]
    L --> M[주문 저널 기록 OrderJournal]
    M --> N[BotController 대시보드 / 텔레그램 / 시트 동기화]
```

---

## 5. 변경 이력 및 개선 히스토리 (Changelog)

| 버전 | 일자 | 주요 변경 및 최적화 내역 |
| :---: | :---: | :--- |
| **v4.5** | 2026-08-24 | • **단일 거대 파일(God Object) 모듈 분산 리팩토링** (`main.py` 1,850줄 ➜ `risk_manager.py`, `realtime_engine.py`, `bot_controller.py` 분리, `main.py` ~450줄 슬림화)<br>• **구글 시트 Strategy 탭 실시간 동기화 및 과거 비감시 종목 자동 정리 (`prune_unmonitored_strategies`)**<br>• **장중 자금 입출금 자동 감지 & 기준자산 자동 보정** (Cashflow Adjustment 도입)<br>• **자산 연동형 3단계 스마트 Auto-Scaling** (소액 2종목 $\rightarrow$ 100만 원 이상 4종목 자동 전환)<br>• **보유 슬롯 만석 시 AI 호출 생략 및 경량화** (사이클 속도 1초 미만 단축)<br>• **HTTP Keep-Alive 커넥션 풀링 적용** (`BithumbAPI` RTT 30~50% 단축)<br>• **텔레그램 반복 경보 디바운싱** (15분 중복 알림 방지)<br>• **웹소켓 Heartbeat 튜닝** (`ping_interval=30`, `ping_timeout=20`)<br>• **최소 거래대금 완화 (`10억 원`)** 및 2중 호가벽(스프레드 0.35% + 2천만 원 잔량) 연동 |
| **v4.0** | 2026-08-24 | • **0.1초 실시간 웹소켓 즉각 손절/익절 엔진** 탑재<br>• **결정론적 정량 진입 게이트 및 MTF 1시간봉 추세 필터** 추가<br>• **주문 저널(`OrderJournal`) 멱등성 및 봇 주문 소유권 식별** 구현<br>• **백테스터 Next-Bar Open 및 인트라바 편향 제거** 완료<br>• **웹 대시보드 (Trade Memory & Order Journal 연동)** 구현 |
| **v3.0** | 이전 버전 | • Gemini AI 분석 엔진 및 텔레그램 양방향 원격 제어 기초 구축 |

---

## 6. 유지보수 및 코드 수정 원칙 (For Developers & AI Agents)

1. **설계 문서 상시 동기화**: 코드 수정 또는 기능 추가 시 반드시 본 `PROJECT_DESIGN.md` 문서를 함께 최신 상태로 갱신합니다.
2. **지표 로직 단일화**: 기술 지표 및 손익비 공식 수정 시 `strategy_engine.py`를 단일 진실 공급원(SSOT)으로 사용합니다.
3. **단일 책임 원칙(SRP) 준수**: 모듈별 고유 책임(`risk_manager`, `realtime_engine`, `bot_controller`, `main`)을 분리하여 유지합니다.
4. **스레드 안전성 준수**: 주문 저널 및 공유 상태 조작 시 반드시 `threading.Lock` 및 원자적 파일 저장을 준수합니다.
5. **단위 테스트 무결성 유지**: 작업 완료 후 반드시 `python -m unittest discover tests`를 실행하여 25개 이상의 모든 단위 테스트 통과를 검증합니다.

