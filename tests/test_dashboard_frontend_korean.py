"""
대시보드 프론트엔드(app.js)의 체결 사유 및 상태 한글 변환 검증 테스트
- AI_EMERGENCY_EXIT 등 AI 사유의 올바른 한국어 변환
- 체결 구분 뱃지, 행동 뱃지, 피드 상태 한글화 검증
"""

import json
import os
import subprocess
import unittest


class DashboardFrontendKoreanTests(unittest.TestCase):
    """대시보드 JavaScript 함수의 한글 변환 동작 검증"""

    @classmethod
    def setUpClass(cls):
        app_js_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "dashboard", "src", "app.js")
        )
        with open(app_js_path, "r", encoding="utf-8") as f:
            cls.app_js_content = f.read()

    def _extract_function(self, func_name: str) -> str:
        idx = self.app_js_content.find(f"function {func_name}(")
        self.assertNotEqual(idx, -1, f"{func_name} function must exist in app.js")
        start_brace = self.app_js_content.find("{", idx)
        depth = 0
        for i in range(start_brace, len(self.app_js_content)):
            if self.app_js_content[i] == "{":
                depth += 1
            elif self.app_js_content[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.app_js_content[idx : i + 1]
        raise ValueError(f"Braces not closed for {func_name}")

    def test_format_reason_korean_translations(self):
        """formatReason이 AI_EMERGENCY_EXIT를 비롯한 주요 사유들을 한글로 올바르게 변환하는지 검증"""
        func_code = self._extract_function("formatReason")
        js_code = f"""
        {func_code}
        const testCases = [
            'AI_EMERGENCY_EXIT',
            'AI_TIGHTENED_STOP',
            'AI_EXIT',
            'EMERGENCY_EXIT',
            'TRAILING_STOP',
            'TIME_STOP',
            'PANIC_SELL',
            'MANUAL_EXIT',
            'MOMENTUM_BREAKOUT',
            'ORDERBOOK_IMBALANCE',
            'DAILY_LOSS_LIMIT',
            'MARKET_COOLDOWN'
        ];
        const results = Object.fromEntries(testCases.map(tc => [tc, formatReason(tc)]));
        console.log(JSON.stringify(results));
        """
        proc = subprocess.run(
            ["node", "-e", js_code],
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )
        results = json.loads(proc.stdout)

        # AI_EMERGENCY_EXIT 검증
        self.assertIn("비상탈출", results["AI_EMERGENCY_EXIT"])
        self.assertIn("AI", results["AI_EMERGENCY_EXIT"])
        self.assertNotIn("AI_EMERGENCY_EXIT", results["AI_EMERGENCY_EXIT"])

        # AI_TIGHTENED_STOP 검증
        self.assertIn("손절선 상향", results["AI_TIGHTENED_STOP"])
        self.assertNotIn("AI_TIGHTENED_STOP", results["AI_TIGHTENED_STOP"])

        # 기타 주요 사유 검증
        self.assertIn("트레일링", results["TRAILING_STOP"])
        self.assertIn("타임스탑", results["TIME_STOP"])
        self.assertIn("긴급 전량매도", results["PANIC_SELL"])
        self.assertIn("수동 청산", results["MANUAL_EXIT"])
        self.assertIn("모멘텀 돌파", results["MOMENTUM_BREAKOUT"])
        self.assertIn("호가 불균형", results["ORDERBOOK_IMBALANCE"])
        self.assertIn("일일 손실 한도", results["DAILY_LOSS_LIMIT"])
        self.assertIn("재진입 쿨다운", results["MARKET_COOLDOWN"])

    def test_format_trade_side_badge_korean(self):
        """formatTradeSideBadge가 AI 비상탈출 사유를 한글 뱃지로 변환하는지 검증"""
        func_code = self._extract_function("formatTradeSideBadge")
        js_code = f"""
        {func_code}
        const results = {{
            emergency: formatTradeSideBadge('AI_EMERGENCY_EXIT'),
            tightened: formatTradeSideBadge('AI_TIGHTENED_STOP'),
            trailing: formatTradeSideBadge('TRAILING_STOP'),
            panic: formatTradeSideBadge('PANIC_SELL'),
            buy: formatTradeSideBadge('BID')
        }};
        console.log(JSON.stringify(results));
        """
        proc = subprocess.run(
            ["node", "-e", js_code],
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )
        results = json.loads(proc.stdout)

        self.assertIn("AI비상탈출", results["emergency"])
        self.assertIn("AI손절상향", results["tightened"])
        self.assertIn("트레일링", results["trailing"])
        self.assertIn("긴급매도", results["panic"])
        self.assertIn("매수", results["buy"])

    def test_render_action_badge_korean(self):
        """renderActionBadge가 비상탈출 및 손절상향 액션을 한글 뱃지로 변환하는지 검증"""
        func_code = self._extract_function("renderActionBadge")
        js_code = f"""
        {func_code}
        const results = {{
            emergency: renderActionBadge('EMERGENCY_EXIT'),
            tighten: renderActionBadge('TIGHTEN_STOP'),
            runner: renderActionBadge('RUNNER_HOLD'),
            buy: renderActionBadge('BUY')
        }};
        console.log(JSON.stringify(results));
        """
        proc = subprocess.run(
            ["node", "-e", js_code],
            capture_output=True,
            text=True,
            check=True,
            encoding="utf-8",
        )
        results = json.loads(proc.stdout)

        self.assertIn("비상 탈출", results["emergency"])
        self.assertIn("손절선 상향", results["tighten"])
        self.assertIn("추세 홀딩", results["runner"])
        self.assertIn("매수 승인", results["buy"])


if __name__ == "__main__":
    unittest.main()
