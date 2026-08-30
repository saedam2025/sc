"""인사관리 일괄수정과 명단 UI 회귀 검사."""

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


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    with tempfile.TemporaryDirectory(prefix="saedam-user-bulk-test-") as directory:
        db_path = Path(directory) / "bulk-test.db"
        source = database.get_db()
        target = _connect(db_path)
        source.backup(target)
        source.close()
        target.close()

        original_get_db = user_mgmt.get_db
        user_mgmt.get_db = lambda: _connect(db_path)
        app_module.record_page_usage = lambda *_args, **_kwargs: False
        try:
            conn = _connect(db_path)
            user_mgmt._ensure_hr_schema(conn)
            conn.execute("DELETE FROM users WHERE emp_no LIKE 'bulk-test-%'")
            rows = [
                ('bulk-test-1', '일괄수정가', '승인'),
                ('bulk-test-2', '일괄수정나', '승인'),
                ('bulk-test-pending', '일괄수정대기', '대기'),
            ]
            for emp_no, name, status in rows:
                conn.execute(
                    """INSERT INTO users
                       (emp_no,name,password,status,department,custom_department,custom_team,position,level)
                       VALUES (?,?,?,?,'본부','기존소속','기존팀','사원',5)""",
                    (emp_no, name, 'test-password', status),
                )
            conn.commit()
            ids = {
                row['emp_no']: row['id']
                for row in conn.execute("SELECT id,emp_no FROM users WHERE emp_no LIKE 'bulk-test-%'")
            }
            conn.close()

            client = app_module.app.test_client()
            with client.session_transaction() as login_session:
                login_session['emp_no'] = 'admin'
                login_session['user_name'] = 'admin'
                login_session['user_level'] = 0

            page = client.get('/user/')
            html = page.get_data(as_text=True)
            assert page.status_code == 200
            assert 'id="memberSelectAll"' in html
            assert 'id="memberBulkField"' in html
            assert 'autocomplete="off"' in html
            assert "window.addEventListener('pageshow', clearMemberSearch)" in html
            assert 'data-member-sort="사번"' in html
            assert 'data-member-sort="이름"' in html
            node_executable = os.environ.get('CODEX_NODE_EXE')
            if node_executable:
                management_script = next(
                    script for script in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
                    if 'MEMBER_SORT_COLUMNS' in script
                )
                syntax_check = subprocess.run(
                    [node_executable, '--check'], input=management_script,
                    text=True, encoding='utf-8', capture_output=True, check=False,
                )
                assert syntax_check.returncode == 0, syntax_check.stderr

            approved_ids = [ids['bulk-test-1'], ids['bulk-test-2']]
            invalid_department = client.post('/user/bulk_update', json={
                'user_ids': approved_ids, 'field': 'department', 'value': '없는부서',
            })
            assert invalid_department.status_code == 400

            mixed_status = client.post('/user/bulk_update', json={
                'user_ids': [ids['bulk-test-1'], ids['bulk-test-pending']],
                'field': 'department', 'value': '파견',
            })
            assert mixed_status.status_code == 400

            department_update = client.post('/user/bulk_update', json={
                'user_ids': approved_ids, 'field': 'department', 'value': '북부지점',
            })
            assert department_update.status_code == 200
            assert department_update.get_json()['updated_count'] == 2

            custom_update = client.post('/user/bulk_update', json={
                'user_ids': approved_ids, 'field': 'custom_department', 'value': '교육사업본부',
            })
            assert custom_update.status_code == 200

            clear_team = client.post('/user/bulk_update', json={
                'user_ids': approved_ids, 'field': 'custom_team', 'value': '',
            })
            assert clear_team.status_code == 200

            conn = _connect(db_path)
            updated = conn.execute(
                "SELECT department,custom_department,custom_team FROM users WHERE id IN (?,?) ORDER BY id",
                approved_ids,
            ).fetchall()
            pending = conn.execute(
                "SELECT department FROM users WHERE id=?", (ids['bulk-test-pending'],)
            ).fetchone()
            conn.close()
            assert all(row['department'] == '북부지점' for row in updated)
            assert all(row['custom_department'] == '교육사업본부' for row in updated)
            assert all(row['custom_team'] == '' for row in updated)
            assert pending['department'] == '본부'
        finally:
            user_mgmt.get_db = original_get_db

    print('User bulk management test: PASS')


if __name__ == '__main__':
    main()
