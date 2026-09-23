# 변경 이력

버전별 상세 근거는 관련 커밋과 설계 문서를 함께 확인한다. 이후 변경은 관련 설계 문서 갱신과 동시에 맨 위에 추가한다.

## v9.09 (2026-09-24)

- **Gemini AI API 호출량 폭증 방어 및 1단계 즉각 안정화 구현**:
  - `src/gemini_analyzer.py`:
    - **실패 응답 네거티브 캐싱 (Negative Caching - 300초)**: 구글 서버 503/Timeout 등으로 실패(fail-closed) 시, 해당 결과를 300초(5분, 1개 사이클) 동안 캐싱하여 동일 사이클 내 반복 호출 폭풍 차단.
    - `evaluate_holding_position` 및 `rank_candidate_markets`에도 예외 발생 시 300초 네거티브 캐시를 적용하여 에러 시 재호출 낭비 방지.
  - `src/ai_provider.py`:
    - **서버 에러(500/502/503/504) 및 타임아웃 단기 모델 쿨다운(120초)**: 구글 서버 장애 시 120초간 해당 모델 쿨다운을 부여하여, 한 사이클 내 10개 종목 전체가 503을 맞으며 차순위 모델로 연쇄 폴백(호출수 2배 가속)하는 현상 원천 차단.
  - `src/strategy_engine.py`:
    - `StrategyPolicy.AI_DIRECT_ENTRY_MIN_ALPHA` 상수(기본 65점) 및 `get_ai_direct_entry_min_alpha()` 헬퍼 추가.
  - `src/trading_runtime.py`:
    - 관망 종목의 AI Direct Entry 품질 게이트 기준을 기존 50점에서 **65점**으로 상향하여 무의미한 관망 종목에 대한 AI 호출 60% 이상 절감.
    - `EntryGatingResult` 조기 반환(`should_continue=True`) 시 `called_ai` 플래그 누락 버그 수정: AI가 HOLD/PAUSE를 반환하더라도 `ai_budget_remaining`이 정상 차감되도록 보장하여, 사이클당 신규 AI 분석 상한(기본 2개)이 엄격히 준수되도록 수정.
    - `MAX_AI_CALLS_PER_CYCLE` 환경변수 호환 지원.
  - `tests/test_gemini_call_reduction.py`:
    - 네거티브 캐싱 재호출 방지, 503/Timeout 쿨다운 등록, 최소 알파 기준(65점) 및 called_ai 플래그 보존 단위 테스트 추가 및 전원 통과 검증.
  - `docs/project-design/strategy-and-risk.md`:
    - Gemini 호출량 절감 및 네거티브 캐싱 설계 정책 문서 동기화.

## v9.08 (2026-09-23)

- **개미털기(SHAKEOUT_SWEEP) 유동성 스윕 역매수 및 휩쏘 방어·스마트 재진입 구현**:
  - `src/strategy_engine.py`:
    - `StrategyPolicy`에 `SHAKEOUT_SWEEP` 전용 파라미터 신설 (`SHAKEOUT_SWEEP_ENABLED`, `SHAKEOUT_SWEEP_LOOKBACK_BARS: 12`, `SHAKEOUT_SWEEP_MIN_LOWER_SHADOW_RATIO: 0.50`, `SHAKEOUT_SWEEP_MAX_UPPER_SHADOW_RATIO: 0.25`, `SHAKEOUT_SWEEP_VOLUME_RATIO_MIN: 1.3`, `SHAKEOUT_SWEEP_RECLAIM_BUFFER_RATIO: 1.001`, `SHAKEOUT_SWEEP_ALLOC_RATIO: 0.70`, `SHAKEOUT_SWEEP_TARGET_PCT: 0.040`, `SHAKEOUT_SWEEP_TIME_STOP_SECONDS: 2700`).
    - `evaluate_shakeout_sweep_setup()` 구현: 전저점 스윕(스탑 헌팅) 이탈 여부, 종가 전저점 재탈환(Reclaim), 아랫꼬리 50% 이상, 윗꼬리 25% 이하, 거래량 1.3배 이상 폭증, RSI 30.0 이상, %B 0.10 이상 정량 검증.
    - `evaluate_entry_rules()` 연동: `entry_type="SHAKEOUT_SWEEP"` 경로 지원 및 스윕 저점 기준 초타이트 손절가(-1.0%~-2.0%) 및 목표가(+4.0% 이상, 손익비 1:2.0 이상) 산출.
    - `get_shakeout_sweep_alpha_threshold()`: 레짐별 알파 기준(BULL 50점, NORMAL 55점, RISK_OFF 60점, 심야 +5점) SSOT 함수 제공.
  - `src/market_screener.py`:
    - 스크리너에서 당일 변동률 -5.0% ~ +0.5% 내외에서 급락 후 반등 셋업을 보이는 유망 알트코인을 탐지하여 `candidate_type="SHAKEOUT_SWEEP"`으로 분류 및 사이클 우선 배정.
  - `src/order_safety/cooldown.py`:
    - `CooldownManager.check_reentry_allowed()`에 `allow_shakeout_reclaim` 옵션 추가: 손절 1회 후 쿨다운 중이더라도 현재가가 직전 손절가 이상으로 복귀하여 스윕 신호가 확인되면 쿨다운을 유예하고 즉시 본전 회복 재진입 허용 (당일 손절 2회 누적 시에는 일일 한도 엄격 차단 유지).
  - `src/trading_runtime.py`:
    - `SHAKEOUT_SWEEP` 진입 타입 처리: 비중(70% 캡), 45분 타임스탑, 대시보드 상태 표시에 `SHAKEOUT_SWEEP` 경로 정보 노출.
    - `is_in_cooldown` Mock 객체 및 튜플 언패킹 방어 코드 적용.
  - `src/gemini_analyzer.py` & `src/ai_provider.py`:
    - Gemini AI 매수 분석 시스템 프롬프트 및 정책 지침에 `SHAKEOUT_SWEEP` 경로 규정 동기화.
  - `tests/test_shakeout_sweep.py`:
    - 스윕 캔들 통과, 장대 음봉(떨어지는 칼날) 차단, BTC CRASH 차단, 스마트 재진입 쿨다운 바이패스 단위 테스트 작성 및 전원 통과 검증.
  - `docs/project-design/strategy-and-risk.md`:
    - 개미털기 유동성 스윕 역매수 및 스마트 재진입 설계 정책 문서 갱신.

## v9.07 (2026-09-23)

- **강세장(BULL_TREND) 손익비 정상화 및 알트코인 휩쏘(Whipsaw) 조기 손절 방지 체계 개편**:
  - `src/strategy_engine.py`:
    - `StrategyPolicy.BULL_STOP_LOSS_PCT`: `0.020` (-2.0%) $\rightarrow$ `0.035` (-3.5%)로 완화하여 강세장 특유의 일시적 -2~3% 눌림목(롱 스퀴즈) 조기 털림 방지.
    - `StrategyPolicy.BULL_PARTIAL_TP_1_PCT`: `0.030` (+3.0%) $\rightarrow$ `0.050` (+5.0%)로 상향.
    - `StrategyPolicy.BULL_PARTIAL_TP_2_PCT`: `0.060` (+6.0%) $\rightarrow$ `0.100` (+10.0%)로 상향하여 대세 상승 추세 수익 극대화.
    - `StrategyPolicy.BULL_TRAILING_START_PCT`: `0.030` (+3.0%) $\rightarrow$ `0.050` (+5.0%), `StrategyPolicy.BULL_TRAILING_DROP_PCT`: `0.015` $\rightarrow$ `0.025` (2.5%)로 버퍼 확장.
    - `StrategyPolicy.BULL_TIME_STOP_SECONDS`: 7,200초 (2시간) $\rightarrow$ `14,400초` (4시간), `StrategyPolicy.BULL_TIME_STOP_MAX_HOLD_SECONDS`: 10,800초 $\rightarrow$ `21,600초` (6시간)로 연장.
    - `StrategyPolicy.AUTO_BREAKEVEN_TRIGGER_PCT`: `0.018` (+1.8%) $\rightarrow$ `0.030` (+3.0%)로 상향하여 미세한 잔파동에 본전 스탑이 켜져 조기 청산되는 현상 원천 차단.
  - `src/risk_manager.py`:
    - `TrailingStopTracker.check_position`: `is_bull` 레짐 트레일링 드롭 계산 시 하드코딩된 `0.015` 대신 `StrategyPolicy.BULL_TRAILING_DROP_PCT`(2.5%)를 직접 참조하도록 수정하여 SSOT 단일 기준 준수.
  - `src/realtime_engine.py`:
    - 실시간 0.1초 웹소켓 손절 감시에서 `is_bull_regime` 시 급락 즉시 손절 기준(`severe_drop_threshold`)을 기존 0.965에서 `0.950`(-5.0%)으로 동기화하여 2회 틱 휩쏘 방어 로직 정상 작동 보장.
  - `src/market_screener.py`:
    - `BULL_TREND` 레짐에서 비트코인 대비 상대강도(RS)가 -1.0% 이상 뒤처지는 역행 약세 알트코인에 감점(-25점)을 적용하여 상승장 주도주 위주로 후보 선별.
  - `tests/test_strategy_policy_ssot.py`, `tests/test_bull_trend_strategy.py`:
    - 신규 BULL_TREND 파라미터 및 손익비(1.42~2.85), 타임스탑 봉 수(48봉/72봉), 트레일링 2.5% 동작 단위 테스트 갱신 및 전원 통과 검증.
  - `docs/project-design/strategy-and-risk.md`:
    - 강세장 손익비 정상화 및 휩쏘 방어 설계 문서 반영.

## v9.06 (2026-09-23)

- **Gemini AI 일일 쿼터(RPD) 안전 가드 임계치 95% 상향 및 쿨다운 자동 복구 개선**:
  - `src/gemini_telemetry.py`:
    - `can_call_model`: 모델별 평시 안전 임계치를 기존 85%(Flash-Lite 425회)에서 `95%`(Flash-Lite 475회, 일반 Flash 19회)로 상향. 긴급 탈출(`for_emergency_exit=True`)은 `98%`(Flash-Lite 490회, 일반 Flash 20회)로 보장.
    - `can_make_api_call`: 일일 총 HTTP 호출 시도량 기준 가드를 기존 850회(85%)에서 `950회`(95%)로 상향하여, 단체 가드로 인한 가용 모델(3.1 Flash-Lite 등)의 조기 차단 방지. 긴급 탈출은 `980회`(98%)까지 허용.
  - `src/ai_provider.py`:
    - `_COOLDOWN_REASONS_BY_EXCHANGE` 추가: 쿨다운 등록 사유를 거래소별로 정밀 추적.
    - `can_call_model_safety`: 빗썸 쿼터 안전선을 95%로 상향하고, 사전 임계치 검사("한도 도달")로 걸렸던 쿨다운에 대해 잔여 쿼터 확인 시 자동 해제(`clear_model_cooldown`) 로직 구현. 외부 HTTP 429 에러 쿨다운은 안전하게 보존.
  - `src/gemini_analyzer.py`:
    - `get_candidate_models` 주석 및 독스트링을 95% 안전선(Flash-Lite 475회 / Flash 19회)으로 동기화.
  - `tests/test_gemini_quota_guard.py`, `tests/test_gemini_pt_rollover.py`, `tests/test_gemini_model_cooldown.py`:
    - 95% 임계치 및 비상 탈출 한도 단위 테스트 갱신 및 36개 관련 테스트 전원 통과 검증.
  - `docs/project-design/strategy-and-risk.md`, `docs/project-design/operations-and-observability.md`:
    - 일일 쿼터 가드 정책 및 텔레메트리 관측성 설계 문서 동반 갱신.

## v9.05 (2026-09-22)

- **약세장(RISK_OFF) 레짐 알트코인 단타 적극 매수 완화 및 가용 헤드룸 사전 클램핑 구현**:
  - `src/strategy_engine.py`:
    - `StrategyPolicy.REGIME_MAX_EXPOSURE["RISK_OFF"]`: 기존 `0.20`(20%)에서 `0.50`(50%)로 상향하여 조정/약세장에서도 최대 50%까지 코인 보유 허용.
    - 환경변수 `REGIME_RISK_OFF_MAX_EXPOSURE`를 통한 동적 오버라이드 지원 (`get_regime_max_exposure`).
    - `StrategyPolicy.RISK_OFF_MAX_ALT_ALLOC_PCT`: 기존 `0.06`(6%)에서 `0.12`(12%)로 상향 (`get_risk_off_max_alt_alloc_pct`).
    - `StrategyPolicy.RISK_OFF_MAX_ALT_BUDGET_KRW`: 기존 75,000원에서 200,000원으로 상향 (`get_risk_off_max_alt_budget_krw`).
    - `REGIME_SWING_CAP["RISK_OFF"]`: `0` 유지 (약세장에서는 단타만 집중 운용).
  - `src/trading_runtime.py`:
    - `trade_budget` 산정 시 `StrategyPolicy` 헬퍼 메서드 및 환경변수와 완벽 연동.
    - 레짐별 총 투자 비중 상한에 따른 남은 가용 헤드룸(`max_headroom_order`) 사전 계산 및 자동 클램핑 로직 구현.
    - 한도에 미세하게 걸쳐 주문이 전면 차단되던 구조적 문제를 해결하고, 남은 헤드룸 범위 내에서 안전하게 부분 매수 집행.
  - `tests/test_dynamic_slots.py`:
    - 상향된 50% 익스포저 한도, 단타 연속 매수 허용, 3번째 한도 초과 차단 및 환경변수 오버라이드 단위 테스트 갱신/추가 완료 (전원 통과).
  - `docs/project-design/strategy-and-risk.md`:
    - `RISK_OFF` 알트코인 단타 적극 매수 정책 및 한도, 헤드룸 사전 클램핑 설계 명세 반영.

- **수익 반납 및 손실 전환 방지를 위한 메이저 스윙 분할익절 현실화 및 전 전략 자동 본전 보장(Auto Break-Even) 스탑 구현**:
  - `src/strategy_engine.py`:
    - `StrategyPolicy`에 메이저 전용 스윙 파라미터 신규 정의: `SWING_MAJOR_PARTIAL_TP_1_PCT=0.025` (+2.5%, 40%), `SWING_MAJOR_PARTIAL_TP_2_PCT=0.050` (+5.0%, 30%), `SWING_MAJOR_TRAILING_START_PCT=0.025` (+2.5%), `SWING_MAJOR_TRAILING_DROP_PCT=0.012` (1.2% 반락 시 시장가 청산), `SWING_MAJOR_BREAKEVEN_STOP_PCT=0.005` (+0.5% 마진).
    - 전 전략 공통 자동 본전 보장 파라미터 정의: `AUTO_BREAKEVEN_TRIGGER_PCT=0.018` (+1.8% 고점 도달 시 활성화), `AUTO_BREAKEVEN_STOP_PCT=0.003` (+0.3% 수수료 보장).
  - `src/risk_manager.py`:
    - `TrailingStopTracker`: 포지션 진입가 초과 시 상시 최고가(`self.peaks`) 갱신 체계 구축.
    - 고점 수익률 `+1.8%` 도달 시 `auto_breakeven_active`를 영속 활성화하여 분할익절 완료 전이라도 손절선을 평단가 위로 강제 락인.
    - `is_breakeven_active(market, avg_buy_price)` 개선: 1차 분할익절 체결 또는 +1.8% 고점 도달 시 모두 `True` 반환.
    - `check_position()`에서 `is_swing and is_major` 분기 적용하여 BTC/ETH/SOL이 비현실적인 +8%를 기다리지 않고 +2.5%에서 선제 익절 및 트레일링하도록 개편.
  - `src/realtime_engine.py`, `src/trading_runtime.py`:
    - 실시간 0.1초 틱 및 5분 주기 평가 시 `is_breakeven_active`와 연동하여 메이저 스윙 `+0.5%`, 알트 스윙 `+1.5%`, 일반 단타 `+0.3%` 이상으로 손절선 하한선 보장.
    - 메이저 스윙 1차/2차 분할익절 문구 및 로그를 실제 기준(+2.5%/+5.0%)에 맞게 정밀 표기.
  - `tests/test_swing_major_and_breakeven.py`, `tests/test_swing_strategy.py`:
    - 어제 발생한 ETH(+2.11% 도달 후 급락) 시나리오 완벽 방어 검증, SOL 메이저 스윙 분할익절, 비트코인 1.2% 트레일링 청산, 재시작 복원, 알트 스윙 독립성 단위 테스트 전원 통과 검증.
  - `docs/project-design/strategy-and-risk.md`:
    - 메이저 스윙 분할익절 및 자동 본전 보장 스탑 설계 정책 명세화 완료.

## v9.03 (2026-09-22)

- **사이클당 최대 분석 종목 수(`MAX_CYCLE_MARKETS`) 대시보드 웹 UI 연동 및 빗썸/업비트 공통 핫리로드 구축**:
  - `src/runtime_config.py`:
    - `COMMON_CONFIG_SCHEMA`에 `MAX_CYCLE_MARKETS` (정수형, 기본값 10개, 0=제한없음, 범위 0~30개) 필드 신규 등록 및 유효성 검증 체계 구현.
  - `src/dashboard_server.py`:
    - 대시보드 `⚙️ 공통 설정` 모달의 `🔍 스크리닝 & 전략` 탭에 `cfg_MAX_CYCLE_MARKETS` 입력 폼 추가.
    - [💾 저장 및 즉시 적용] 클릭 시 `.env` 영구 저장 및 활성 봇 코어(17979/17980) 핫리로드 무중단 반영 지원.
  - `src/trading_runtime.py`:
    - 빗썸과 업비트 공통으로 사이클당 타겟 마켓 상한 로직(`cap_cycle_target_markets`) 확장.
    - 거래소 전용 환경변수(`BITHUMB_MAX_CYCLE_MARKETS`, `UPBIT_MAX_CYCLE_MARKETS`) 우선순위 및 공통 `MAX_CYCLE_MARKETS` 연동.
    - 캡핑 시 현재 보유 종목(`held_markets`)을 최우선 보장하여 포지션 보호 완벽 유지.
  - `.env`, `.env.sample`, `.env.upbit`:
    - 공통 `.env` 및 샘플에 `MAX_CYCLE_MARKETS=10` 적용, `.env.upbit`는 주석화하여 공통 설정 자동 상속 지원.
  - `tests/test_runtime_config_manager.py`, `tests/test_trading_runtime.py`:
    - 스키마 커버리지, 정규화/유효성 검사 및 빗썸/업비트 캡핑 동작 단위 테스트 추가 및 전원 통과 검증.
  - `docs/project-design/strategy-and-risk.md`:
    - 사이클당 최대 분석 종목 수 상한 캡 및 대시보드 핫리로드 정책 명세화.

## v9.02 (2026-09-21)

- **상승장 매수 지정가 미체결 취소 방지를 위한 스마트 오더 체결(Smart Order Placement) 적극성 상향**:
  - `src/trading_runtime.py`:
    - 상승장에서 가격이 급등할 때 매수 호가 내림(`floor`) 지정가가 체결되지 못하고 3분 타임아웃(`clean_stale_orders`)으로 취소되는 결함을 해결.
    - 스마트 테이커 발동 조건 완화: 고알파(`alpha_score >= 65`), 모멘텀 돌파(`MOMENTUM_BREAKOUT`), 신규상장(`NEW_LISTING`) 포함.
    - 호가 스프레드 허용폭을 기존 0.20%에서 **0.50%**로, 최우선 매도호가 한도를 현재가 대비 **0.50% 이내**로 현실화.
    - 최우선 매도호가(Ask 1) 제출 시 `adjust_price_to_tick(side="bid", mode="round")`을 적용하여 불필요한 매수호가 내림 방지 및 즉시 체결 유도.
- **업비트 5분 사이클 분석 종목 수 상한 해제 (빗썸과 동일화)**:
  - `src/main_upbit.py`, `src/trading_runtime.py`, `.env.upbit`:
    - 기존 업비트 전용 `UPBIT_MAX_CYCLE_MARKETS=6` 하드 캡을 기본값 `0`(제한 없음, `None`)으로 전환하여, 빗썸과 동일하게 스크리너가 추출한 전 종목(약 18~20개)을 5분마다 전수 분석하도록 개편.
    - 런타임 환경 변수(`UPBIT_MAX_CYCLE_MARKETS`) 변경 시 다음 사이클에 즉시 동적 반영 지원.
  - `tests/test_smart_taker_and_cycle_cap.py`:
    - 스마트 테이커 발동 조건, 모멘텀 돌파, 스프레드 과대 폴백, 업비트 마켓 상한 해제 동작 단위 테스트 작성 및 전원 통과 검증.
- `docs/project-design/strategy-and-risk.md`:
  - 스마트 오더 체결 적극성 정책 및 업비트 분석 종목 수 상한 해제 설계 문서화 완료.

## v9.01 (2026-09-21)

- **시장 국면(Regime) 연동 동적 유동 슬롯(Dynamic Slots) 및 자본 노출도(Exposure) 중심 포트폴리오 관리 시스템 구축**:
  - `src/strategy_engine.py`:
    - `StrategyPolicy.DYNAMIC_SLOTS_ENABLED` (기본 활성, 환경변수/대시보드 제어), `DYNAMIC_SLOT_SAFETY_MAX_POSITIONS` (물리적 안전 상한선 8개) 정책 상수 신설.
    - `REGIME_MAX_EXPOSURE`: 강세장(`BULL_TREND`: 85%), 횡보/일반(`NORMAL`: 55%), 약세장(`RISK_OFF`: 20%), 급락장(`CRASH`: 0%) 차등 배분.
    - `REGIME_SWING_CAP`: 강세장 무제한(최대 8개 내 자율 확장), 횡보장 1개 캡, 약세/급락장 0개(스윙 차단) 정의.
    - 레짐별 정책 조회 헬퍼 메서드(`get_regime_max_exposure`, `get_regime_swing_cap`) 구현.
  - `src/risk_controls.py`:
    - `RiskGuard`: `dynamic_slots_enabled`, `dynamic_safety_max_positions`, `current_regime` 속성 및 `set_current_regime` 메서드 추가.
    - `validate_buy`: 동적 모드 활성화 시 고정 칸막이(`max_swing_positions`, `max_scalp_positions`) 대신 [물리적 안전 상한선(8개)] + [레짐별 스윙 캡] + [레짐별 총 익스포저 상한선]을 종합 판정하여 슬롯 인위적 병목을 완전 해소.
    - 기존 고정 슬롯 모드(`dynamic_slots_enabled=False`)와의 100% 하위 호환성 유지.
  - `src/trading_orchestrator.py`:
    - 스윙 후보 스크리닝(`scan_swing_markets`): 기존 무조건 `top_count=1` 고정 호출에서 탈피하여, 동적 모드 및 강세장(`BULL_TREND`) 시 상위 3개까지 유망 스윙 후보를 수집해 복수 진입 파이프라인으로 연결.
  - `src/trading_runtime.py`:
    - 사이클마다 확정된 `btc_regime`을 `ctx.risk_guard.set_current_regime(btc_regime)`에 즉시 전달하여 실시간 리스크 가드 동기화.
  - `src/runtime_config.py`, `src/bot_controller.py`, `src/dashboard_server.py`:
    - 대시보드 포트폴리오 탭에서 혼란을 유발하던 수동 슬롯 입력 필드(`단타/스윙/신규상장 예약 슬롯 수`, `총 동시 보유 수`) 및 자동 합산/로컬스토리지 보존 자바스크립트 로직을 전면 제거.
    - 대신 직관적이고 현대적인 **[스마트 동적 슬롯 시스템 가동 중] 상태 안내 카드**(레짐별 노출도·스윙 운용·안전 상한 8개 표시)로 교체하여 사용자 인지 부하를 최소화하고, 자본 익스포저 핵심 설정에만 집중하도록 UI를 대폭 단순화.
    - 런타임 핫 리로드(Hot-Reload) 연동 유지.
  - `tests/test_dynamic_slots.py`:
    - 강세장 복수 스윙 연속 승인, 횡보장 스윙 1개 캡 및 단타 허용, 약세장 스윙 차단 및 20% 노출도 캡, 물리적 상한선 8개 차단, 기존 고정 모드 호환성 등 8개 단위 테스트 작성 및 전원 통과 검증 완료.

## v9.00 (2026-09-18)

- **주문 수량 정밀도 내림(ROUND_DOWN) 보정 및 잔고 부족(`insufficient_funds`) 오류 원천 해결**:
  - `src/bithumb_api.py`: `BithumbAPI.round_volume`을 기존 과거 4자리 반올림(`round(volume, 4)`)에서 빗썸 v2 공식 규격인 소수점 8자리 및 `Decimal` 기반 무조건 내림(`ROUND_DOWN`)으로 전면 개편.
    - **원인 분석**: 1차 50% 분할 익절 후 남은 잔여 수량(예: EDEN 283.50009262개)을 타임스탑/보호청산으로 전량 매도할 때, `round(..., 4)`로 인해 `283.5001`개로 올림되어 계좌 잔고를 초과, 빗썸 API에서 `400 Client Error: insufficient_funds (주문가능한 금액(EDEN)이 부족합니다.)` 에러가 발생하여 5분 루프마다 크래시가 반복되던 문제를 해결.
  - `src/upbit_api.py`: `UpbitAPI.round_volume` 역시 반올림(`round(volume, 8)`) 대신 `Decimal` 기반 소수점 8자리 내림(`ROUND_DOWN`)을 적용하고, `create_order` 지정가/시장가 매도 시 보정 수량을 전송하도록 일관화.
  - `tests/test_startup_integration.py`, `tests/test_upbit_api.py`: 빗썸/업비트 소수점 8자리 보존 및 초과 자릿수 내림 검증, EDEN 잔고 이슈 재현 케이스 단위 테스트 추가 및 통과 검증.
- **APScheduler 일시 지연 시 사이클 건너뜀(Missed Run Time) 방지**:
  - `src/trading_bot_bootstrap.py`: `BackgroundScheduler.add_job`에 `misfire_grace_time`을 명시적으로 설정(`run_cycle` 최소 120초, 모닝 리포트 600초).
  - **원인 분석**: APScheduler 기본 `misfire_grace_time`이 1초에 불과하여, WebSocket I/O나 고래 체결 감지 등 백그라운드 작업 경합으로 스케줄러가 예정 시각보다 단 1.3초 늦게 트리거되었을 때 사이클 전체가 건너뛰어지던(`missed by 0:00:01.326796`) 문제를 해결.
- **스윙 전략 진입 승인 로깅 포맷 에러 수정**:
  - `src/trading_runtime.py`: `process_entry_gating`의 스윙 전략 진입 승인 로그에서 파이썬 `%` 연산자에 지원되지 않는 콤마 포맷 지정자(`%,.2f`)를 사용하여 `ValueError: unsupported format character ',' (0x2c)` 에러가 발생하던 문제를 표준 f-string (`{target_price:,.2f}원`)으로 교체하여 해결.
  - `tests/test_swing_4h_safety.py`: 버퍼 비율 상향(1.005)에 맞게 단위 테스트 단언문 동기화 및 14건 전체 통과.

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
