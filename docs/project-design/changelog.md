# 변경 이력

버전별 상세 근거는 관련 커밋과 설계 문서를 함께 확인한다. 이후 변경은 관련 설계 문서 갱신과 동시에 맨 위에 추가한다.

## v8.99 (2026-09-18)

- 전일 매매 실데이터 분석 기반 3대 전략·리스크 안전성 정밀 강화 (승률-손익 역전 해소 및 조기 청산 방지):
  - **포지션 사이징 균등화 및 RISK_OFF 하드 캡**:
    - `strategy_engine.py`: `StrategyPolicy.RISK_OFF_MAX_ALT_ALLOC_PCT` (6.0%), `RISK_OFF_MAX_ALT_BUDGET_KRW` (최대 75,000 KRW), `RISK_OFF_MIN_ORDERBOOK_RATIO` (1.00) 정책 상수 신설.
    - `trading_runtime.py`: 약세장(`RISK_OFF`) 시 알트코인 포지션 배분 한도를 최대 6.0% 및 최대 75,000원으로 강제 클램핑. 평상시 알트 최소 10% 하한선 강제를 약세장에서 제외하여 소액/분할 진입을 보장하고, 승률이 높아도 손실 종목에 과다 금액이 투입되어 계좌가 역전되는 현상 원천 차단.
  - **스윙 전략 진입·청산 룰 안전 버퍼 동기화**:
    - `strategy_engine.py`: `SWING_ENTRY_EMA20_BUFFER_RATIO`를 기존 1.000에서 1.005(+0.5% 상단 지지)로 상향. 청산 기준(0.985, -1.5%)과의 간격을 최소 2.0% 확보.
    - `market_screener.py`: 스윙 후보 스크리닝(`screen_swing_candidates`) 시점에 4H 확정봉 지지 여부를 사전 평가하여 4H EMA20 미달 종목을 조기 탈락.
    - `trading_runtime.py`: 스윙 진입 승인 시 목표가/손절가/배분비중 및 지지 확인 상세 내역을 명시적으로 로깅.
  - **약세장(RISK_OFF) AI 단독 승인 안전 가드 강화**:
    - `trading_runtime.py`: 로컬 관망 후 AI 단독 자율 승인 시, `RISK_OFF` 환경에서는 반등 확정(`rebound_confirmed == True`) 및 호가 잔량비(`orderbook_ratio >= 1.00`)를 필수 통과 조건으로 강제. 떨어지는 칼날 잡기와 매도 압박 구간 진입 차단.
    - `strategy_engine.py`: 호가 잔량비 점수(`score_orderflow`) 구간을 세분화하여 1.0 미만 매도벽 우세 종목에 대한 감점 반영.
  - **체결 처리기(fill_processor) 멱등적 후속 대사 경고 노이즈 억제**:
    - `src/order_safety/fill_processor.py`: 이미 `FILLED` 완료된 주문에 대해 직후 도착한 REST 대사의 체결 증가분이 0인 경우, 기존에는 불필요한 `WARNING`("체결 증가분 없이 FILLED 상태를 거부했습니다")이 발생하여 콘솔(StreamHandler)에 출력되던 문제를 개선. 이미 완료된 주문의 후속 대사는 `DEBUG` 레벨로 정상 처리하여 커맨드 창 노이즈를 제거하고, 미체결 주문에 대한 가짜 체결 승격 방어는 완벽히 유지.
  - `tests/test_trade_improvement_guards.py`, `tests/test_storage_and_fill_boundaries.py`: 단위 테스트 추가 및 검증 완료.

## v8.98 (2026-09-18)

- 스윙 전략(SWING) 4H EMA20 추세 지지 진입 게이트 신설 및 진입 직후 조기 청산(자가당착) 방지:
  - `strategy_engine.py`: `StrategyPolicy.SWING_ENTRY_EMA20_BUFFER_RATIO` (기본 1.000, 100% 이상 지지) 및 `SWING_TREND_EXIT_BUFFER_RATIO` (기본 0.985) SSOT 상수 정의.
  - `strategy_engine.py`: `evaluate_swing_trend_entry` 함수 신설. 4시간봉 확정 EMA20 상단 안착 여부를 사전 검증하여, 현재가가 4H EMA20 미달인 역배열/하락추세 종목의 스윙 진입을 원천 차단.
  - `trading_runtime.py`:
    - 1차 게이트(`entry_gating`): 스윙 후보 종목에 대해 4H EMA20 지지선 미달 시 조기 관망(`HOLD`) 처리하여 불필요한 AI 호출 및 휩소 진입 방지.
    - 2차 게이트(`AI Direct Entry`): 로컬 관망 후 AI 단독 자율 승인 시에도 스윙 후보는 4H EMA20 지지 검증을 통과해야만 진입 허용.
    - 3차/4차 방어선: 스윙 전략 목표가/손절가 할당 직전 및 주문 실행(`process_buy_execution`) 직전 4H 추세 미달 시 fail-closed 차단.
  - `gemini_analyzer.py`: 스윙(SWING) 경로 프롬프트 지침에 4H EMA20 상단 지지 필수 원칙 및 역배열 종목 BUY 금지 지침 동기화.
  - `tests/test_swing_4h_safety.py`, `tests/test_swing_strategy.py`, `tests/test_trading_runtime.py`: 스윙 진입 게이트 통과/차단 단위 및 통합 테스트 추가 완료.

## v8.97 (2026-09-17)

- 투 트랙(Two-Track) 퀀트 분석 체계 구축 및 09:00 텔레그램 모닝 리포트 24시간 결산 연동:
  - `confirmed_fill_performance.py`: 직전 24시간 롤링 확정 체결 퀀트 성과 집계 함수(`get_24h_quant_summary`) 및 텔레그램 HTML 포맷팅 함수(`format_24h_quant_telegram_block`) 추가. 거래소 격리를 준수하여 지정 거래소 DB만 조회하며, 승률·손익·수수료·수수료 잠식률(Fee Erosion)·MDD·Whipsaw 손절 내역을 산출.
  - `trading_bot_bootstrap.py`: `execute_daily_morning_report_shared`에 24시간 롤링 퀀트 결산 블록 연동. 퀀트 모듈 오류 시에도 기본 잔고/손익 리포트가 정상 발송되는 Fail-Safe 보장.
  - `scripts/daily_performance_report.py`: 대화창 및 터미널용 24시간 롤링 양 거래소 비교 정밀 보고서 생성기 추가 (`reports/daily_quant_report_YYYYMMDD.md` 자동 저장).
  - 외부 AI API 장애나 Rate Limit과 무관하게 100% 무결점 파이썬 퀀트 계산 엔진 기반으로 신뢰성 확보.

## v8.96 (2026-09-17)

- 당일 손절 2회 누적 차단 정책 완화 (자정 고정 차단 ➔ 3시간 쿨다운 완화 및 상태 파일 mtime 자동 동기화):
  - `strategy_engine.py`: `StrategyPolicy.COOLDOWN_DAILY_LOSS_LIMIT_SEC` (기본 10,800.0초 / 3시간, 환경변수 연동) 및 `MAX_DAILY_LOSSES_PER_MARKET` SSOT 정의.
  - `cooldown.py`: 당일 2회 이상 손절 발생 시 자정까지 무조건 전면 차단하던 경직성을 3시간 쿨다운 만료 후 재진입 허용으로 완화. 직전 청산가 대비 떨어지는 칼날 잡기 방지(-1.5% 급락 차단) 및 갭 필터는 계속 엄격하게 유지. 상태 파일 mtime 감지 로직 추가로 외부 상태 편집 시 프로세스 재시작 없는 핫 리로드 지원.
  - `risk_off_loss_reentry.py`: 당일 2회 이상 손실(Soft Blacklist) 차단 기간을 동일하게 3시간 쿨다운 기준으로 일원화하여 모듈 간 정합성 유지.
  - `main.py`, `main_upbit.py`, `trading_runtime.py`: `daily_loss_cooldown` SSOT 파라미터 주입 및 쿨다운 만료 기반 재진입 검증 연동.

## v8.95 (2026-09-17)

- 빗썸(00:00 KST)과 업비트(09:00 KST) 일봉 리셋 시차 및 스크리너 완충(Reset Grace Period) 정책 도입:
  - `market_screener.py`: 거래소별 리셋 직후 30분(업비트 09:00~09:30, 빗썸 00:00~00:30) 동안 `min_change_rate`를 50% 수준(최소 +0.2% 이상)으로 완화하는 완충 세션(`is_reset_grace_period`, `get_effective_change_rate_thresholds`) 도입으로 개장 직후 후보 기근 방어.
  - 리셋 직후 10% 이상 급등한 개장 펌핑 잡코인에 대해 과열 피로도 감점(`momentum_multiplier = 0.3`) 및 `EXTENDED` 단계 지정으로 뇌동 추격 매수 차단.
  - `gemini_analyzer.py`: AI 프롬프트의 `24h변동` 오표기를 `당일변동({reset_desc} 리셋)`으로 정정하고 거래소별 리셋 기준을 명시하여 LLM의 추세 오판 방지.
  - `strategy_engine.py` & `docs/project-design/strategy-and-risk.md`: 빗썸(00,04,08,12,16,20시)과 업비트(01,05,09,13,17,21시) 간 4시간봉(240분봉) 1시간 위상차 특성 및 확정봉 기반 안전성 명문화.
  - `trading_bot_bootstrap.py` & `bot_controller.py`: 09:00 KST 일일 모닝 리포트 및 대화형 명령어(`/status`, `/trades`) 메시지에 `손익 집계 기준: KST 자정(00:00) 리셋` 문구를 명시하여 업비트 앱(09:00 리셋)과의 혼동 방지.

## v8.94 (2026-09-17)

- 주문 즉시 단건 REST 대사(Fast Reconciliation) 및 종목별 차단 격리(Per-Market Isolation) 구현:
  - `AckReconcileScheduler.trigger_async` 추가: 주문 접수(ACK) 및 Private WebSocket `trade`/`done` 알림 직후 0.5~1.5초 내에 비동기 백그라운드 단건 REST 대사를 자동 실행하여 체결 대사 지연을 기존 최대 300초(5분)에서 1~2초 이내로 단축.
  - `OrderJournal`: `is_system_entry_ready()`, `get_entry_blocking_markets()` 추가로 시스템 전역 정지와 단일 종목 대사 대기 격리.
  - `trading_runtime.py`: 단일 종목 대사 중에도 시스템 레벨이 정상이면 AI 후보 랭킹 및 AI 예산 배분을 차단하지 않고 타 종목 매수 기회 보존.
  - `bot_controller.py` & `dashboard_server.py`: `entry_blocking_markets`를 `safety` 페이로드에 연동.
  - `dashboard/src/app.js`: 대사 대기 중인 종목만 황색 `[체결 대사 대기]`로 표기하고, 무관한 타 후보 종목은 녹색 `[진입 검토 가능]` 또는 `[전략 관망]`으로 정상 판정.

## v8.93 (2026-09-17)

- 알트코인 단일 종목 최소 진입 비중(`MIN_ALT_ALLOC_PCT`) 상향 조정:
  - 수수료 대비 실익 확보를 위해 알트코인 최소 비중 하한선을 5%(약 6만원)에서 **10%(약 12만원)**로 상향 (`DEFAULT_ALT_ALLOC_PCT` 12%, `NIGHT_SESSION_MAX_ALLOC_PCT` 10% 일치 동기화).
  - 미세 변동 시 수수료 역마진(왕복 0.1%~0.5%) 및 극소액 푼돈 진입 방지.
  - `runtime_config.py` 스키마 및 `bot_controller.py` 핫리로드 주입 지원, 대시보드 설정 UI 필드 연동.

## v8.92 (2026-09-17)

- 빗썸 API 서버 시간 오프셋 자동 보정 및 `expired_jwt` 복구 패치:
  - HTTP 응답의 `Date` 헤더(RFC 2822) 기반 로컬 시계 드리프트(Time Drift) 오프셋 자동 계산 및 공용 상태 동기화.
  - `BithumbAPI._generate_jwt_token` 및 `BithumbPrivateWebSocketClient`의 JWT `timestamp` 생성 시 서버 시각 오프셋 보정 적용.
  - HTTP 401 `expired_jwt` 수신 시 서버 시각 즉시 재동기화 및 멱등성이 보장되는 조회(`GET`) 요청에 한해 1회 안전 자동 재시도 적용 (`POST` 주문은 중복 방지를 위해 fail-closed 유지).

## v8.91 (2026-09-17)

- 대시보드 모바일 UI/UX 전면 최적화:
  - 데스크톱 고밀도 테이블 보존 및 스마트폰(640px 미만) 전용 하이브리드 반응형 카드 뷰(Card layout & Accordion) 지원.
  - 전략 탭 바 및 설정 모달 탭의 가로 스크롤 스와이프 지원 (`no-scrollbar`).
  - 테이블 컨테이너의 모바일 고정 max-height 축소로 모바일 스크롤 트랩 해소.
  - 상단 헤더의 거래소 전환 및 퀵 액션 버튼의 모바일 flex-wrap 간격 조정 및 긴급 전량 매도 버튼 오터치 방지 간격 확보.
  - TradingView 인터랙티브 차트 모달의 모바일 높이 동적 가변화 및 Canvas 14일 자산 추이 차트의 화면 회전(`resize`, `orientationchange`) 즉시 리드로우 핸들러 추가.

## v8.90 ~ v8.88 (2026-09-16)

- v8.90: 레거시 `.agentrules`를 제거하고 Codex·Cursor·Antigravity 규칙 경로를 정리했다. 거래 코드와 실거래 상태는 변경하지 않았다.
- v8.89: 도구별 규칙 진입점과 `docs/agent-rules/` 단일 원본 구조를 정합화했다.
- v8.88: 작업 규칙을 기능별 문서로 분리하고 루트 `AGENTS.md`를 라우터로 정리했다.

## v8.87 ~ v8.50 (2026-09-03 ~ 2026-09-15)

- 사이클·Gemini 장애 관측성, 확정 체결 성과 리포트, 주문 제출 직전 안전 게이트와 ACK 후 REST 대사 예약을 추가했다.
- 거래소별 Gemini 키·모델·거시 브리핑 폴백, 런타임 설정·포지션 슬롯 핫 리로드, WebSocket 보호와 공통 런타임·부트스트랩·워치독 profile 분리를 개선했다.

## v8.4 ~ v7.0 (2026-08-30 ~ 2026-09-02)

- 주문 저널, REST 대사, 확정 체결 증가분, 거래소별 경로와 제외 종목 보호를 보존하며 공통 실행 엔진을 정비했다.
- 확정봉 모멘텀, 호가 영향 검증, 재진입 쿨다운, 회복 반등, 전략 판단 이력, 거래소별 SQLite 격리를 추가했다.

## v6.3 ~ v4.0 (2026-08-24 ~ 2026-08-25)

- `StrategyPolicy` 기반 실거래·백테스트 SSOT, 확정봉 진입, 하드 안전 게이트와 알파 팩터를 정립했다.
- 체결 슬리피지·호가 정정, 트레일링·타임스탑·일일 손실 제한, 원자적 상태 저장, 워치독, 업비트 듀얼 거래소 지원을 구축했다.

## 문서 분리 (2026-09-16)

- 기존 단일 문서의 아키텍처, 전략·리스크, 주문·체결 안전, 운영·관측성 설명을 주제별 문서로 분리했다.
- 루트 `PROJECT_DESIGN.md` 경로는 인덱스로 유지했다. 코드·환경 변수·주문 API·대시보드 응답 계약·실거래 실행 구성은 변경하지 않았다.
- `tools/check_design_docs.py`와 GitHub Actions 검사를 추가해 운영 코드 변경에 관련 설계 문서와 변경 이력이 빠지면 CI가 실패하도록 했다.
