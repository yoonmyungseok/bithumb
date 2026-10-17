import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dashboard_server import UnifiedDashboardServer
from web_server import DashboardWebServer


class Analysis24hDashboardTests(unittest.TestCase):
    """24h 종목 시세 흐름 및 전략 분석 보고서 대시보드 API 단위 테스트"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.analysis_dir = self.tmp_dir.name

        # 테스트용 24h 보고서 4개 및 무관한 파일 생성
        self.file_holo = os.path.join(self.analysis_dir, "HOLO_24h_20261017.md")
        self.file_holo_past = os.path.join(self.analysis_dir, "HOLO_24h_20261016.md")
        self.file_rsr = os.path.join(self.analysis_dir, "RSR_24h_20261017.md")
        self.file_us_old = os.path.join(self.analysis_dir, "US_24h_20261016.md")
        self.file_other = os.path.join(self.analysis_dir, "other_notes.txt")

        with open(self.file_holo, "w", encoding="utf-8") as f:
            f.write("# 홀로월드에이아이(HOLO) 24시간 시세 흐름 분석\n\n내용 HOLO 최신")
        with open(self.file_holo_past, "w", encoding="utf-8") as f:
            f.write("# 홀로월드에이아이(HOLO) 24시간 시세 흐름 분석 (과거)\n\n내용 HOLO 과거")
        with open(self.file_rsr, "w", encoding="utf-8") as f:
            f.write("# 리저브라이트(RSR) 24시간 시세 흐름 분석\n\n내용 RSR")
        with open(self.file_us_old, "w", encoding="utf-8") as f:
            f.write("# US 24시간 시세 흐름 분석\n\n과거 내용 US")
        with open(self.file_other, "w", encoding="utf-8") as f:
            f.write("무관한 텍스트 파일")

        self.server = UnifiedDashboardServer(
            port=17998,
            analysis_24h_dir=self.analysis_dir,
        )
        self.web_server = DashboardWebServer(
            port=17997,
            analysis_24h_dir=self.analysis_dir,
            is_api_only=True,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_get_analysis_24h_reports_listing_and_sorting(self):
        """24h 보고서 목록이 종목별 가장 최신 보고서만 필터링되고 최신 날짜 및 심볼순으로 정렬되는지 검증"""
        # 기본 조회: 종목별 최신 1개만 반환 (HOLO는 20261017만 포함, 20261016은 제외)
        result = self.server.get_analysis_24h_reports()

        self.assertTrue(result["success"])
        self.assertEqual(result["total"], 3)
        reports = result["reports"]

        # 최신 날짜(20261017) 우선 정렬, 동일 날짜는 심볼 알파벳 오름차순(HOLO -> RSR)
        self.assertEqual(reports[0]["filename"], "HOLO_24h_20261017.md")
        self.assertEqual(reports[0]["symbol"], "HOLO")
        self.assertEqual(reports[0]["raw_date"], "20261017")
        self.assertEqual(reports[0]["date"], "2026-10-17")
        self.assertIn("홀로월드에이아이", reports[0]["title"])

        self.assertEqual(reports[1]["filename"], "RSR_24h_20261017.md")
        self.assertEqual(reports[1]["symbol"], "RSR")

        self.assertEqual(reports[2]["filename"], "US_24h_20261016.md")
        self.assertEqual(reports[2]["symbol"], "US")
        self.assertEqual(reports[2]["raw_date"], "20261016")

        # only_latest=False 시 전체 4개 보고서 모두 반환 확인
        all_result = self.server.get_analysis_24h_reports(only_latest=False)
        self.assertEqual(all_result["total"], 4)

    def test_get_analysis_24h_content_default_latest(self):
        """타깃 지정 없이 조회 시 가장 최신의 첫 번째 보고서가 반환되는지 검증"""
        res = self.server.get_analysis_24h_content(None)

        self.assertTrue(res["success"])
        self.assertEqual(res["filename"], "HOLO_24h_20261017.md")
        self.assertEqual(res["symbol"], "HOLO")
        self.assertIn("내용 HOLO", res["content"])

    def test_get_analysis_24h_content_by_filename(self):
        """정확한 파일명으로 조회 시 해당 마크다운 내용이 반환되는지 검증"""
        res = self.server.get_analysis_24h_content("RSR_24h_20261017.md")

        self.assertTrue(res["success"])
        self.assertEqual(res["filename"], "RSR_24h_20261017.md")
        self.assertEqual(res["symbol"], "RSR")
        self.assertIn("내용 RSR", res["content"])

    def test_get_analysis_24h_content_by_symbol(self):
        """심볼명만으로 조회 시 해당 종목의 최신 보고서가 매칭되는지 검증"""
        res = self.server.get_analysis_24h_content("rsr")

        self.assertTrue(res["success"])
        self.assertEqual(res["filename"], "RSR_24h_20261017.md")
        self.assertEqual(res["symbol"], "RSR")

    def test_get_analysis_24h_content_traversal_attack_blocked(self):
        """디렉터리 트래버설 공격 시도 시 fail-closed로 거부되는지 검증"""
        res1 = self.server.get_analysis_24h_content("../etc/passwd")
        self.assertFalse(res1["success"])

        res2 = self.server.get_analysis_24h_content("..\\windows\\system32")
        self.assertFalse(res2["success"])

    def test_web_server_analysis_24h_methods(self):
        """DashboardWebServer에서도 동일하게 24h 보고서 목록 및 내용이 반환되는지 검증"""
        list_res = self.web_server.get_analysis_24h_reports()
        self.assertTrue(list_res["success"])
        self.assertEqual(list_res["total"], 3)

        content_res = self.web_server.get_analysis_24h_content("HOLO_24h_20261017.md")
        self.assertTrue(content_res["success"])
        self.assertIn("홀로월드에이아이", content_res["title"])


if __name__ == "__main__":
    unittest.main()
