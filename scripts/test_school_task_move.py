"""학교업무 통합 처리 게시물 일괄 이동 회귀 검사."""

import sqlite3
import sys
import tempfile
from pathlib import Path

from flask import Flask


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from routes import school_task as school_task_routes


def connect(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def prepare_database(path):
    connection = connect(path)
    connection.executescript('''
        CREATE TABLE schools (
            id INTEGER PRIMARY KEY,
            school_name TEXT NOT NULL
        );
        CREATE TABLE school_posts (
            id INTEGER PRIMARY KEY,
            school_id INTEGER,
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
        CREATE TABLE school_post_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_emp_no TEXT NOT NULL,
            user_name TEXT NOT NULL,
            school_name TEXT DEFAULT '',
            confirmed_at TEXT DEFAULT (datetime('now', 'localtime')),
            UNIQUE(post_id, user_emp_no)
        );

        INSERT INTO schools(id, school_name) VALUES
            (1, '새담센터'),
            (2, '푸른센터');
        INSERT INTO school_posts
            (id, school_id, category, title, content, author, filename, filepath, status, processor)
        VALUES
            (1, 1, 'survey', '만족도 A', '내용 A', '작성자 A', 'a.pdf', 'uploads/a.pdf', '처리중', '담당자 A'),
            (2, 2, '만족도조사', '만족도 B', '내용 B', '작성자 B', 'b.xlsx', 'uploads/b.xlsx', '완료', '담당자 B'),
            (3, 1, 'community', '본부 공지', '공지 내용', '본부', 'notice.pdf', 'uploads/notice.pdf', '접수', NULL),
            (4, NULL, 'community', '학교 없음', '학교 없음 내용', '본부', '', '', '접수', NULL);
        INSERT INTO school_post_confirmations
            (post_id, user_emp_no, user_name, school_name)
        VALUES
            (3, 'director-1', '센터장', '새담센터');
    ''')
    connection.commit()
    connection.close()


def login(client, level=5):
    with client.session_transaction() as session:
        session.clear()
        session['emp_no'] = 'hq-1'
        session['user_name'] = '본부담당자'
        session['user_level'] = level


def snapshot(connection, post_id):
    return dict(connection.execute('''
        SELECT id, school_id, title, content, author, filename, filepath, status, processor
        FROM school_posts
        WHERE id = ?
    ''', (post_id,)).fetchone())


def test_bulk_move(path):
    app = Flask(__name__, template_folder=str(PROJECT_ROOT / 'templates'))
    app.secret_key = 'school-task-move-test'
    app.register_blueprint(school_task_routes.school_task_bp, url_prefix='/school/tasks')

    original_get_db = school_task_routes.get_db
    school_task_routes.get_db = lambda: connect(path)
    try:
        client = app.test_client()
        login(client)

        connection = connect(path)
        before = {post_id: snapshot(connection, post_id) for post_id in (1, 2)}
        connection.close()

        response = client.post(
            '/school/tasks/api/move_posts',
            json={'post_ids': [1, 2], 'target_category': 'open_class'},
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        assert sorted(response.get_json()['moved_ids']) == [1, 2]
        assert response.get_json()['target_name'] == '강사정보현황'

        connection = connect(path)
        for post_id in (1, 2):
            assert connection.execute(
                'SELECT category FROM school_posts WHERE id = ?', (post_id,)
            ).fetchone()['category'] == 'open_class'
            assert snapshot(connection, post_id) == before[post_id]
        connection.close()

        # 공유 게시판에서 학교별 게시판으로 옮기면 이전 확인 기록은 제거한다.
        response = client.post(
            '/school/tasks/api/move_posts',
            json={'post_ids': [3], 'target_category': 'survey'},
        )
        assert response.status_code == 200
        assert response.get_json()['confirmation_reset_ids'] == [3]
        connection = connect(path)
        assert connection.execute(
            'SELECT category FROM school_posts WHERE id = 3'
        ).fetchone()['category'] == 'survey'
        assert connection.execute(
            'SELECT COUNT(*) FROM school_post_confirmations WHERE post_id = 3'
        ).fetchone()[0] == 0
        connection.close()

        # 학교 정보가 없는 공유 자료는 학교별 게시판으로 이동할 수 없다.
        response = client.post(
            '/school/tasks/api/move_posts',
            json={'post_ids': [4], 'target_category': 'survey'},
        )
        assert response.status_code == 400
        assert '소속 학교 정보' in response.get_json()['message']

        assert client.post(
            '/school/tasks/api/move_posts',
            json={'post_ids': [999], 'target_category': 'notice'},
        ).status_code == 404
        assert client.post(
            '/school/tasks/api/move_posts',
            json={'post_ids': [1], 'target_category': 'not-a-board'},
        ).status_code == 400
        assert client.post(
            '/school/tasks/api/move_posts',
            json={'post_ids': [1], 'target_category': 'open_class'},
        ).status_code == 400

        # 학교 계정(레벨 8)은 통합 처리 API에 접근할 수 없다.
        login(client, level=8)
        assert client.post(
            '/school/tasks/api/move_posts',
            json={'post_ids': [1], 'target_category': 'notice'},
        ).status_code == 403
    finally:
        school_task_routes.get_db = original_get_db


def test_template():
    template = (PROJECT_ROOT / 'templates' / 'school_task.html').read_text(encoding='utf-8')
    assert 'id="moveTargetCategory"' in template
    assert 'function moveSelectedPosts(button)' in template
    assert "fetch('/school/tasks/api/move_posts'" in template
    assert 'class="chk-box chk-item' in template
    assert '.task-table td :not(i)' in template
    assert '.task-table i.fa-solid' in template
    assert 'font-size: 0.855rem' in template
    assert 'font-weight: 400 !important' in template
    assert '<option value="접수">본사접수</option>' in template
    assert 'function getStatusLabel(status)' in template
    assert "return status === '접수' ? '본사접수' : status;" in template
    assert 'data-status="{{ task.status }}"' in template
    assert 'class="badge status-{{ task.status }}">{{ task.status_display }}</span>' in template
    assert 'badge.textContent = getStatusLabel(newStatus);' in template
    assert "statusBadge.className = 'badge status-' + statusValue;" in template


def test_category_display_names():
    assert school_task_routes.get_mapped_category('open_class') == '강사정보현황'
    assert school_task_routes.get_mapped_category('공개수업') == '강사정보현황'
    assert school_task_routes.get_mapped_category('survey') == '공개수업&만족도조사'
    assert school_task_routes.get_mapped_category('만족도조사') == '공개수업&만족도조사'
    assert school_task_routes.get_status_display('접수') == '본사접수'
    assert school_task_routes.get_status_display('처리중') == '처리중'
    assert school_task_routes.get_status_display('') == '본사접수'


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-school-task-move-') as directory:
        path = Path(directory) / 'test.db'
        prepare_database(path)
        test_bulk_move(path)
    test_template()
    test_category_display_names()
    print('School task bulk move test: PASS')


if __name__ == '__main__':
    main()
