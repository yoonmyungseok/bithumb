# Codex 프로젝트 규칙: Bithumb & Upbit AI Quant Trading Bot

## 적용 범위와 우선순위

- 이 파일은 저장소 전체에 적용되는 공통 작업 규칙이자 규칙 문서의 진입점이다.
- 사용자 요청, 보안 규칙, 실행 환경 제약이 이 문서와 충돌하면 더 높은 우선순위를 따른다.
- 더 하위 디렉터리에 `AGENTS.md`가 있으면 해당 경로에서는 루트 규칙과 하위 규칙을 함께 적용한다.
- 세부 규칙은 `docs/agent-rules/`가 단일 원본이다. 이 파일에는 어떤 작업에서도 누락되면 안 되는 최소 안전 규칙만 유지한다.

## 모든 작업의 필수 규칙

- 모든 설명, 질문, 코드 주석, 문서는 한국어로 작성한다.
- 기존 사용자 변경을 보존하고, 요청 범위를 벗어난 리팩터링이나 외부 API·URL·환경 변수·데이터 경로·주문 형식·대시보드 응답 계약 변경을 하지 않는다.
- 빗썸과 업비트의 키, API 클라이언트, 포트, 주문 상태, 데이터, 로그, 거래 메모리를 절대로 혼합하지 않는다.
- 주문 ACK·OPEN·Private WebSocket 이벤트만으로 체결·포지션·손익·쿨다운·거래 메모리를 갱신하지 않는다. REST 대사로 확정하기 전까지 신규 BUY는 fail-closed로 차단한다.
- API 키, 시크릿, 토큰, 계좌·주문 식별자를 코드, 문서, 테스트, 로그, 응답에 노출하지 않는다.
- 전략, 안전 정책, 데이터 흐름 또는 운영 절차를 변경하면 `PROJECT_DESIGN.md` 인덱스와 관련 `docs/project-design/` 문서, `changelog.md`를 같은 변경에서 갱신한다.

## 작업별 필독 규칙

| 작업 유형 | 반드시 확인할 문서 |
| --- | --- |
| 주문, 체결, 잔고, WebSocket, 재조정, 리스크 변경 | [trading-safety.md](docs/agent-rules/trading-safety.md) |
| 거래소별 키, Provider, 환경 변수, 포트, 데이터·로그 경로 변경 | [exchange-isolation.md](docs/agent-rules/exchange-isolation.md) |
| 진입·청산·필터·임계값·비중·레짐·AI 판단 변경 | [strategy-change.md](docs/agent-rules/strategy-change.md), [전략 및 리스크](docs/project-design/strategy-and-risk.md) |
| 대시보드, UI 표시, 운영 로그·상태 분석 변경 | [dashboard-operations.md](docs/agent-rules/dashboard-operations.md) |
| 기능 구현 후 테스트, 문서화, 최종 보고 | [testing-reporting.md](docs/agent-rules/testing-reporting.md) |

## 문서 책임

- `PROJECT_DESIGN.md`: 기존 참조를 보존하는 설계 문서 인덱스다. 실제 구현 구조, 정책, 데이터 흐름, 운영 절차와 변경 이력의 기준 문서는 `docs/project-design/`에 있다.
- `.cursor/rules/bithumb-upbit-trading-safety.mdc`: Cursor 자동 적용용 최소 안전 규칙과 이 문서의 진입점이다.
- `.agents/rules/00-core-safety.md`: Antigravity 워크스페이스 전용 규칙의 진입점이다. Antigravity Rules 화면에서 `Always On`으로 설정한다.
