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

    def test_operational_observability_helpers_preserve_fill_boundary(self):
        """표시 전용 우선순위와 주문 흐름이 ACK를 체결 확정으로 표현하지 않는지 검증"""
        priority_code = self._extract_function("getPositionOperationalPriority")
        lifecycle_code = self._extract_function("getOrderLifecycle")
        summary_code = self._extract_function("summarizeCandidateReason")
        candidate_code = self._extract_function("getCandidateEntryAvailability")
        js_code = f"""
        {priority_code}
        {lifecycle_code}
        {summary_code}
        {candidate_code}
        const results = {{
          urgent: getPositionOperationalPriority({{ current_price: 100, stop_loss: 101 }}),
          observe: getPositionOperationalPriority({{ current_price: 100, stop_loss: 99.5 }}),
          ack: getOrderLifecycle({{ status: 'ACKNOWLEDGED' }}),
          filled: getOrderLifecycle({{ status: 'FILLED' }}),
          blocked: getCandidateEntryAvailability({{ allow_buy: true }}, {{ entry_ready: false, entry_block_reasons: ['체결 대사 진행 주문 1건'] }}),
          eligible: getCandidateEntryAvailability({{ allow_buy: true }}, {{ entry_ready: true }}),
          watching: getCandidateEntryAvailability({{ allow_buy: false, reason: '1차 퀀트 관망 대기: 하드게이트 통과, 알파스코어 74점, MA5 <= MA20, RSI 45.8' }}, {{ entry_ready: true }})
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

        self.assertEqual(results["urgent"]["label"], "즉시 확인")
        self.assertEqual(results["observe"]["label"], "관찰")
        self.assertIn("체결 아님", results["ack"]["label"])
        self.assertIn("체결 확정", results["filled"]["label"])
        self.assertEqual(results["blocked"]["label"], "전역 차단")
        self.assertEqual(results["eligible"]["label"], "진입 검토 가능")
        self.assertEqual(results["watching"]["detail"], "1차 퀀트 관망 대기 · 알파 74점")

    def test_watchlist_and_order_journal_use_scroll_limits(self):
        """운영 표는 약 10건 높이만 표시하고 나머지 행은 스크롤로 확인해야 한다."""
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        index_path = os.path.join(project_root, "dashboard", "index.html")
        style_path = os.path.join(project_root, "dashboard", "src", "styles.css")

        with open(index_path, "r", encoding="utf-8") as index_file:
            index_content = index_file.read()
        with open(style_path, "r", encoding="utf-8") as style_file:
            style_content = style_file.read()

        self.assertIn('class="table-scroll-watchlist overflow-x-auto', index_content)
        self.assertIn('class="table-scroll-order-journal overflow-x-auto', index_content)
        self.assertIn('class="table-scroll-recent-trades overflow-x-auto', index_content)
        self.assertIn('.table-scroll-watchlist {', style_content)
        self.assertIn('max-height: 38rem;', style_content)
        self.assertIn('.table-scroll-order-journal', style_content)
        self.assertIn('.table-scroll-recent-trades {', style_content)
        self.assertIn('max-height: 29rem;', style_content)
        self.assertIn('position: sticky;', style_content)

    def test_safety_panel_uses_exchange_cards_instead_of_duplicate_feed_board(self):
        """실거래 안전 상태는 거래소별 카드에만 시세 상태를 표시해야 한다."""
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        index_path = os.path.join(project_root, "dashboard", "index.html")

        with open(index_path, "r", encoding="utf-8") as index_file:
            index_content = index_file.read()

        self.assertIn('id="safety_exchange_detail"', index_content)
        self.assertNotIn('id="feed_health"', index_content)


if __name__ == "__main__":
    unittest.main()
