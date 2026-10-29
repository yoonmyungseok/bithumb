# 아키텍처 및 거래소 경계

## 시스템 구성

Python과 Windows 배치·`process_manager.py` 기반의 듀얼 거래소 자동매매 시스템이다. 빗썸과 업비트는 REST API, Public·Private WebSocket, Gemini·Groq Provider, 데이터 경로, 로그, 내부 API 포트를 독립적으로 사용하고 통합 대시보드만 표시 목적으로 집계한다.

| 구성 | 빗썸 | 업비트 | 공통·통합 |
| --- | --- | --- | --- |
| 진입점 | `src/main.py` | `src/main_upbit.py` | `trading_runtime.py`, `trading_bot_bootstrap.py` |
| 통신 | `bithumb_api.py`, WebSocket 매니저 | `upbit_api.py`, Public·Private WS | `base_websocket.py` |
| 상태 | `data/` | `data/upbit/` | 거래소 범위 원장·`state_store.py` |
| 로그·내부 API | `logs/trading.log`, 17979 | `logs/trading_upbit.log`, 17980 | `dashboard_server.py`, 7979 |
| 리포팅/동기화 | - | - | `src/sheets/` (`sync_worker.py`), 구글 스프레드시트 5분 동기화 |

## 핵심 흐름

1. 거래소별 REST·Public WebSocket에서 시세·호가를 수집하고 Private 이벤트는 bounded queue에 적재한다.
2. `TradingCycleEngine`이 REST 대사, 포트폴리오, 레짐, 스크리닝, 진입·청산을 공통 오케스트레이션으로 실행한다. 거래소 차이는 profile로만 주입한다.
3. `StrategyPolicy`가 확정봉 기반 후보를 평가하고 알트코인 포지션 사이징 하한(`MIN_ALT_ALLOC_PCT`) 균등화를 적용하며, `order_safety/`가 저널·멱등 주문·체결 증가분·쿨다운·호가 영향을 처리한다.
4. 각 거래소 상태 API는 독립 제공하며 통합 대시보드는 표시용으로만 합산한다.
5. `src/sheets/sync_worker.py`는 5분 주기로 빗썸·업비트의 봇 미관리(수동 매수) 종목을 자동 수집하여 단일 구글 스프레드시트에 종합 Summary 탭 및 종목별 개별 탭(`UPBIT_BTC`, `BITHUMB_ETH` 등)으로 시세와 손익 히스토리를 갱신한다. Gemini 웹 Workspace 연동 시 즉각 분석 질의가 가능하다.
6. `src/analysis_24h_generator.py`는 매일 09:00 KST에 `TradingBotBootstrap` 스케줄러에 의해 자동 실행되며, 거래소별 수동 매수 종목(봇 관리 포지션 제외)에 대해 실시간 시세와 캔들 히스토리를 주입하고 Groq LLM을 통해 24시간 분석 마크다운 보고서를 자동 생성하여 `24h/{SYMBOL}_24h_{YYYYMMDD}.md`로 저장한다. 빗썸 봇은 빗썸 종목, 업비트 봇은 업비트 종목만 담당하여 거래소 격리를 유지한다.

`KRW-HOLO`는 업비트의 수동 관리 종목으로 환경 설정, 스크리닝, 매수, 주문, 자산 평가, 실시간 청산과 긴급 매도의 자동 경로에서 제외한다.

