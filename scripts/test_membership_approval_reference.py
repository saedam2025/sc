"""가입 승인 결과 메일과 전자결재 참조함 회귀 검사."""

import sqlite3
import sys
import tempfile
from pathlib import Path

from flask import Flask, jsonify

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import approval, database, user_mgmt


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _copy_database(target_path):
    source = database.get_db()
    target = _connect(target_path)
    source.backup(target)
    source.close()
    target.close()


def _session(client, *, emp_no, name, level):
    with client.session_transaction() as session:
        session['emp_no'] = emp_no
        session['user_name'] = name
        session['user_level'] = level


def _test_membership_rejection(db_path):
    app = Flask(__name__)
    app.secret_key = 'membership-test-secret'
    app.register_blueprint(user_mgmt.user_mgmt_bp, url_prefix='/user')
    original_get_db = user_mgmt.get_db
    original_mail = user_mgmt.send_membership_result_email
    captured_mail = []
    try:
        user_mgmt.get_db = lambda: _connect(db_path)
        user_mgmt.send_membership_result_email = (
            lambda *args, **kwargs: captured_mail.append((args, kwargs)) or True
        )
        conn = _connect(db_path)
        conn.execute("DELETE FROM users WHERE email='membership-reject@example.test'")
        cursor = conn.execute('''
            INSERT INTO users (name, password, position, level, rrn, email, status)
            VALUES ('가입거부테스트', 'hashed', '사원', 5, '900101-1234568',
                    'membership-reject@example.test', '대기')
        ''')
        user_id = cursor.lastrowid
        conn.commit()
        conn.close()

        client = app.test_client()
        _session(client, emp_no='admin', name='admin', level=0)
        missing = client.post('/user/reject', json={'user_idx': user_id})
        assert missing.status_code == 400
        assert '거부 사유' in missing.get_json()['message']

        reason = '입력한 소속 정보를 확인할 수 없습니다.'
        rejected = client.post(
            '/user/reject',
            json={'user_idx': user_id, 'rejection_reason': reason},
        )
        assert rejected.status_code == 200
        assert rejected.get_json()['mail_sent'] is True
        assert captured_mail[0][1]['rejection_reason'] == reason

        conn = _connect(db_path)
        row = conn.execute(
            'SELECT status, rejection_reason, rejected_at FROM users WHERE id=?',
            (user_id,),
        ).fetchone()
        conn.close()
        assert row['status'] == '거부'
        assert row['rejection_reason'] == reason
        assert row['rejected_at']
    finally:
        user_mgmt.get_db = original_get_db
        user_mgmt.send_membership_result_email = original_mail


def _test_approval_reference_box(db_path):
    template_root = str(Path(__file__).resolve().parents[1] / 'templates')
    app = Flask(__name__, template_folder=template_root)
    app.secret_key = 'approval-test-secret'
    app.register_blueprint(approval.approval_bp, url_prefix='/approval')
    original_get_db = approval.get_db
    original_render_template = approval.render_template
    try:
        approval.get_db = lambda: _connect(db_path)
        conn = _connect(db_path)
        test_users = (
            ('approval-drafter-test', '기안테스트', 5),
            ('approval-approver-test', '결재테스트', 4),
            ('approval-reference-test', '참조테스트', 5),
        )
        for emp_no, name, level in test_users:
            conn.execute('DELETE FROM users WHERE emp_no=?', (emp_no,))
            conn.execute('''
                INSERT INTO users (emp_no, name, password, position, level, email, status)
                VALUES (?, ?, 'hashed', '사원', ?, ? || '@example.test', '승인')
            ''', (emp_no, name, level, emp_no))
        conn.commit()
        conn.close()

        client = app.test_client()
        _session(client, emp_no='approval-drafter-test', name='기안테스트', level=5)

        own_reference = client.post('/approval/submit', data={
            'doc_type': '기안서', 'title': '본인 중복 차단', 'doc_data': '{}',
            'approver_1': '결재테스트', 'cc_receivers': '기안테스트',
        })
        assert own_reference.status_code == 400

        duplicate_role = client.post('/approval/submit', data={
            'doc_type': '보고서', 'title': '수신 참조 중복 차단', 'doc_data': '{}',
            'receivers': '참조테스트', 'cc_receivers': '참조테스트',
        })
        assert duplicate_role.status_code == 400

        submitted = client.post('/approval/submit', data={
            'doc_type': '기안서', 'title': '참조함 완료 전후 테스트', 'doc_data': '{}',
            'approver_1': '결재테스트', 'approver_2': '',
            'receivers': '', 'cc_receivers': '참조테스트,참조테스트',
        })
        assert submitted.status_code == 200
        conn = _connect(db_path)
        doc = conn.execute(
            "SELECT * FROM approvals WHERE title='참조함 완료 전후 테스트' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert doc['status'] == '대기'
        assert doc['cc_receivers'] == '참조테스트'

        captured_context = {}

        def capture_template(_name, **context):
            captured_context.clear()
            captured_context.update(context)
            return jsonify({'ok': True})

        approval.render_template = capture_template
        _session(client, emp_no='approval-reference-test', name='참조테스트', level=5)
        before = client.get('/approval/')
        assert before.status_code == 200
        assert not any(item['id'] == doc['id'] for item in captured_context['reference_docs'])

        _session(client, emp_no='approval-approver-test', name='결재테스트', level=4)
        completed = client.post(f"/approval/action/{doc['id']}", json={'action': 'approve'})
        assert completed.status_code == 200

        _session(client, emp_no='approval-reference-test', name='참조테스트', level=5)
        after = client.get('/approval/')
        assert after.status_code == 200
        assert any(item['id'] == doc['id'] for item in captured_context['reference_docs'])

        conn = _connect(db_path)
        notification = conn.execute('''
            SELECT content FROM messages
            WHERE receiver='참조테스트' AND content LIKE '%참조함 완료 전후 테스트%'
            ORDER BY id DESC LIMIT 1
        ''').fetchone()
        conn.close()
        assert notification and '최종 승인' in notification['content']
    finally:
        approval.get_db = original_get_db
        approval.render_template = original_render_template


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-approval-reference-test-') as directory:
        db_path = Path(directory) / 'test.db'
        _copy_database(db_path)
        _test_membership_rejection(db_path)
        _test_approval_reference_box(db_path)
    print('Membership approval and reference box test: PASS')


if __name__ == '__main__':
    main()
