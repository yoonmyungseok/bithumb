"""설계 문서 동반 갱신 검사기의 책임 매핑을 검증한다."""

import sys
import unittest
from pathlib import Path

# 독립 실행 도구를 테스트에서 직접 호출할 수 있도록 도구 경로를 추가한다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from check_design_docs import CHANGELOG, validate  # noqa: E402


class DesignDocumentationCheckTest(unittest.TestCase):
    def test_strategy_change_requires_strategy_document_and_changelog(self):
        missing = validate(["src/strategy_engine.py"])

        self.assertIn("docs/project-design/strategy-and-risk.md", missing)
        self.assertIn(CHANGELOG, missing)

    def test_execution_change_passes_with_matching_documents(self):
        missing = validate(
            [
                "src/order_safety/executor.py",
                "docs/project-design/execution-and-safety.md",
                CHANGELOG,
            ]
        )

        self.assertEqual([], missing)

    def test_runtime_change_requires_all_connected_design_documents(self):
        missing = validate(["src/trading_runtime.py"])

        self.assertIn("docs/project-design/architecture.md", missing)
        self.assertIn("docs/project-design/strategy-and-risk.md", missing)
        self.assertIn("docs/project-design/execution-and-safety.md", missing)

    def test_test_only_change_does_not_require_design_document(self):
        self.assertEqual([], validate(["tests/test_strategy_engine.py"]))
