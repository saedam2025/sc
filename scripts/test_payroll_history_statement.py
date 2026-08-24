"""스마트명세서 발송본 압축 보관·권한 열람 회귀 테스트."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from routes.payroll import (
    DEFAULT_FORMS,
    FORM_PRESETS,
    EXCEL_META_FORM,
    _archived_statement_html,
    _annotate_visual_texts,
    _apply_visual_text_edits,
    _build_message,
    _clean_field_mappings,
    _compress_statement_html,
    _decompress_statement_html,
    _extract_form_fields,
    _mapped_form_row,
    _mapping_preview_html,
    _safe_form_source,
    payroll_bp,
)


class PayrollHistoryStatementTest(unittest.TestCase):
    def test_excel_header_mapping_changes_render_values(self):
        form = {
            "field_mappings_json": '{"직원명":"수령인", "지급총액":"총지급액"}'
        }
        mapped = _mapped_form_row(
            {"수령인": "김새담", "총지급액": 3210000, "직원명": "기존값"},
            form,
        )
        self.assertEqual(mapped["직원명"], "김새담")
        self.assertEqual(mapped["지급총액"], 3210000)
        self.assertEqual(
            _clean_field_mappings({"직원명": " 수령인 "}),
            {"직원명": "수령인"},
        )

    def test_mapping_preview_marks_actual_form_values(self):
        source = "<p>성명 {{ row.get('직원명', '') }}</p><p>{{ safe_amount(row, '지급총액') }}</p>"
        fields = _extract_form_fields(source)
        rendered, preview_fields = _mapping_preview_html(source)
        self.assertEqual(
            [item["field_key"] for item in fields],
            ["직원명", "지급총액"],
        )
        self.assertEqual(fields, preview_fields)
        self.assertIn('data-field-key="직원명"', rendered)
        self.assertIn('data-field-key="지급총액"', rendered)
        self.assertIn('data-text-index="0"', rendered)
        self.assertIn("{{ safe_amount(row, &#x27;지급총액&#x27;) }}", rendered)

    def test_visual_label_edits_are_applied_without_editing_html(self):
        source = (
            "<style>.title{color:#123}</style>"
            "<table><tr><td>기타공제내역</td>"
            "<td>{{ safe_amount(row, '기타공제내역') }}</td></tr></table>"
        )
        annotated, count = _annotate_visual_texts(source)
        self.assertEqual(count, 1)
        self.assertIn('contenteditable="true"', annotated)
        self.assertNotIn('data-text-index', annotated.split('</style>')[0])
        edited = _apply_visual_text_edits(source, {"0": "추가 공제 & 조정"})
        self.assertIn("추가 공제 &amp; 조정", edited)
        self.assertIn("safe_amount(row, '기타공제내역')", edited)

    def test_saved_mapping_is_used_by_outgoing_message(self):
        source = "<p>{{ row.get('직원명', '') }} / {{ safe_amount(row, '지급총액') }}</p>"
        group = {
            "subject": "{{이름}} 명세서",
            "body_html": "{{명세서}}",
            "form_definitions": {
                "custom_test": {
                    "body_html": source,
                    "field_mappings_json": (
                        '{"직원명":"수령인", "이메일":"수신메일", '
                        '"지급총액":"총지급액"}'
                    ),
                }
            },
        }
        row = {
            EXCEL_META_FORM: "custom_test",
            "수령인": "김새담",
            "수신메일": "kim@example.com",
            "총지급액": 1234567,
        }
        message, target_name, archived = _build_message(
            row,
            group,
            {"email": "sender@example.com", "label": "발송자"},
            "unused",
            "2026-08-21",
            "https://intranet.example",
        )
        self.assertEqual(target_name, "김새담")
        self.assertEqual(message["To"], "kim@example.com")
        self.assertIn("1,234,567", archived)

    def test_default_statement_forms_can_be_archived(self):
        template_root = Path(__file__).resolve().parents[1] / "templates"
        group = {
            "banner1_value": "data:image/jpeg;base64,AA==",
            "banner1_asset_filename": "ad0013.jpg",
            "banner2_value": "https://example.com/ad.jpg",
            "logo_value": "https://example.com/logo.jpg",
        }
        for form in DEFAULT_FORMS.values():
            with self.subTest(form=form["filename"]):
                source = (template_root / form["filename"]).read_text(encoding="utf-8")
                archived = _archived_statement_html(
                    source,
                    {"직원명": "홍길동", "이메일": "hong@example.com"},
                    group,
                    "2026-08-21",
                    "https://intranet.example",
                )
                self.assertIn("홍길동", archived)
                self.assertNotIn("data:image", archived)

        for preset in FORM_PRESETS.values():
            with self.subTest(preset=preset["filename"]):
                source = (template_root / preset["filename"]).read_text(encoding="utf-8")
                self.assertEqual(_safe_form_source(source), source.strip())

    def test_archive_keeps_web_image_and_replaces_uploaded_image(self):
        source = """
            <div>
              <img src="{{ ad1_url }}" alt="첨부 광고">
              <img src="{{ ad2_url }}" alt="웹 광고">
              <strong>{{ row.get('name') }}</strong>
            </div>
        """
        group = {
            "banner1_value": "data:image/jpeg;base64,AA==",
            "banner1_asset_filename": "ad0013.jpg",
            "banner2_value": "https://example.com/ad.jpg",
            "logo_value": "",
        }

        archived = _archived_statement_html(
            source,
            {"name": "홍길동"},
            group,
            "2026-08-21",
            "https://intranet.example",
        )

        self.assertIn("ad0013.jpg첨부", archived)
        self.assertNotIn("data:image", archived)
        self.assertIn("https://example.com/ad.jpg", archived)
        compressed = _compress_statement_html(archived)
        self.assertLess(len(compressed), len(archived.encode("utf-8")))
        self.assertEqual(_decompress_statement_html(compressed), archived)

    def test_statement_endpoint_allows_owner_or_menu_user(self):
        with tempfile.TemporaryDirectory(prefix="payroll-statement-") as directory:
            database_path = Path(directory) / "test.db"
            conn = sqlite3.connect(database_path)
            conn.executescript("""
                CREATE TABLE payroll_campaigns (
                    id INTEGER PRIMARY KEY,
                    owner_emp_no TEXT NOT NULL,
                    subject TEXT NOT NULL
                );
                CREATE TABLE payroll_campaign_recipients (
                    id INTEGER PRIMARY KEY,
                    campaign_id INTEGER NOT NULL,
                    owner_emp_no TEXT NOT NULL,
                    recipient_name TEXT,
                    email TEXT,
                    status TEXT,
                    statement_html_zlib BLOB
                );
            """)
            conn.execute(
                "INSERT INTO payroll_campaigns VALUES (1, 'owner01', '8월 명세서')"
            )
            conn.execute(
                "INSERT INTO payroll_campaign_recipients VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    10,
                    1,
                    "owner01",
                    "홍길동",
                    "hong@example.com",
                    "sent",
                    _compress_statement_html("<p>발송 명세서</p>"),
                ),
            )
            conn.commit()
            conn.close()

            def open_connection():
                connection = sqlite3.connect(database_path)
                connection.row_factory = sqlite3.Row
                return connection

            app = Flask(
                __name__,
                root_path=str(Path(__file__).resolve().parents[1]),
            )
            app.secret_key = "test-secret"
            app.register_blueprint(payroll_bp, url_prefix="/payroll")
            client = app.test_client()

            with patch("routes.payroll._db", side_effect=open_connection):
                with client.session_transaction() as session:
                    session["emp_no"] = "owner01"
                response = client.get(
                    "/payroll/api/history/1/recipients/10/statement"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store, private")
                self.assertIn("발송 명세서", response.get_json()["statement"]["html"])

                with client.session_transaction() as session:
                    session["emp_no"] = "reviewer01"
                with patch("routes.payroll.has_menu_permission", return_value=True):
                    response = client.get(
                        "/payroll/api/history/1/recipients/10/statement"
                    )
                self.assertEqual(response.status_code, 200)

                with patch("routes.payroll.has_menu_permission", return_value=False):
                    response = client.get(
                        "/payroll/api/history/1/recipients/10/statement"
                    )
                self.assertEqual(response.status_code, 404)

                with client.session_transaction() as session:
                    session["emp_no"] = "owner01"
                    session["ai_mail_csrf_token"] = "csrf-test"
                response = client.get("/payroll/api/templates/presets")
                self.assertEqual(response.status_code, 200)
                presets = response.get_json()["presets"]
                self.assertEqual(
                    [preset["key"] for preset in presets],
                    ["classic", "landscape", "portrait"],
                )
                response = client.post(
                    "/payroll/api/templates/preview",
                    json={
                        "body_html": "<p>성명: {{ row.get('직원명', '') }}</p>",
                        "visual_text_edits": {"0": "수령인:"},
                        "mapping_mode": True,
                    },
                    headers={"X-CSRF-Token": "csrf-test"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    'data-field-key="직원명"',
                    response.get_json()["rendered_html"],
                )
                self.assertIn("수령인:", response.get_json()["rendered_html"])


if __name__ == "__main__":
    unittest.main()
