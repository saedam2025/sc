"""로그인 사번/임시 비밀번호 찾기와 자동저장 차단 회귀 검사."""

import email
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from routes import database, user_mgmt
from routes.security import hash_password, verify_password


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _post(client, payload, ip_address):
    return client.post(
        '/account-recovery', json=payload,
        environ_overrides={'REMOTE_ADDR': ip_address},
    )


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


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-login-recovery-test-') as directory:
        db_path = Path(directory) / 'login-recovery.db'
        source = database.get_db()
        target = _connect(db_path)
        source.backup(target)
        source.close()
        target.close()

        original_get_db = app_module.get_db
        original_send = app_module.send_account_recovery_email
        app_module.get_db = lambda: _connect(db_path)
        app_module._account_recovery_attempts.clear()
        try:
            conn = _connect(db_path)
            conn.execute("DELETE FROM users WHERE emp_no='recovery-test-1'")
            original_password = 'Original!123'
            cursor = conn.execute(
                """INSERT INTO users
                   (emp_no,name,password,position,level,rrn,email,status,department)
                   VALUES ('recovery-test-1','복구테스트',?,'사원',5,
                           '900101-1234567','recovery@example.test','승인','본부')""",
                (hash_password(original_password),),
            )
            user_id = cursor.lastrowid
            conn.commit()
            conn.close()

            client = app_module.app.test_client()
            page = client.get('/login_page')
            html = page.get_data(as_text=True)
            assert page.status_code == 200
            assert 'id="accountRecoveryTrigger"' in html
            assert 'id="accountRecoveryPanel"' in html
            assert '<div class="widget-content">사번/비번찾기</div>' in html
            assert '사번/비번찾기<small>클릭하세요!' not in html
            assert "getBoundingClientRect().top" in html
            assert "--login-anchor-top" in html
            assert '.account-recovery-panel.open.shadow-ready' in html
            assert "panel.classList.add('shadow-ready')" in html
            assert "if (panel.contains(document.activeElement)) trigger.focus({preventScroll:true})" in html
            assert "document.body.classList.remove('recovery-open')" in html
            assert "fetch('/account-recovery'" in html
            assert 'name="saedam_login_password_no_store"' in html
            assert 'autocomplete="off"' in html
            assert 'data-lpignore="true"' in html
            assert "window.addEventListener('pageshow', resetLoginPassword)" in html

            node_executable = os.environ.get('CODEX_NODE_EXE')
            if node_executable:
                login_script = next(
                    script for script in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
                    if 'submitAccountRecovery' in script
                )
                syntax_check = subprocess.run(
                    [node_executable, '--check'], input=login_script,
                    text=True, encoding='utf-8', capture_output=True, check=False,
                )
                assert syntax_check.returncode == 0, syntax_check.stderr

            missing = _post(
                client, {'name': '없는회원', 'rrn': '900101-1234567'}, '10.0.0.1'
            )
            assert missing.status_code == 404
            assert '가입 된 내용이 존재하지 않습니다' in missing.get_json()['message']

            app_module.send_account_recovery_email = lambda *_args: False
            failed_mail = _post(
                client, {'name': '복구테스트', 'rrn': '900101-1234567'}, '10.0.0.2'
            )
            assert failed_mail.status_code == 503
            conn = _connect(db_path)
            stored_after_failure = conn.execute(
                'SELECT password FROM users WHERE id=?', (user_id,)
            ).fetchone()['password']
            conn.close()
            assert verify_password(stored_after_failure, original_password)

            sent = {}
            app_module.send_account_recovery_email = lambda email, name, emp_no, password: sent.update({
                'email': email, 'name': name, 'emp_no': emp_no, 'password': password,
            }) or True
            success = _post(
                client, {'name': '복구테스트', 'rrn': '9001011234567'}, '10.0.0.3'
            )
            result = success.get_json()
            assert success.status_code == 200
            assert result['name'] == '복구테스트'
            assert result['email'] == 'recovery@example.test'
            assert sent['emp_no'] == 'recovery-test-1'
            assert len(sent['password']) == 12

            conn = _connect(db_path)
            stored_after_success = conn.execute(
                'SELECT password FROM users WHERE id=?', (user_id,)
            ).fetchone()['password']
            conn.close()
            assert verify_password(stored_after_success, sent['password'])
            assert not verify_password(stored_after_success, original_password)
            assert stored_after_success != sent['password']

            app_module._account_recovery_attempts.clear()
            for _ in range(app_module.ACCOUNT_RECOVERY_MAX_ATTEMPTS):
                response = _post(
                    client, {'name': '없는회원', 'rrn': '900101-1234567'}, '10.0.0.9'
                )
                assert response.status_code == 404
            limited = _post(
                client, {'name': '없는회원', 'rrn': '900101-1234567'}, '10.0.0.9'
            )
            assert limited.status_code == 429
        finally:
            app_module.get_db = original_get_db
            app_module.send_account_recovery_email = original_send
            app_module._account_recovery_attempts.clear()

    original_smtp = user_mgmt.smtplib.SMTP
    user_mgmt.smtplib.SMTP = FakeSMTP
    try:
        FakeSMTP.messages.clear()
        assert user_mgmt.send_account_recovery_email(
            'recovery@example.test', '복구테스트', 'sd01001', 'Sd!A2bcdefg'
        )
        parsed = email.message_from_string(FakeSMTP.messages[-1])
        html_body = next(
            part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8')
            for part in parsed.walk() if part.get_content_type() == 'text/html'
        )
        assert '로그인 후 개인 프로필 수정에서 비밀번호를 반드시 변경해주세요.' in html_body
        assert '인사관리의 개인 프로필' not in html_body
        assert 'recovery-password-warning' in html_body
        assert 'recoveryPasswordWarningBlink' in html_body
        assert 'https://www.saedam.org/img/logo01.gif' in html_body
    finally:
        user_mgmt.smtplib.SMTP = original_smtp

    print('Login account recovery test: PASS')


if __name__ == '__main__':
    main()
