# 운영 및 관측성 설계

빗썸, 업비트, 통합 대시보드는 독립 프로세스로 구동한다. 통합 대시보드는 표시 목적으로만 합산하며 거래소별 탭과 내부 API 범위는 유지한다. 민감한 키, 시크릿, 토큰, 계좌·주문 식별자는 API·로그·대시보드에 노출하지 않는다.

- API·Gemini 텔레메트리는 거래소별 호출량, 쿼터, 실패, 폴백, 캐시 적중을 분리해 기록한다.
- 사이클 관측성은 REST 대사, 포트폴리오, 후보 탐색, 캔들 조회, AI 분석, 주문 처리의 지연을 집계한다.
- Provider 키 누락·AI 실패·스키마 실패는 신규 BUY만 차단하고, 기존 포지션 보호와 REST 대사는 계속한다.
- Public 가격 스트림 복구만으로 Private 주문·체결 정상화를 판단하지 않는다. 재연결 뒤 구독 상태와 REST 대사를 확인한다.
- 워치독은 거래소별 하트비트, stale 상태, crash-loop를 감시한다. 재시작과 실거래 활성화는 문서 변경과 별도의 운영 승인 사항이다.
- 대시보드 UI는 데스크톱 고밀도 다단 테이블과 스마트폰(sm 미만) 전용 반응형 카드 뷰(Card layout & Accordion)를 하이브리드로 지원한다. 모바일 환경에서 긴급 전량 매도 오터치를 방지하기 위해 터치 타겟과 간격을 분리하며, TradingView 차트 모달과 Canvas 성능 차트는 화면 회전(resize)에 즉시 반응하도록 설계되었다.
- 런타임 설정 매니저(`CommonConfigManager`)는 `MAX_ALT_ALLOC_PCT`뿐 아니라 `MIN_ALT_ALLOC_PCT`도 지원하여 대시보드 및 내부 API를 통해 알트코인 최소 비중 하한선을 실시간 동적으로 조정 및 관측할 수 있다.
- 09:00 KST 일일 모닝 리포트(`execute_daily_morning_report_shared`)는 기존 잔고·당일 손익 외에 직전 24시간 롤링 확정 체결 퀀트 성과(`get_24h_quant_summary`)를 통합 제공한다. 거래소 격리를 엄격히 유지하며, 승률·실현손익·차감 수수료·수수료 잠식률(Fee Erosion)·Whipsaw 손절 내역을 포맷팅하여 모바일 텔레그램으로 전달한다. 예외 발생 시에도 기본 계좌 알림은 Fail-Safe로 보장된다.
- 독립 리포팅 스크립트(`scripts/daily_performance_report.py`)를 통해 대화창이나 터미널에서 언제든 직전 24시간 양 거래소 비교 정밀 마크다운 보고서(`reports/daily_quant_report_YYYYMMDD.md`)를 즉시 생성·보관할 수 있다.

상세 작업 규칙은 [대시보드·운영 규칙](../agent-rules/dashboard-operations.md)을 따른다.

## 설계 문서 동반 갱신 검사

GitHub Actions는 `tools/check_design_docs.py`를 실행한다. `src/` 운영 코드가 변경되면 `changelog.md`와 최소 한 개의 설계 문서가 필요하며, 전략·주문 안전·운영·아키텍처 책임 경로는 각각 대응하는 설계 문서도 함께 변경되어야 한다. 테스트 파일만 변경한 경우에는 이 검사를 요구하지 않는다.
