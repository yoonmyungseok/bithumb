# 주문, 체결 및 안전성 설계

## 확정 체결 경계

ACK, `OPEN`, `PARTIALLY_FILLED`, Private WebSocket 이벤트는 체결 확정이 아니다. REST 주문 대사에서 체결 수량·잔량·평균가·수수료를 검증한 뒤에만 포지션, 손익, 보유 시간, 쿨다운, 거래 메모리를 갱신한다. `OrderJournal`과 `OrderFillProcessor`는 확정 체결 증가분만 반영해 중복 이벤트를 방지하고, 업비트 주문은 고유 `identifier`를 사용한다.

## 신규 BUY 안전 게이트

`evaluate_pre_buy_submit_gate()`는 주문 직전에 최신가, 시세 WebSocket 상태, 대사 대기, 미해결 주문, 쿨다운, 호가 영향 조건을 재확인한다. 조회 실패, 0 이하 가격, `RECONCILIATION_PENDING`, 모순 상태, 호가 잔량 부족, 과도한 슬리피지는 신규 BUY를 차단한다. ACK 뒤 `AckReconcileScheduler`는 중복 주문 없이 단건 REST 대사만 예약한다. 기존 포지션의 보호 청산은 계속 수행한다.

## 연결·성과

WebSocket은 상태머신, bounded queue, ping timeout, 지수 백오프를 사용한다. 실제 종료 시 재연결·재구독과 REST 주문 대사를 모두 확인하며, 큐 포화와 대사 실패는 신규 BUY를 fail-closed로 전환한다. `confirmed_fill_performance.py`는 확정 체결 필드만 읽어 거래소별 성과를 read-only 재집계한다.

## 빗썸 API 인증 및 서버 시간 오프셋 동기화

빗썸 API 2.0은 Private API 호출 시 JWT 페이로드에 밀리초 단위 `timestamp`를 요구하며, 서버 시각과 약 20초 이상 차이 발생 시 `expired_jwt` (401)를 반환한다.
- **서버 시간 오프셋 자동 보정:** 모든 HTTP 응답의 `Date` 헤더(RFC 2822)로부터 `server_ts - local_ts` 오프셋을 추출하여 클래스 공용 상태로 유지하고, JWT 생성 시 이를 반영한다.
- **`expired_jwt` 안전 복구:** 401 `expired_jwt` 감지 시 즉시 서버 시각 오프셋을 재동기화한다. 멱등성이 보장되는 조회(`GET`) 요청은 새 토큰으로 1회 자동 재시도하며, 중복 체결 위험이 있는 주문(`POST`) 요청은 재시도하지 않고 fail-closed로 차단한다.
- **Private WebSocket 공유:** `BithumbPrivateWebSocketClient`도 동일한 서버 시간 오프셋을 반영하여 핸드셰이크 토큰을 생성한다.

상세 작업 규칙은 [주문·체결 안전 규칙](../agent-rules/trading-safety.md)을 따른다.
