# 아키텍처 및 거래소 경계

## 시스템 구성

Python과 Windows 배치·`process_manager.py` 기반의 듀얼 거래소 자동매매 시스템이다. 빗썸과 업비트는 REST API, Public·Private WebSocket, Gemini·Groq Provider, 데이터 경로, 로그, 내부 API 포트를 독립적으로 사용하고 통합 대시보드만 표시 목적으로 집계한다.

| 구성 | 빗썸 | 업비트 | 공통·통합 |
| --- | --- | --- | --- |
| 진입점 | `src/main.py` | `src/main_upbit.py` | `trading_runtime.py`, `trading_bot_bootstrap.py` |
| 통신 | `bithumb_api.py`, WebSocket 매니저 | `upbit_api.py`, Public·Private WS | `base_websocket.py` |
| 상태 | `data/` | `data/upbit/` | 거래소 범위 원장·`state_store.py` |
| 로그·내부 API | `logs/trading.log`, 17979 | `logs/trading_upbit.log`, 17980 | `dashboard_server.py`, 7979 |

## 핵심 흐름

1. 거래소별 REST·Public WebSocket에서 시세·호가를 수집하고 Private 이벤트는 bounded queue에 적재한다.
2. `TradingCycleEngine`이 REST 대사, 포트폴리오, 레짐, 스크리닝, 진입·청산을 공통 오케스트레이션으로 실행한다. 거래소 차이는 profile로만 주입한다.
3. `StrategyPolicy`가 확정봉 기반 후보를 평가하고, `order_safety/`가 저널·멱등 주문·체결 증가분·쿨다운·호가 영향을 처리한다.
4. 각 거래소 상태 API는 독립 제공하며 통합 대시보드는 표시용으로만 합산한다.

`KRW-HOLO`는 업비트의 수동 관리 종목으로 환경 설정, 스크리닝, 매수, 주문, 자산 평가, 실시간 청산과 긴급 매도의 자동 경로에서 제외한다.
