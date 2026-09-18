# Bithumb & Upbit AI Pro Quant Trading Bot (v9.00)

빗썸과 업비트를 엄격히 분리해 운영하는 AI 퀀트 트레이딩 시스템의 설계 문서 진입점입니다. 기존 참조 호환성을 위해 이 경로는 유지하며, 실제 설계의 단일 원본은 아래 하위 문서입니다.

> [!IMPORTANT]
> 코드·전략·안전 정책·데이터 흐름·운영 절차를 변경할 때는 관련 설계 문서와 [변경 이력](docs/project-design/changelog.md)을 같은 변경에서 갱신해야 합니다. 정책 수치는 코드의 `StrategyPolicy`와 실제 실행 경로가 단일 기준입니다.

## 설계 문서 목차

| 문서 | 책임 | 갱신 시점 |
| --- | --- | --- |
| [아키텍처](docs/project-design/architecture.md) | 디렉터리, 모듈, 거래소별 프로세스·데이터 경계 | 모듈·경로·구동 흐름 변경 |
| [전략 및 리스크](docs/project-design/strategy-and-risk.md) | 진입·청산·레짐·비중·쿨다운·백테스트 | 전략·임계값·AI 판단·리스크 변경 |
| [주문 및 안전성](docs/project-design/execution-and-safety.md) | 주문 저널, 확정 체결, REST 대사, WebSocket | 주문·체결·재조정·포지션 보호 변경 |
| [운영 및 관측성](docs/project-design/operations-and-observability.md) | 대시보드, 알림, 텔레메트리, 워치독 | 운영 절차·로그·관측 변경 |
| [변경 이력](docs/project-design/changelog.md) | 버전별 구현·안전 영향·검증 | 모든 기능 변경 |

## 문서 경계

- `docs/agent-rules/`는 작업 규칙의 단일 원본이고, 이 문서 묶음은 실제 구현 구조와 동작 흐름을 설명한다.
- 빗썸과 업비트의 키, API 클라이언트, 포트, 주문 상태, 데이터, 로그, 거래 메모리는 혼합하지 않는다.
- 주문 ACK, `OPEN`, Private WebSocket 이벤트는 체결 확정이 아니다. REST 대사 전 신규 BUY는 fail-closed로 차단하며 기존 포지션 보호는 유지한다.
