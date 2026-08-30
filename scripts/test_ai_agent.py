"""AI에이전트의 라우트, 보안 경계, 실제 DB 조회 도구 회귀 테스트."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
SOURCE_DB = PROJECT_ROOT / "data" / "saedam.db"
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="saedam_ai_test_", dir=PROJECT_ROOT / "tmp"))
shutil.copy2(SOURCE_DB, TEST_DATA_DIR / "saedam.db")
os.environ["DATA_DIR"] = str(TEST_DATA_DIR)
os.environ.pop("OPENAI_API_KEY", None)

from app import app  # noqa: E402
from routes.database import get_db  # noqa: E402
from routes.openai_settings import save_preset  # noqa: E402
from services.ai_tools import ToolPermissionError, execute_tool  # noqa: E402
from services.openai_agent import get_ai_agent_configuration  # noqa: E402


def context(**overrides):
    value = {
        "emp_no": "admin",
        "user_name": "admin",
        "user_level": 1,
        "position": "최고관리자",
        "department": "",
        "center_director_mode": False,
        "menu_access": {
            "attendance_main": True,
            "school_workspace": True,
            "expense_main": True,
            "contract_admin": True,
            "verified_contract_admin": True,
            "contacts_main": True,
            "gallery_main": True,
            "board_noti": True,
            "board_archive": True,
            "board_manual": True,
        },
    }
    value.update(overrides)
    return value


class AIAgentTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)

    def setUp(self):
        app.config.update(TESTING=True)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["emp_no"] = "admin"
            session["user_name"] = "admin"
            session["user_level"] = 1
            session["position"] = "최고관리자"
            session["department"] = ""

    def test_page_and_menu_render(self):
        response = self.client.get("/ai-agent")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("AI에이전트", text)
        self.assertIn("새담인트라넷에 대해 무엇이든 물어보세요.", text)
        self.assertIn("이번 달 근태 우수자", text)
        self.assertIn('href="/ai-agent"', text)
        self.assertIn('id="aiAgentApiStatus"', text)
        self.assertNotIn('id="aiAgentSettings"', text)
        self.assertNotIn('id="aiAgentSettingsModal"', text)
        css = (PROJECT_ROOT / "static" / "css" / "ai_agent.css").read_text(encoding="utf-8")
        self.assertIn("height: calc(80vh - 83px);", css)
        self.assertIn("min-height: 496px;", css)
        self.assertEqual(self.client.get("/ai-agent/").status_code, 200)

    def test_ai_preset_is_shared_from_admin_menu_without_exposing_key(self):
        # 통합관리 > AI api설정의 프리셋 1은 emp_no와 무관하게 전 직원(AI에이전트·스마트공문발송)에 공통 적용된다.
        save_preset("1", "openai", "gpt-5.6-terra", api_key="sk-test-" + "a" * 32, actor="admin")
        try:
            for emp_no in ("admin", "sd05002", "sd08001"):
                configuration = get_ai_agent_configuration(context(emp_no=emp_no))
                self.assertTrue(configuration["configured"])
                self.assertEqual(configuration["source"], "menu")
                self.assertEqual(configuration["provider"], "openai")
                self.assertEqual(configuration["model"], "gpt-5.6-terra")
                self.assertEqual(configuration["status_text"], "프리셋 1 · OpenAI gpt-5.6-terra · 메뉴 등록 API 사용 중")
                self.assertNotIn("api_key", configuration)

            page = self.client.get("/ai-agent").get_data(as_text=True)
            self.assertIn("프리셋 1 · OpenAI gpt-5.6-terra · 메뉴 등록 API 사용 중", page)
            self.assertNotIn("sk-test-", page)

            smart_document_page = self.client.get("/smart-document").get_data(as_text=True)
            self.assertIn("프리셋 1 · OpenAI gpt-5.6-terra · 메뉴 등록 API 사용 중", smart_document_page)
            self.assertNotIn("sk-test-", smart_document_page)
        finally:
            conn = get_db()
            try:
                conn.execute("DELETE FROM admin_settings WHERE key IN ('ai_api_settings', 'openai_api_settings')")
                conn.commit()
            finally:
                conn.close()

    def test_csrf_and_generic_openai_error(self):
        self.client.get("/ai-agent")
        denied = self.client.post("/ai-agent/api/chat", json={"question": "최근 사진"})
        self.assertEqual(denied.status_code, 403)
        with self.client.session_transaction() as session:
            token = session["ai_agent_csrf_token"]
        # 통합관리에 등록된 키(및 예전 개인별 키의 1회성 fallback 값)가 없는 상태를 강제로 재현한다.
        empty_settings = {
            "api_key": "", "model": "gpt-5.6-luna", "source": "none", "provider": "openai",
            "preset_id": "1", "preset_label": "프리셋 1",
            "has_menu_key": False, "has_environment_key": False, "updated_by": "", "updated_at": "",
        }
        with patch("routes.openai_settings.get_ai_settings", return_value=empty_settings):
            failed = self.client.post(
                "/ai-agent/api/chat",
                json={"question": "최근 사진"},
                headers={"X-CSRF-Token": token},
            )
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(
            failed.get_json()["message"],
            "AI 응답을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        )

    def test_chat_returns_server_defined_payload(self):
        self.client.get("/ai-agent")
        with self.client.session_transaction() as session:
            token = session["ai_agent_csrf_token"]
        expected = {"type": "table", "title": "테스트", "message": "안전한 결과", "columns": [], "rows": []}
        with patch("routes.ai_agent.ask_ai_agent", return_value=expected):
            response = self.client.post(
                "/ai-agent/api/chat",
                json={"question": "테스트 질문"},
                headers={"X-CSRF-Token": token},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["answer"], expected)

    def test_real_read_only_tools_and_sensitive_field_exclusion(self):
        checks = [
            ("get_best_attendance", {"start_date": "2026-01-01", "end_date": "2026-12-31", "limit": 5, "position": None}),
            ("get_missing_weekly_reports", {"start_date": "2026-08-24", "end_date": "2026-08-30"}),
            ("get_expense_ranking", {"start_date": "2026-01-01", "end_date": "2026-06-30", "limit": 5}),
            ("get_contract_expirations", {"start_date": "2026-01-01", "end_date": "2027-12-31", "limit": 20}),
            ("get_incomplete_contracts", {"contract_system": "all", "limit": 20}),
            ("search_employees", {"keyword": None, "position": None, "limit": 10}),
            ("search_school_data", {"keyword": None, "limit": 10}),
            ("search_board_posts", {"keyword": "새담", "start_date": "2026-01-01", "end_date": "2026-12-31", "limit": 10}),
            ("search_gallery", {"keyword": None, "start_date": "2026-01-01", "end_date": "2026-12-31", "limit": 10}),
            ("search_documents", {"keyword": "자료", "start_date": "2026-01-01", "end_date": "2026-12-31", "limit": 10}),
        ]
        result_keys = set()

        def collect_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    result_keys.add(str(key).lower())
                    collect_keys(child)
            elif isinstance(value, list):
                for child in value:
                    collect_keys(child)

        with app.test_request_context("/ai-agent"):
            for name, arguments in checks:
                result = execute_tool(name, arguments, context())
                self.assertIsInstance(result.model_data, dict)
                self.assertIsInstance(result.display, dict)
                collect_keys(result.model_data)
        for forbidden in ("password", "rrn", "bank_account", "주민번호", "계좌번호"):
            self.assertNotIn(forbidden, result_keys)

    def test_tool_permission_cannot_be_bypassed_by_arguments(self):
        restricted = context(emp_no="sd05002", user_name="차승원", user_level=5,
                             menu_access={"contract_admin": False})
        with app.test_request_context("/ai-agent"):
            with self.assertRaises(ToolPermissionError):
                execute_tool("get_incomplete_contracts", {"contract_system": "all", "limit": 20}, restricted)

    def test_gallery_urls_match_existing_routes(self):
        with app.test_request_context("/ai-agent"):
            result = execute_tool(
                "search_gallery",
                {"keyword": None, "start_date": "2025-01-01", "end_date": "2026-12-31", "limit": 20},
                context(),
            )
        items = result.display["items"]
        self.assertTrue(items)
        adapter = app.url_map.bind("")
        for item in items:
            for key in ("thumbnail", "image_url"):
                endpoint, _values = adapter.match(item[key])
                self.assertIn(endpoint, {
                    "gall2.serve_thumb",
                    "gall2.serve_file",
                    "gall2.school_gallery_serve_thumb",
                    "gall2.school_gallery_serve_file",
                })

    def test_attendance_scope_matches_existing_level_rule(self):
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT a.emp_no,u.name FROM daily_attendance a JOIN users u ON u.emp_no=a.emp_no "
                "WHERE a.emp_no<>'admin' LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        restricted = context(emp_no=row["emp_no"], user_name=row["name"], user_level=5,
                             menu_access={"attendance_main": True})
        with app.test_request_context("/ai-agent"):
            result = execute_tool(
                "get_attendance_summary",
                {"start_date": "2026-01-01", "end_date": "2026-12-31", "employee_name": "admin"},
                restricted,
            )
        names = {item["name"] for item in result.model_data["records"]}
        self.assertTrue(not names or names == {row["name"]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
