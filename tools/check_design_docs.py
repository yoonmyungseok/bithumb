"""설계 문서 동반 갱신 여부를 검사하는 CI 도구입니다."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable
from pathlib import PurePosixPath


CHANGELOG = "docs/project-design/changelog.md"
DESIGN_ROOT = "docs/project-design/"

# 코드 책임과 설계 문서의 책임을 같은 이름으로 연결한다.
CATEGORY_PREFIXES = {
    "strategy-and-risk": (
        "src/strategy_engine.py",
        "src/market_screener.py",
        "src/gemini_analyzer.py",
        "src/ai_provider.py",
        "src/risk_controls.py",
        "src/backtest.py",
        "src/trading_runtime.py",
    ),
    "execution-and-safety": (
        "src/order_safety/",
        "src/bithumb_api.py",
        "src/upbit_api.py",
        "src/realtime_engine.py",
        "src/base_websocket.py",
        "src/websocket_manager.py",
        "src/private_websocket_manager.py",
        "src/upbit_websocket.py",
        "src/upbit_private_websocket.py",
        "src/trading_runtime.py",
    ),
    "operations-and-observability": (
        "src/dashboard_server.py",
        "src/bot_controller.py",
        "src/web_server.py",
        "src/gemini_telemetry.py",
        "src/api_telemetry.py",
        "src/trading_watchdog.py",
        "src/watchdog.py",
        "src/watchdog_upbit.py",
        "src/process_manager.py",
    ),
    "architecture": (
        "src/main.py",
        "src/main_upbit.py",
        "src/trading_runtime.py",
        "src/trading_bot_bootstrap.py",
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
    ),
}


def matches_prefix(path: str, prefix: str) -> bool:
    """파일 또는 디렉터리 접두사와 정확히 일치하는지 확인한다."""
    return path == prefix or path.startswith(prefix)


def required_documents(changed_paths: Iterable[str]) -> set[str]:
    """변경된 운영 코드가 요구하는 설계 문서 목록을 계산한다."""
    required: set[str] = set()
    for path in changed_paths:
        if path.startswith("src/") and path.endswith(".py"):
            # 어떤 운영 코드 변경이든 변경 이력과 최소 한 개의 설계 설명이 필요하다.
            required.add(CHANGELOG)
        for category, prefixes in CATEGORY_PREFIXES.items():
            if any(matches_prefix(path, prefix) for prefix in prefixes):
                required.add(f"{DESIGN_ROOT}{category}.md")
                required.add(CHANGELOG)
    return required


def validate(changed_paths: Iterable[str]) -> list[str]:
    """변경 목록을 기준으로 누락된 문서를 사람이 읽을 수 있게 반환한다."""
    changed = {PurePosixPath(path).as_posix() for path in changed_paths if path}
    required = required_documents(changed)
    missing = sorted(required - changed)
    if any(path.startswith("src/") and path.endswith(".py") for path in changed):
        has_design_update = any(path.startswith(DESIGN_ROOT) and path.endswith(".md") for path in changed)
        if not has_design_update:
            missing.append("docs/project-design/ 아래 관련 설계 문서 1개 이상")
    return missing


def changed_paths_from_git(base: str) -> list[str]:
    """base와 현재 HEAD의 공통 조상 이후 변경 파일만 읽는다."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def main() -> int:
    parser = argparse.ArgumentParser(description="설계 문서 동반 갱신 검사")
    parser.add_argument("--base", required=True, help="비교 기준 Git SHA 또는 ref")
    args = parser.parse_args()
    changed = changed_paths_from_git(args.base)
    missing = validate(changed)
    if not missing:
        print("설계 문서 동반 갱신 검사 통과")
        return 0

    print("다음 설계 문서를 같은 변경에 포함해야 합니다:", file=sys.stderr)
    for path in missing:
        print(f"- {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
