# Bithumb AI Pro Quant Trading Bot (v4.0)

본 문서는 `c:\AI\bithumb` 디렉토리에 위치한 Bithumb 기반 AI 퀀트 트레이딩 봇의 프로젝트 설명 및 아키텍처 설계서입니다. 이 문서는 다른 AI 에이전트 또는 개발자가 프로젝트의 전반적인 구조와 핵심 로직을 빠르고 명확하게 파악할 수 있도록 작성되었습니다.

---

## 1. 프로젝트 개요

이 프로젝트는 빗썸(Bithumb) 거래소의 실시간 데이터와 Google Gemini AI를 결합하여, 유망한 단타/스윙 종목을 자동으로 탐색하고 매매를 수행하는 **AI 기반 퀀트 트레이딩 시스템**입니다. 

- **언어 및 환경**: Python 3, Windows 환경 (`.bat` 및 `.ps1` 스크립트 기반 구동)
- **핵심 기술**: Bithumb REST API & WebSocket, Google Gemini API (Flash 모델), Telegram API, Google Sheets API
- **주요 전략**: 
  - 다중 시간대(MTF) 분석 (1시간봉 추세 + 5분봉 정밀 타점)
  - 거래대금 및 모멘텀 기반 동적 시장 스크리닝 (자동 모드 지원)
  - 호가창 및 체결강도 기반 수급 분석
  - 볼린저 밴드, RSI, ATR 기반 변동성 및 리스크 관리 (동적 익절/손절선, 트레일링 스탑 적용)
  - 비트코인(BTC) 거시 환경 추적을 통한 폭락장 회피

---

## 2. 디렉토리 구조 및 주요 파일

프로젝트는 모듈화되어 있으며, 각 기능이 독립된 파일 및 디렉토리로 분리되어 있습니다.

```text
c:\AI\bithumb\
├── .env                  # 환경변수 파일 (API 키, 텔레그램 토큰, 설정값 등)
├── requirements.txt      # Python 의존성 패키지 목록
├── logs/                 # 일자별 트레이딩 및 시스템 로그 보관
├── data/                 # 로컬 데이터 (거래 내역 등) 임시/영구 저장
├── config/               # 설정 파일 폴더
│   └── service_account.json # Google Sheets 연동을 위한 GCP 서비스 계정 키
├── src/                  # 핵심 소스코드 디렉토리
│   ├── main.py                # 봇의 진입점(Entry Point), 스케줄러 및 메인 루프 실행
│   ├── bithumb_api.py         # 빗썸 REST API 호출 래퍼
│   ├── websocket_manager.py   # 빗썸 웹소켓 클라이언트 (실시간 시세/호가 수집)
│   ├── gemini_analyzer.py     # Gemini API를 활용한 퀀트 분석 및 매매 시그널 생성 엔진
│   ├── market_screener.py     # 조건(거래대금, 상승률 등)에 맞는 시장(코인) 동적 탐색
│   ├── trade_memory.py        # 트레이딩 기록 및 상태 메모리 관리
│   ├── telegram_alert.py      # 텔레그램 봇 API를 통한 실시간 알림 전송
│   ├── sheets_manager.py      # Google Sheets(gspread) 기반 매매 일지 자동 기록
│   ├── chart_renderer.py      # matplotlib를 활용한 차트 이미지 생성
│   ├── web_server.py          # 봇 상태 모니터링을 위한 웹 대시보드 서버
│   └── backtest.py            # 과거 데이터를 활용한 전략 백테스트 스크립트
└── *.bat, *.ps1          # 봇 실행, 중지, 재시작, 상태 확인 및 로그 조회를 위한 유틸리티 스크립트
```

---

## 3. 핵심 모듈 설계 및 역할

### 3.1. `main.py` (시스템 오케스트레이션)
- 전체 시스템의 **진입점**입니다. 
- `.env`에 정의된 각종 설정(마켓 모드, 리스크 파라미터 등)을 로드합니다.
- `APScheduler`를 사용하여 주기적(예: 5분마다)으로 트레이딩 로직을 실행합니다.
- 예외 처리, 로깅 설정, 글로벌 리스크 관리(일일 최대 손실 제한 등)를 담당합니다.
- 시스템 일시정지(`IS_BOT_PAUSED`) 상태 등을 관리합니다.

### 3.2. `market_screener.py` (동적 스크리닝)
- `MarketScreener` 클래스는 시장에서 유망한 종목을 실시간으로 걸러냅니다.
- **필터링 조건**: 최소 거래대금(예: 50억 원), 특정 범위의 당일 상승률(예: +1.5% ~ +15.0%).
- 상승 초입 구간(+2% ~ +8%)에 가중치를 부여하여 고점 물림을 방지하는 알고리즘을 포함합니다.

### 3.3. `gemini_analyzer.py` (AI 브레인)
- 봇의 **두뇌 역할**을 합니다. Google Gemini(주로 Flash 모델)에 시장 데이터를 프롬프트로 주입하여 분석 결과를 얻습니다.
- **분석 지표**: RSI, Bollinger Bands 등의 기술적 지표를 내부에서 계산합니다.
- 다중 시간대(MTF) 정렬, 호가창(매수/매도 잔량 비율), 비트코인 거시 추세 데이터를 종합합니다.
- 최종적으로 5대 정량적 매수 승인 체크리스트를 통과했을 때만 "BUY" 시그널을 발생시킵니다.
- 모델 Rate Limit 방지를 위해 쿨다운 캐싱 및 라우팅 로직이 구현되어 있습니다.

### 3.4. 데이터 수집 및 관리
- **`bithumb_api.py`**: 빗썸 Private/Public REST API 규격에 맞춰 서명(Signature)을 생성하고 주문, 잔고 조회, 과거 캔들 데이터를 가져옵니다.
- **`websocket_manager.py`**: 실시간 대응을 위해 웹소켓으로 현재가 및 호가창 데이터를 스트리밍 받아 최신 상태를 유지합니다.
- **`trade_memory.py`**: 진입가, 보유 수량, 목표가 등 현재 포지션 상태를 기록하고 추적합니다.

### 3.5. 모니터링 및 리포팅
- **`telegram_alert.py`**: 거래 진입/청산, 시스템 에러 발생 시 지정된 텔레그램 방으로 알림을 보냅니다.
- **`sheets_manager.py`**: 트레이딩 내역(수익률, 시간, 매수/매도 사유 등)을 구글 스프레드시트에 자동으로 기록하여 장기적인 성과 추적을 돕습니다.
- **`web_server.py`**: 로컬(또는 원격)에서 봇의 실시간 상태, 현재 포지션, 수익률을 시각적으로 확인할 수 있는 대시보드 웹페이지를 서빙합니다.
- **`chart_renderer.py`**: 매매 시점의 차트를 이미지로 렌더링하여 텔레그램이나 웹에 첨부할 수 있게 합니다.

---

## 4. 매매 파이프라인 (Trading Workflow)

1. **마켓 스크리닝 (Market Screener)**
   - 설정된 주기마다 `market_screener.py`가 전체 KRW 마켓 중 거래대금과 상승률이 유효한 타겟 코인을 선정합니다 (보유 중인 코인은 최우선 포함).
2. **데이터 수집 (Data Fetching)**
   - 타겟 코인들에 대한 과거 캔들 데이터(1시간봉, 5분봉 등)와 실시간 호가/체결 데이터를 REST API 및 WebSocket을 통해 수집합니다.
3. **AI 기반 분석 (Gemini Analyzer)**
   - 수집된 정량적 데이터(지표 포함)를 Prompt로 구성하여 Gemini API에 전송합니다.
   - 5개 핵심 퀀트 조건 중 4개 이상 충족 등 엄격한 필터를 통해 매수(BUY), 유지(HOLD), 매도(SELL) 판단을 받습니다.
4. **리스크 관리 및 주문 실행 (Risk Management & Order)**
   - BTC 급락 여부, 일일 최대 손실(Max Daily Loss) 등을 체크합니다.
   - 트레일링 스탑(Trailing Stop)을 적용하여 이익을 보존하면서 주문(Bithumb API)을 실행합니다.
5. **사후 처리 및 로깅 (Reporting)**
   - 체결 결과를 `trade_memory.py`에 업데이트합니다.
   - `telegram_alert.py`를 통해 사용자에게 알림을 발송하고, `sheets_manager.py`를 통해 구글 시트에 거래 로그를 기록합니다.

---

## 5. 실행 및 관리 스크립트

운영체제(Windows)에서 봇을 쉽게 관리할 수 있도록 스크립트를 제공합니다.

- `start_bot.bat` / `launcher.ps1`: 가상환경(venv)을 활성화하고 메인 프로세스를 백그라운드 또는 새로운 터미널에서 실행합니다.
- `stop_bot.bat` / `stop_bot.ps1`: 실행 중인 봇 프로세스를 안전하게 종료합니다.
- `status_bot.bat` / `status_bot.ps1`: 봇 프로세스가 현재 실행 중인지 확인합니다.
- `view_logs.bat`: 최근 로그(`logs/trading.log`)를 지속적으로 모니터링(tail) 합니다.

---

## 요약 (For AI Agents)

해당 프로젝트는 **Bithumb API, Gemini LLM 기반 퀀트 전략, 자동화된 리스크 관리 및 상태 모니터링(Telegram, Google Sheets, Web Dashboard)** 이 종합적으로 결합된 완성도 높은 트레이딩 시스템입니다. 수정 사항이나 새로운 전략을 추가할 때, 핵심 매매 로직은 `gemini_analyzer.py`와 `main.py`를, 데이터 파이프라인은 `bithumb_api.py` 및 `market_screener.py`를 주로 참고 및 수정하면 됩니다.
