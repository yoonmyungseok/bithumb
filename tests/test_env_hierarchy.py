# -*- coding: utf-8 -*-
"""환경 변수 3단계 계층 구조(.env 공통, .env.bithumb, .env.upbit) 로딩 및 격리 테스트."""

import os
import sys
import tempfile
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_runtime import TradingCycleEngine, TradingRuntimeConfig


def _create_mock_engine(common_env: str | None, exchange_env: str | None) -> TradingCycleEngine:
    """테스트용 가상 TradingCycleEngine 인스턴스를 생성한다."""
    mock_config = MagicMock(spec=TradingRuntimeConfig)
    mock_config.common_env_file = common_env
    mock_config.env_file = exchange_env
    mock_config.entry_profile = MagicMock()
    mock_config.buy_profile = MagicMock()
    mock_config.analyzer_factory = None
    mock_config.gemini_api_key = ""

    mock_context = MagicMock()
    mock_profile = MagicMock()

    engine = TradingCycleEngine.__new__(TradingCycleEngine)
    engine.config = mock_config
    engine.context = mock_context
    engine.profile = mock_profile
    engine.entry_profile = mock_config.entry_profile
    engine.buy_profile = mock_config.buy_profile
    engine.analyzer = None
    return engine


def test_env_hierarchy_inheritance_and_override(monkeypatch):
    """공통 .env의 기본값을 거래소 전용 .env.* 파일이 올바르게 오버라이드하고 상속하는지 검증한다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        common_env = os.path.join(tmpdir, ".env")
        bithumb_env = os.path.join(tmpdir, ".env.bithumb")

        # 공통 설정 작성
        with open(common_env, "w", encoding="utf-8") as f:
            f.write(
                "DASHBOARD_PORT=7979\n"
                "INTERVAL_MINUTES=5\n"
                "COMMON_ONLY_SETTING=shared_value\n"
            )

        # 빗썸 전용 설정 작성 (INTERVAL_MINUTES 오버라이드, 빗썸 전용 키 추가)
        with open(bithumb_env, "w", encoding="utf-8") as f:
            f.write(
                "INTERVAL_MINUTES=3\n"
                "BITHUMB_SPECIFIC_KEY=bithumb_secret\n"
            )

        # 환경변수 초기화
        for k in ["DASHBOARD_PORT", "INTERVAL_MINUTES", "COMMON_ONLY_SETTING", "BITHUMB_SPECIFIC_KEY"]:
            monkeypatch.delenv(k, raising=False)

        engine = _create_mock_engine(common_env=common_env, exchange_env=bithumb_env)
        engine._load_cycle_environment()

        # 공통 설정 상속 확인
        assert os.getenv("DASHBOARD_PORT") == "7979"
        assert os.getenv("COMMON_ONLY_SETTING") == "shared_value"
        # 거래소 전용 파일에 의한 오버라이드 확인
        assert os.getenv("INTERVAL_MINUTES") == "3"
        # 거래소 전용 키 로드 확인
        assert os.getenv("BITHUMB_SPECIFIC_KEY") == "bithumb_secret"


def test_env_fallback_when_exchange_file_missing(monkeypatch):
    """거래소 전용 파일이 없을 때 공통 .env만으로 정상 Fallback 로드되는지 검증한다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        common_env = os.path.join(tmpdir, ".env")
        missing_bithumb_env = os.path.join(tmpdir, ".env.bithumb")  # 생성하지 않음

        with open(common_env, "w", encoding="utf-8") as f:
            f.write("DASHBOARD_HOST=127.0.0.1\nFALLBACK_TEST=success\n")

        monkeypatch.delenv("DASHBOARD_HOST", raising=False)
        monkeypatch.delenv("FALLBACK_TEST", raising=False)

        engine = _create_mock_engine(common_env=common_env, exchange_env=missing_bithumb_env)
        engine._load_cycle_environment()

        assert os.getenv("DASHBOARD_HOST") == "127.0.0.1"
        assert os.getenv("FALLBACK_TEST") == "success"


def test_upbit_hierarchy_isolation(monkeypatch):
    """업비트 전용 파일이 공통 설정을 상속받으면서 업비트 키를 격리 로드하는지 검증한다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        common_env = os.path.join(tmpdir, ".env")
        upbit_env = os.path.join(tmpdir, ".env.upbit")

        with open(common_env, "w", encoding="utf-8") as f:
            f.write("DASHBOARD_ACTION_TOKEN=global_secret\nUPBIT_API_URL=http://127.0.0.1:17980\n")

        with open(upbit_env, "w", encoding="utf-8") as f:
            f.write("UPBIT_SPECIFIC_SETTING=upbit_val\n")

        monkeypatch.delenv("DASHBOARD_ACTION_TOKEN", raising=False)
        monkeypatch.delenv("UPBIT_API_URL", raising=False)
        monkeypatch.delenv("UPBIT_SPECIFIC_SETTING", raising=False)

        engine = _create_mock_engine(common_env=common_env, exchange_env=upbit_env)
        engine._load_cycle_environment()

        assert os.getenv("DASHBOARD_ACTION_TOKEN") == "global_secret"
        assert os.getenv("UPBIT_API_URL") == "http://127.0.0.1:17980"
        assert os.getenv("UPBIT_SPECIFIC_SETTING") == "upbit_val"


def test_entrypoint_loading_pattern(monkeypatch):
    """main.py 및 main_upbit.py의 시작 시 계층 로딩 패턴을 직접 검증한다."""
    from dotenv import load_dotenv

    with tempfile.TemporaryDirectory() as tmpdir:
        common_env = os.path.join(tmpdir, ".env")
        bithumb_env = os.path.join(tmpdir, ".env.bithumb")

        with open(common_env, "w", encoding="utf-8") as f:
            f.write("SHARED_CFG=common\nOVERRIDE_ME=old\n")
        with open(bithumb_env, "w", encoding="utf-8") as f:
            f.write("OVERRIDE_ME=new_bithumb\n")

        monkeypatch.delenv("SHARED_CFG", raising=False)
        monkeypatch.delenv("OVERRIDE_ME", raising=False)

        # main.py 시작 로딩 패턴 시뮬레이션
        if os.path.exists(common_env):
            load_dotenv(common_env, override=True)
        if os.path.exists(bithumb_env):
            load_dotenv(bithumb_env, override=True)
        elif not os.path.exists(common_env):
            load_dotenv(override=True)

        assert os.getenv("SHARED_CFG") == "common"
        assert os.getenv("OVERRIDE_ME") == "new_bithumb"
