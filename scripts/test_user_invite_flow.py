"""Regression checks for invitation signup password, completion, and mail guidance."""

import email
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from routes import database, user_mgmt


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _valid_rrn():
    first_twelve = "900101123456"
    weights = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
    checksum = (11 - sum(int(number) * weight for number, weight in zip(first_twelve, weights)) % 11) % 10
    digits = first_twelve + str(checksum)
    return f"{digits[:6]}-{digits[6:]}"


class FakeSMTP:
    messages = []

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def starttls(self):
        pass

    def login(self, *_args):
        pass

    def sendmail(self, _sender, _target, message):
        self.messages.append(message)

    def quit(self):
        pass


def _decoded_message(raw_message):
    parsed = email.message_from_string(raw_message)
    return "\n".join(
        part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8")
        for part in parsed.walk()
        if part.get_content_type() in {"text/plain", "text/html"}
    )


def main():
    assert user_mgmt._is_valid_signup_password("Abcdefgh1!xy")
    assert not user_mgmt._is_valid_signup_password("Abcdefgh1!xyz")

    with tempfile.TemporaryDirectory(prefix="saedam-invite-test-") as directory:
        db_path = Path(directory) / "invite-test.db"
        source = database.get_db()
        target = _connect(db_path)
        source.backup(target)
        source.close()
        target.close()

        user_mgmt.get_db = lambda: _connect(db_path)
        app_module.record_page_usage = lambda *_args, **_kwargs: False
        token = "invite-regression-token"
        target_email = "invite-regression@example.test"
        conn = _connect(db_path)
        user_mgmt._ensure_user_invite_schema(conn)
        conn.execute("DELETE FROM users WHERE email=?", (target_email,))
        conn.execute("DELETE FROM user_invites WHERE email=?", (target_email,))
        conn.execute(
            """INSERT INTO user_invites (token_hash,email,status,sent_at)
               VALUES (?,?,'sent',CURRENT_TIMESTAMP)""",
            (user_mgmt._invite_token_hash(token), target_email),
        )
        conn.commit()
        conn.close()

        client = app_module.app.test_client()
        with client.session_transaction() as login_session:
            login_session["emp_no"] = "admin"
            login_session["user_name"] = "admin"
            login_session["user_level"] = 0

        conn = _connect(db_path)
        user_mgmt._ensure_sender_schema(conn)
        conn.execute(
            """INSERT OR IGNORE INTO ai_mail_senders
               (owner_emp_no,label,email,encrypted_app_password,is_active,last_test_status)
               VALUES ('admin','초대 테스트 계정','invite-sender@example.test','encrypted-test',1,'success')"""
        )
        sender_id = conn.execute(
            "SELECT id FROM ai_mail_senders WHERE owner_emp_no='admin' AND email='invite-sender@example.test'"
        ).fetchone()["id"]
        conn.commit()
        conn.close()

        sender_page = client.get("/user/invite-sender")
        sender_page_html = sender_page.get_data(as_text=True)
        assert sender_page.status_code == 200
        assert 'id="senderPanel" class="invite-sender-panel" hidden' in sender_page_html
        assert "sender_id: selectedSenderId" in sender_page_html

        sender_list = client.get("/user/invite_senders", headers={"Accept": "application/json"})
        assert sender_list.status_code == 200
        listed_sender = next(
            item for item in sender_list.get_json()["senders"] if item["id"] == sender_id
        )
        assert listed_sender["email"] == "invite-sender@example.test"
        assert "encrypted_app_password" not in listed_sender

        selected_mail = {}
        original_send_real_email = user_mgmt.send_real_email
        user_mgmt.send_real_email = lambda *args, **kwargs: selected_mail.update(
            {"args": args, "sender": kwargs.get("sender")}
        ) or True
        try:
            selected_response = client.post(
                "/user/send_invite",
                json={
                    "email": "selected-invite@example.test",
                    "subject": "가입 초대",
                    "body": "가입 신청 안내",
                    "sender_id": sender_id,
                },
            )
        finally:
            user_mgmt.send_real_email = original_send_real_email
        assert selected_response.status_code == 200
        assert selected_mail["sender"]["id"] == sender_id
        assert selected_mail["sender"]["email"] == "invite-sender@example.test"

        conn = _connect(db_path)
        saved_invite = conn.execute(
            "SELECT sender_id,sender_email FROM user_invites WHERE email='selected-invite@example.test'"
        ).fetchone()
        selected_setting = conn.execute(
            "SELECT value FROM admin_settings WHERE key=?",
            (user_mgmt._invite_sender_setting_key("admin"),),
        ).fetchone()
        conn.close()
        assert saved_invite["sender_id"] == sender_id
        assert saved_invite["sender_email"] == "invite-sender@example.test"
        assert selected_setting["value"] == str(sender_id)

        invite_page = client.get(f"/user/invite_page/{token}")
        invite_html = invite_page.get_data(as_text=True)
        assert invite_page.status_code == 200
        assert invite_html.count('maxlength="12"') >= 2
        assert "showRegistrationComplete" in invite_html
        assert "www.saedam.org" in invite_html

        common_data = {
            "name": "초대회귀테스트",
            "password_confirm": "Abcdefgh1!xy",
            "rrn": _valid_rrn(),
            "email": target_email,
            "department": "본부",
            "position": "미지정",
            "privacy_security_consent": "1",
            "invite_token": token,
        }
        too_long = dict(common_data, password="Abcdefgh1!xyz", password_confirm="Abcdefgh1!xyz")
        rejected = client.post("/user/register", data=too_long)
        assert rejected.status_code == 400 and "12자 이내" in rejected.get_json()["message"]

        accepted = client.post(
            "/user/register",
            data=dict(common_data, password="Abcdefgh1!xy"),
        )
        assert accepted.status_code == 200
        assert accepted.get_json()["message"] == "가입 신청이 완료되었습니다."

    original_smtp = user_mgmt.smtplib.SMTP
    user_mgmt.smtplib.SMTP = FakeSMTP
    try:
        FakeSMTP.messages.clear()
        assert user_mgmt.send_real_email(
            "test@example.com",
            "https://example.com/invite",
            "가입 초대",
            "가입 신청 안내",
        )
        assert user_mgmt.send_membership_result_email(
            "test@example.com", "테스트", True, "sd05001", "사원", "본부"
        )
        assert len(FakeSMTP.messages) == 2
        assert all("www.saedam.org" in _decoded_message(message) for message in FakeSMTP.messages)
        approval_message = _decoded_message(FakeSMTP.messages[-1])
        assert "접속방법: 새담 홈페이지 http://www.saedam.org 접속 후 인트라넷 메뉴로 접속가능." in approval_message
        assert "인트라넷 주소: https://works.saedam.org" in approval_message
    finally:
        user_mgmt.smtplib.SMTP = original_smtp

    print("User invitation flow test: PASS")


if __name__ == "__main__":
    main()
