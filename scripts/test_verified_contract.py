"""인증전자계약 핵심 흐름 회귀 테스트.

실제 메일과 PDF 엔진은 호출하지 않고 발급·인증·서명·증거 저장 규칙을 검사한다.
"""

from __future__ import annotations

import base64
import io
import sqlite3
import tempfile
from pathlib import Path

from flask import Flask
from PIL import Image, ImageDraw
from openpyxl import Workbook

import routes.verified_contract as verified
from routes.verified_contract_repository import ensure_verified_contract_schema


def run() -> None:
    test_root = Path(tempfile.mkdtemp(prefix="verified_contract_test_"))
    database_path = test_root / "test.db"

    def test_db():
        conn = sqlite3.connect(database_path)
        conn.row_factory = sqlite3.Row
        return conn

    conn = test_db()
    ensure_verified_contract_schema(conn)
    conn.commit()
    conn.close()

    signature_root = test_root / "signatures"
    completed_root = test_root / "completed"
    stamp_root = test_root / "stamps"
    signature_root.mkdir()
    completed_root.mkdir()
    stamp_root.mkdir()

    verified.get_db = test_db
    verified.VERIFIED_SIGNATURE_ROOT = signature_root
    verified.VERIFIED_CONTRACTS_ROOT = completed_root
    verified.VERIFIED_STAMP_ROOT = stamp_root
    verified.VERIFIED_MAIL_FILE = test_root / "mail_settings.json"
    verified.VERIFIED_COMPANY_FILE = test_root / "company_settings.json"
    verified.load_credential_secret = lambda: "verified-contract-test-credential-key"
    verified._save_json(
        verified.VERIFIED_MAIL_FILE,
        {
            "active_account_id": "mail-1",
            "accounts": [
                {
                    "id": "mail-1",
                    "label": "기본 계약메일",
                    "email": "contracts@example.com",
                    "encrypted_password": verified._encrypt_sensitive("test-app-password"),
                }
            ],
        },
    )
    verified._save_json(
        verified.VERIFIED_COMPANY_FILE,
        {
            "active_profile_id": "company-1",
            "profiles": [
                {
                    "id": f"company-{number}",
                    "label": f"회사 {number}",
                    "company_name": f"테스트회사 {number}",
                    "representative_title": "대표",
                    "representative_name": "김대표",
                    "stamp_filename": "",
                }
                for number in range(1, 4)
            ],
        },
    )
    delivered_mail = []
    verified._send_mail = (
        lambda to, subject, contents, attachments=None: delivered_mail.append(
            (to, subject, attachments, contents)
        )
    )
    verified.secrets.randbelow = lambda maximum: 123456

    def fake_pdf(row, contract_data, company, signature_uri, signed_at):
        assert contract_data["주민번호"] == "900101-1234567"
        assert contract_data["은행"] == "테스트은행"
        assert contract_data["계좌번호"] == "123-456-789012"
        path = completed_root / f"verified_contract_{row['id']}.pdf"
        path.write_bytes(b"%PDF-1.4\nverified-contract-test\n%%EOF")
        return path

    verified._build_pdf = fake_pdf

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).resolve().parent.parent / "templates"),
        static_folder=str(Path(__file__).resolve().parent.parent / "static"),
    )
    app.secret_key = "verified-contract-test"
    class DenyMenu(dict):
        def get(self, _key, _default=None):
            return False

    app.context_processor(
        lambda: {
            "global_theme": "light",
            "current_user_level": 1,
            "menu_access": DenyMenu(),
        }
    )
    app.register_blueprint(
        verified.verified_contract_bp,
        url_prefix="/verified-contract",
    )
    app.add_url_rule("/logout", endpoint="logout", view_func=lambda: "")
    app.jinja_env.get_template("verified_contract/admin.html")
    app.jinja_env.get_template("verified_contract/public.html")
    client = app.test_client()
    with client.session_transaction() as flask_session:
        flask_session.update(
            {
                "emp_no": "admin",
                "user_name": "admin",
                "user_level": 1,
                "verified_contract_csrf": "csrf-test",
            }
        )

    admin_html = (
        Path(__file__).resolve().parent.parent
        / "templates"
        / "verified_contract"
        / "admin.html"
    ).read_text(encoding="utf-8")
    assert "새담 인재 인증계약관리 시스템" in admin_html
    assert "location.href='/verified-contract/admin/settings'" in admin_html
    assert "ckeditor.com" not in admin_html.lower()
    admin_response = client.get("/verified-contract/admin")
    assert admin_response.status_code == 200
    rendered_admin = admin_response.get_data(as_text=True)
    assert 'id="termsForm"' not in rendered_admin
    assert "/verified-contract/admin/settings" in rendered_admin
    settings_response = client.get("/verified-contract/admin/settings")
    assert settings_response.status_code == 200
    settings_html = settings_response.get_data(as_text=True)
    assert "인증계약 양식관리" in settings_html
    assert "발송메일계정" in settings_html
    assert "발송회사" in settings_html
    assert "발송계정 추가" in settings_html
    assert "발송회사 추가" in settings_html

    response = client.post(
        "/verified-contract/admin/settings/mail",
        json={
            "action": "add",
            "label": "추가 계약메일",
            "email": "second@example.com",
            "password": "second app password",
        },
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    mail_file_text = verified.VERIFIED_MAIL_FILE.read_text(encoding="utf-8")
    assert "secondapppassword" not in mail_file_text
    mail_store = verified._mail_account_store()
    assert len(mail_store["accounts"]) == 2
    second_mail_id = mail_store["active_account_id"]
    response = client.post(
        "/verified-contract/admin/settings/mail",
        json={
            "action": "save",
            "account_id": second_mail_id,
            "label": "수정된 계약메일",
            "email": "updated@example.com",
            "password": "",
        },
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    response = client.post(
        "/verified-contract/admin/settings/mail",
        json={"action": "select", "account_id": "mail-1"},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    assert verified._mail_account_store()["active_account_id"] == "mail-1"
    response = client.post(
        "/verified-contract/admin/settings/mail",
        json={"action": "delete", "account_id": second_mail_id},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    assert len(verified._mail_account_store()["accounts"]) == 1

    response = client.post(
        "/verified-contract/admin/settings/company",
        data={
            "action": "add",
            "profile_id": "company-1",
            "label": "회사 4",
            "company_name": "테스트회사 4",
            "representative_title": "대표",
            "representative_name": "이대표",
        },
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    assert len(verified._company_settings()["profiles"]) == 4
    added_company_id = verified._company_settings()["active_profile_id"]
    response = client.post(
        "/verified-contract/admin/settings/company",
        data={
            "action": "save",
            "profile_id": "company-1",
            "label": "수정된 회사 1",
            "company_name": "수정된 테스트회사",
            "representative_title": "이사장",
            "representative_name": "박대표",
        },
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    assert verified._company_profile("company-1")["company_name"] == "수정된 테스트회사"
    response = client.post(
        "/verified-contract/admin/settings/company",
        data={"action": "select", "profile_id": added_company_id},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    response = client.post(
        "/verified-contract/admin/settings/company",
        data={"action": "delete", "profile_id": added_company_id},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    assert len(verified._company_settings()["profiles"]) == 3

    signer_name = "홍길동"
    response = client.post(
        "/verified-contract/admin/create",
        json={
            "contract_type": verified._categories()[0],
            "company_profile_id": verified._company_settings()["active_profile_id"],
            "signer_name": signer_name,
            "signer_email": "hong@example.com",
            "school_name": "새담학교",
            "department": "수학",
            "expires_days": 7,
        },
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    invitation = response.get_json()
    assert invitation["status"] == "success", invitation
    token = invitation["invitation_url"].rsplit("/", 1)[-1]

    response = client.get(f"/verified-contract/sign/{token}")
    assert response.status_code == 200
    assert "sendCode()" in response.get_data(as_text=True)

    response = client.post(
        f"/verified-contract/sign/{token}/send-code",
        json={},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    response = client.post(
        f"/verified-contract/sign/{token}/verify-code",
        json={"code": "223456"},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    response = client.get(f"/verified-contract/sign/{token}")
    sign_page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "주민등록번호 *" in sign_page
    assert "은행 *" in sign_page
    assert "계좌번호 *" in sign_page
    assert "위변조 확인용 고유번호" in sign_page

    signature = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
    ImageDraw.Draw(signature).line(
        (20, 60, 100, 20, 180, 70, 260, 30),
        fill=(0, 0, 0, 255),
        width=4,
    )
    buffer = io.BytesIO()
    signature.save(buffer, format="PNG")
    signature_data = (
        "data:image/png;base64,"
        + base64.b64encode(buffer.getvalue()).decode("ascii")
    )
    response = client.post(
        f"/verified-contract/sign/{token}/complete",
        json={
            "agreements": {item["key"]: True for item in verified.AGREEMENTS},
            "confirmed_name": signer_name,
            "phone": "010-1234-5678",
            "address": "서울시 테스트구",
            "resident_number": "900101-1234567",
            "bank_name": "테스트은행",
            "account_number": "123-456-789012",
            "signature": signature_data,
        },
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()

    conn = test_db()
    contract = conn.execute("SELECT * FROM verified_contracts").fetchone()
    events = {
        row["event_type"]
        for row in conn.execute(
            "SELECT event_type FROM verified_contract_events ORDER BY id"
        ).fetchall()
    }
    conn.close()
    assert contract["status"] == "completed"
    assert len(contract["pdf_sha256"]) == 64
    assert contract["signer_rrn_encrypted"] != "900101-1234567"
    assert contract["signer_bank_encrypted"] != "테스트은행"
    assert contract["signer_account_encrypted"] != "123-456-789012"
    assert verified._decrypt_sensitive(contract["signer_rrn_encrypted"]) == "900101-1234567"
    assert verified._decrypt_sensitive(contract["signer_bank_encrypted"]) == "테스트은행"
    assert verified._decrypt_sensitive(contract["signer_account_encrypted"]) == "123-456-789012"
    assert "900101-1234567" not in contract["contract_data_json"]
    assert {
        "CREATED",
        "INVITATION_SENT",
        "LINK_OPENED",
        "OTP_SENT",
        "OTP_VERIFIED",
        "COMPLETED",
        "COMPLETION_MAIL_SENT",
    }.issubset(events)
    assert client.get(f"/verified-contract/sign/{token}/download").status_code == 200
    assert client.get("/verified-contract/admin/excel-template").status_code == 200

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "계약구분",
            "성명",
            "email",
            "수탁학교명",
            "부서명",
            "계약기간",
        ]
    )
    for number in range(1, 26):
        sheet.append(
            [
                verified._categories()[0],
                f"강사{number}",
                f"teacher{number}@example.com",
                "새담초등학교",
                f"과목{number}",
                "2026.03.01 ~ 2027.02.28",
            ]
        )
    excel_buffer = io.BytesIO()
    workbook.save(excel_buffer)
    excel_buffer.seek(0)
    response = client.post(
        "/verified-contract/admin/upload-excel",
        data={"excel_file": (excel_buffer, "contracts.xlsx")},
        headers={"X-CSRF-Token": "csrf-test"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["inserted"] == 25

    conn = test_db()
    draft_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM verified_contracts WHERE status='draft' ORDER BY id"
        ).fetchall()
    ]
    conn.close()
    assert len(draft_ids) == 25

    class FakeSMTP:
        def __init__(self, username, password):
            self.username = username

        def send(self, to, subject, contents, attachments=None):
            delivered_mail.append((to, subject, attachments, contents))

    verified.yagmail.SMTP = FakeSMTP
    response = client.post(
        "/verified-contract/admin/bulk-send",
        json={"ids": draft_ids, "expires_days": 7},
        headers={"X-CSRF-Token": "csrf-test"},
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["sent"] == 25
    assert (
        client.get(
            f"/verified-contract/admin/download-selected?ids={contract['id']}"
        ).status_code
        == 200
    )
    assert len(delivered_mail) == 28
    assert any(
        "계약서 위변조 확인용 고유번호" in str(mail[3])
        for mail in delivered_mail
    )
    print("VERIFIED_CONTRACT_FLOW_OK")


if __name__ == "__main__":
    run()
