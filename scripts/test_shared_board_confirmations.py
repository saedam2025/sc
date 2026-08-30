"""본부공지사항·자료실 확인 기록과 상태 변경 차단 회귀 검사."""

import sqlite3
import sys
import tempfile
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import school_bp as school_routes
from routes import school_task as school_task_routes


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def prepare_database(path):
    conn = connect(path)
    conn.executescript('''
        CREATE TABLE schools (
            id INTEGER PRIMARY KEY,
            school_name TEXT,
            year INTEGER,
            center_director_id TEXT,
            center_director_id_2 TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            emp_no TEXT,
            name TEXT,
            position TEXT,
            department TEXT
        );
        CREATE TABLE school_posts (
            id INTEGER PRIMARY KEY,
            school_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            author TEXT,
            filename TEXT,
            filepath TEXT,
            status TEXT DEFAULT '접수',
            processor TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        INSERT INTO schools VALUES
            (1, '예당초등학교', 2026, 'director-1', '', 1);
        INSERT INTO users VALUES
            (1, 'director-1', '센터장사용자', '파견', '교육운영팀'),
            (2, 'hq-1', '본부담당자', '본부담당', '본부');
        INSERT INTO school_posts
            (id, school_id, category, title, content, author, status)
        VALUES
            (1, 1, 'community', '본부 공지', '공지 내용', '본부담당자', '접수'),
            (2, 1, 'reference', '공유 자료', '자료 내용', '본부담당자', '접수'),
            (3, 1, 'notice', '센터 요청', '요청 내용', '센터장사용자', '접수');
    ''')
    conn.commit()
    conn.close()


def set_session(client, emp_no, user_name, user_level, department='', position=''):
    with client.session_transaction() as session:
        session['emp_no'] = emp_no
        session['user_name'] = user_name
        session['user_level'] = user_level
        session['department'] = department
        session['position'] = position


def test_confirmation_routes(path):
    app = Flask(__name__)
    app.secret_key = 'shared-board-confirmation-test'
    app.register_blueprint(school_routes.school_bp, url_prefix='/school')

    original_get_db = school_routes.get_db
    original_can_access_post = school_routes.can_access_post
    school_routes.get_db = lambda: connect(path)
    school_routes.can_access_post = lambda _conn, _school_id, _category: True
    try:
        client = app.test_client()
        set_session(client, 'director-1', '센터장사용자', 8, '교육운영팀', '파견')

        confirmed = client.post('/school/post/1/confirm')
        assert confirmed.status_code == 200, confirmed.get_data(as_text=True)
        payload = confirmed.get_json()
        assert payload['confirmation_count'] == 1
        assert payload['confirmation_names'] == ['센터장사용자 (예당초등학교)']
        assert payload['already_confirmed'] is False

        duplicate = client.post('/school/post/1/confirm')
        assert duplicate.status_code == 200
        assert duplicate.get_json()['confirmation_count'] == 1
        assert duplicate.get_json()['already_confirmed'] is True

        detail = client.get('/school/post/api/1')
        assert detail.status_code == 200
        detail_data = detail.get_json()
        assert detail_data['is_shared'] is True
        assert detail_data['can_confirm'] is True
        assert detail_data['confirmed_by_me'] is True
        assert detail_data['confirmation_count'] == 1
        assert detail_data['view_count'] == 1

        non_shared = client.post('/school/post/3/confirm')
        assert non_shared.status_code == 400

        set_session(client, 'hq-1', '본부담당자', 5, '본부', '본부담당')
        hq_detail = client.get('/school/post/api/1').get_json()
        assert hq_detail['can_confirm'] is True
        assert hq_detail['confirmation_count'] == 1
        assert hq_detail['view_count'] == 2
        hq_confirmed = client.post('/school/post/1/confirm')
        assert hq_confirmed.status_code == 200
        assert hq_confirmed.get_json()['confirmation_count'] == 2
        assert '본부담당자 (본부담당)' in hq_confirmed.get_json()['confirmation_names']
    finally:
        school_routes.get_db = original_get_db
        school_routes.can_access_post = original_can_access_post


def test_status_api_and_headquarters_detail(path):
    app = Flask(__name__)
    app.secret_key = 'shared-board-status-test'
    app.register_blueprint(school_task_routes.school_task_bp, url_prefix='/school/tasks')
    original_get_db = school_task_routes.get_db
    school_task_routes.get_db = lambda: connect(path)
    try:
        client = app.test_client()
        set_session(client, 'hq-1', '본부담당자', 5)

        blocked = client.post(
            '/school/tasks/api/update_status',
            json={'post_ids': [1, 2], 'status': '완료'},
        )
        assert blocked.status_code == 400
        assert '확인 현황' in blocked.get_json()['message']

        allowed = client.post(
            '/school/tasks/api/update_status',
            json={'post_ids': [3], 'status': '처리중'},
        )
        assert allowed.status_code == 200

        detail = client.get('/school/tasks/api/detail/1')
        assert detail.status_code == 200
        data = detail.get_json()
        assert data['category'] == '본부공지사항'
        assert data['school_name'] == '전체 센터'
        assert data['is_shared'] is True
        assert data['confirmation_count'] == 2
        assert data['view_count'] == 3
    finally:
        school_task_routes.get_db = original_get_db


def test_templates():
    root = Path(__file__).resolve().parents[1] / 'templates'
    school_template = (root / 'school_bp.html').read_text(encoding='utf-8')
    task_template = (root / 'school_task.html').read_text(encoding='utf-8')

    assert "{{ '확인' if is_shared_current_board else '상태' }}" in school_template
    assert 'id="confirmPostBtn"' in school_template
    assert 'function confirmCurrentPost()' in school_template
    assert 'confirmation-tooltip' in school_template
    assert '아직 확인한 조직원이 없습니다.' in school_template
    assert "$('#readTitle').text(`제목: ${data.title || ''}`);" in school_template
    assert 'fa-regular fa-eye' in school_template
    assert school_template.count('class="read-outside-actions"') == 1
    assert school_template.count('<i class="fas fa-list"></i> 목록으로') == 1
    assert "url_for('memo.memo_board')" in school_template
    assert 'class="school-profile-logout"' in school_template
    assert school_template.count('onclick="openMyInfoModal()"') >= 2
    assert '>설정</button>' not in school_template
    assert 'fa-solid fa-chalkboard' in school_template
    assert 'fa-solid fa-gear' not in school_template

    assert 'id="bulkStatusControls"' in task_template
    assert 'id="singleStatusControls"' in task_template
    assert 'data-shared=' in task_template
    assert 'sharedConfirmationDetail' in task_template
    assert 'id="rViewCount"' in task_template


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-shared-board-confirmation-') as directory:
        path = Path(directory) / 'test.db'
        prepare_database(path)
        test_confirmation_routes(path)
        test_status_api_and_headquarters_detail(path)
        test_templates()
    print('Shared board confirmation test: PASS')


if __name__ == '__main__':
    main()
