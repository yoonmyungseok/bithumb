import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import os
import tempfile
import pytest
from strategy_engine import StrategyPolicy, calculate_composite_alpha_score
from order_safety.risk_off_loss_reentry import RiskOffLossReentryGuard
from trade_memory import TradeMemoryManager
from db_manager import DatabaseManager


def test_penny_stock_breakeven_buffer():
    # 초저가주 (< 10.0 KRW): 최소 +0.55%
    bep_penny = StrategyPolicy.get_effective_breakeven_min_pct(7.20, fee_rate=0.0004)
    assert bep_penny >= 0.0055
    assert bep_penny == pytest.approx(0.0055)

    # 일반 코인 (>= 10.0 KRW): 기본 +0.35%
    bep_normal = StrategyPolicy.get_effective_breakeven_min_pct(500.0, fee_rate=0.0004)
    assert bep_normal == pytest.approx(0.0035)


def test_regime_mtf_ema20_ratio():
    # NORMAL 장세: 0.980
    assert StrategyPolicy.get_mtf_ema20_ratio('NORMAL') == 0.980
    assert StrategyPolicy.get_mtf_ema20_ratio('BULL_TREND') == 0.980

    # RISK_OFF 장세: 0.980 (-2.0% 이내 안착 필수, 역추세 방어)
    assert StrategyPolicy.get_mtf_ema20_ratio('RISK_OFF') == 0.980


def test_mtf_downtrend_zero_score_in_alpha():
    candles_5m = [{'trade_price': 100.0, 'high_price': 101.0, 'low_price': 99.0, 'opening_price': 99.5, 'candle_acc_trade_volume': 1000.0} for _ in range(25)]
    candles_1h = [{'trade_price': 90.0} for _ in range(5)] + [{'trade_price': 100.0} for _ in range(20)]
    
    res = calculate_composite_alpha_score(candles=candles_5m, candles_1h=candles_1h, btc_regime='RISK_OFF')
    assert res['factor_breakdown']['mtf_score'] == 0
    assert '1H 역배열' in res['factor_breakdown']['mtf_reason']


def test_soft_blacklist_after_two_daily_losses():
    with tempfile.TemporaryDirectory() as tmp_dir:
        guard_bithumb = RiskOffLossReentryGuard(data_dir=tmp_dir, exchange='bithumb')
        
        # 1회 손절 기록
        guard_bithumb.record_confirmed_loss_exit(
            exchange='bithumb', market='KRW-SOLV', exit_reason='STOP_LOSS', net_pnl_krw=-500.0,
        )
        blocked, _ = guard_bithumb.check_reentry_blocked('KRW-SOLV', btc_regime='NORMAL', candidate_type='CONFIRMED')
        assert not blocked

        blocked, _ = guard_bithumb.check_reentry_blocked('KRW-SOLV', btc_regime='RISK_OFF', candidate_type='MOMENTUM_BREAKOUT')
        assert blocked

        # 2회째 손절 기록
        guard_bithumb.record_confirmed_loss_exit(
            exchange='bithumb', market='KRW-SOLV', exit_reason='TIME_STOP', net_pnl_krw=-300.0,
        )
        blocked, info = guard_bithumb.check_reentry_blocked('KRW-SOLV', btc_regime='NORMAL', candidate_type='CONFIRMED')
        assert blocked
        assert info['loss_count'] == 2
        assert '당일 2회 연속 손실' in info['summary']
        assert guard_bithumb.applies_to_entry_path('NORMAL', 'CONFIRMED', market='KRW-SOLV')


def test_trade_memory_duplicate_prevention():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tm = TradeMemoryManager(data_dir=tmp_dir, exchange_scope='bithumb')
        
        trade_id = 'tr-test-dedup-12345'
        tm.record_completed_trade(
            market='KRW-BTC', side='트레일링 익절', entry_price=100000000.0, exit_price=102000000.0,
            pnl_pct=2.0, pnl_krw=20000.0, reason='트레일링', timestamp='2026-09-16 10:00:00',
            trade_id=trade_id, exchange='bithumb'
        )
        assert len(tm.trades) == 1

        # 동일 trade_id로 2차 기록 시도
        tm.record_completed_trade(
            market='KRW-BTC', side='트레일링 익절', entry_price=100000000.0, exit_price=102000000.0,
            pnl_pct=2.0, pnl_krw=20000.0, reason='트레일링', timestamp='2026-09-16 10:00:00',
            trade_id=trade_id, exchange='bithumb'
        )
        assert len(tm.trades) == 1

        db_trades = tm.db.get_trades(exchange='bithumb', market='KRW-BTC')
        assert len(db_trades) == 1
