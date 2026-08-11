import io
import os
import sqlite3
import sys
import tempfile
import unittest
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flask import Flask

from routes import document
from routes.database import ensure_certificate_schema


class CertificateManagementFlowTest(unittest.TestCase):
    def setUp(self):
        descriptor, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(descriptor)

        def test_db():
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            return connection

        self.original_get_db = document.get_db
        self.original_seal_folder = document.CERT_SEAL_FOLDER
        self.original_logo_folder = document.CERT_LOGO_FOLDER
        self.asset_directory = tempfile.TemporaryDirectory()
        document.CERT_SEAL_FOLDER = os.path.join(self.asset_directory.name, "seals")
        document.CERT_LOGO_FOLDER = os.path.join(self.asset_directory.name, "logos")
        os.makedirs(document.CERT_SEAL_FOLDER, exist_ok=True)
        os.makedirs(document.CERT_LOGO_FOLDER, exist_ok=True)
        document.get_db = test_db
        connection = test_db()
        connection.execute("""
            CREATE TABLE ai_mail_senders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_emp_no TEXT NOT NULL,
                label TEXT NOT NULL,
                email TEXT NOT NULL,
                encrypted_app_password TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_tested_at DATETIME,
                last_test_status TEXT,
                last_test_error TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                provider TEXT NOT NULL DEFAULT 'gmail',
                smtp_host TEXT,
                smtp_port INTEGER,
                smtp_security TEXT,
                smtp_username TEXT
            )
        """)
        ensure_certificate_schema(connection)
        self.sender_id = connection.execute("""
            INSERT INTO ai_mail_senders (
                owner_emp_no, label, email, encrypted_app_password,
                provider, smtp_host, smtp_port, smtp_security, smtp_username
            ) VALUES ('admin', '테스트 발송', 'sender@example.com', 'encrypted',
                      'gmail', 'smtp.gmail.com', 465, 'ssl', 'sender@example.com')
        """).lastrowid
        connection.commit()
        connection.close()

        self.app = Flask(
            __name__,
            template_folder=str(ROOT / "templates"),
            static_folder=str(ROOT / "static"),
        )
        self.app.secret_key = "certificate-management-test-secret"
        self.app.register_blueprint(document.document_bp, url_prefix="/document")
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["emp_no"] = "admin"
            session["user_name"] = "admin"
            session["user_level"] = 1
            session["ai_mail_csrf_token"] = "csrf-test"

    def tearDown(self):
        document.get_db = self.original_get_db
        document.CERT_SEAL_FOLDER = self.original_seal_folder
        document.CERT_LOGO_FOLDER = self.original_logo_folder
        self.asset_directory.cleanup()
        try:
            os.remove(self.db_path)
        except FileNotFoundError:
            pass

    @property
    def headers(self):
        return {"X-CSRF-Token": "csrf-test"}

    def test_company_workgroup_link_and_request_snapshot(self):
        response = self.client.post(
            "/document/api/companies",
            data={
                "company_name": "테스트 주식회사",
                "representative_name": "홍대표",
                "business_number": "123-45-67890",
                "address": "서울시 테스트구",
                "phone": "02-123-4567",
                "seal": (io.BytesIO(b"seal-image"), "회사 도장 파일.png"),
                "logo": (io.BytesIO(b"logo-image"), "회사 로고 파일.png"),
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        company_id = response.get_json()["company_id"]

        response = self.client.post(
            "/document/api/workgroups",
            json={
                "name": "테스트 발급그룹",
                "company_id": company_id,
                "sender_id": self.sender_id,
                "allow_instructor": True,
                "allow_employee": True,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)

        settings = self.client.get("/document/api/settings").get_json()
        company = settings["companies"][0]
        self.assertEqual(company["seal_filename"], "회사 도장 파일.png")
        self.assertEqual(company["logo_filename"], "회사 로고 파일.png")
        self.assertTrue(company["seal_path"].endswith(".png"))
        self.assertTrue(company["logo_path"].endswith(".png"))
        self.assertNotIn("회사 도장", os.path.basename(company["seal_path"]))
        old_logo_url = company["logo_url"]
        old_seal_url = company["seal_url"]

        response = self.client.post(
            f"/document/api/companies/{company_id}",
            data={
                "company_name": "테스트 주식회사",
                "representative_name": "홍대표",
                "business_number": "123-45-67890",
                "address": "서울시 테스트구",
                "phone": "02-123-4567",
                "seal": (io.BytesIO(b"new-seal-image"), "회사 도장 파일.png"),
                "logo": (io.BytesIO(b"new-logo-image"), "회사 로고 파일.png"),
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        settings = self.client.get("/document/api/settings").get_json()
        company = settings["companies"][0]
        self.assertNotEqual(company["logo_url"], old_logo_url)
        self.assertNotEqual(company["seal_url"], old_seal_url)
        self.assertIn("?v=", company["logo_url"])
        self.assertIn("?v=", company["seal_url"])

        group = settings["workgroups"][0]
        with self.client.session_transaction() as session:
            session.clear()

        auth_response = self.client.get(group["instructor_path"])
        self.assertEqual(auth_response.status_code, 200)
        auth_page = auth_response.get_data(as_text=True)
        self.assertIn("테스트 주식회사", auth_page)
        self.assertIn("02-123-4567", auth_page)
        self.assertIn(company["logo_url"], auth_page)

        with self.client.session_transaction() as session:
            session["certificate_apply_verified"] = document.CERTIFICATE_FORM_AUTH_TOKEN

        response = self.client.get(group["instructor_path"])
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("테스트 주식회사", page)
        self.assertIn("02-123-4567", page)
        self.assertIn(company["logo_url"], page)

        logo_response = self.client.get(company["logo_url"])
        self.assertEqual(logo_response.status_code, 200)
        self.assertEqual(logo_response.data, b"new-logo-image")
        self.assertIn("no-store", logo_response.headers.get("Cache-Control", ""))
        logo_response.close()

        response = self.client.post(
            group["instructor_path"],
            data={
                "신청구분": "강사",
                "증명서종류": "경력증명서",
                "성명": "신청테스터",
                "이메일주소": "applicant@example.com",
            },
        )
        self.assertEqual(response.status_code, 200)
        connection = document.get_db()
        row = connection.execute("""
            SELECT workgroup_id, company_id, workgroup_name, company_name
            FROM certificate_requests ORDER BY id DESC LIMIT 1
        """).fetchone()
        connection.close()
        self.assertEqual(row["workgroup_id"], group["id"])
        self.assertEqual(row["company_id"], company_id)
        self.assertEqual(row["company_name"], "테스트 주식회사")
        self.assertEqual(row["workgroup_name"], "테스트 발급그룹")

    def test_zeptomail_uses_encoded_from_header_and_explicit_envelope_sender(self):
        class FakeSmtp:
            def __init__(self):
                self.message = None
                self.from_addr = None
                self.closed = False

            def send_message(self, message, from_addr=None, **_kwargs):
                self.message = message
                self.from_addr = from_addr

            def quit(self):
                self.closed = True

            def close(self):
                self.closed = True

        smtp = FakeSmtp()
        sender = {
            "provider": "zeptomail",
            "label": "한글 증명서 발송 <certificate@saedam.org>",
            "email": "certificate@saedam.org",
        }
        message = MIMEText("테스트", "plain", "utf-8")
        message["From"] = document._sender_from_header(sender)
        message["To"] = "recipient@example.com"
        message["Subject"] = "증명서 테스트"

        with mock.patch.object(document, "_smtp_login_for_sender", return_value=smtp), \
             mock.patch.object(document, "_verify_smtp_sender"):
            document._send_registered_message(sender, message)

        self.assertEqual(smtp.from_addr, "certificate@saedam.org")
        self.assertEqual(parseaddr(str(smtp.message["From"]))[1], "certificate@saedam.org")
        self.assertTrue(smtp.closed)

if __name__ == "__main__":
    unittest.main()
