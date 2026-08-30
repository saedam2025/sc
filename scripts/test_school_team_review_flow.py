"""별도 팀 게시물 열람과 근무표·청구관련 팀장확인 연동 회귀 검사."""

import sqlite3
import sys
import tempfile
from pathlib import Path

from flask import Flask, session


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from routes import school_bp as school_routes
from routes import school_task as school_task_routes
from routes.school_team_review import (
    build_team_review_post_queries,
    post_matches_team,
    post_requires_team_review,
)


def connect(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def prepare_database(path):
    connection = connect(path)
    connection.executescript('''
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            emp_no TEXT UNIQUE,
            name TEXT,
            position TEXT,
            level INTEGER,
            custom_team TEXT DEFAULT '',
            status TEXT DEFAULT '승인'
        );
        CREATE TABLE schools (
            id INTEGER PRIMARY KEY,
            school_name TEXT NOT NULL,
            year INTEGER,
            center_director_id TEXT DEFAULT '',
            center_director_id_2 TEXT DEFAULT ''
        );
        CREATE TABLE school_posts (
            id INTEGER PRIMARY KEY,
            school_id INTEGER,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            author TEXT,
            author_emp_no TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            filepath TEXT DEFAULT '',
            status TEXT DEFAULT '접수',
            processor TEXT DEFAULT '',
            team_reviewer TEXT DEFAULT '',
            team_reviewed_at TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );

        INSERT INTO users(id, emp_no, name, position, level, custom_team, status) VALUES
            (1, 'leader-a', 'A팀장', '센터장(팀장)', 7, '별도A팀', '승인'),
            (2, 'director-a', 'A센터장', '센터장', 8, '별도A팀', '승인'),
            (3, 'director-b', 'B센터장', '센터장', 8, '별도B팀', '승인');
        INSERT INTO schools(id, school_name, year, center_director_id, center_director_id_2) VALUES
            (1, 'A센터', 2026, 'director-a', 'leader-a'),
            (2, 'B센터', 2026, 'director-b', '');
        INSERT INTO school_posts(id, school_id, category, title, author, author_emp_no, status) VALUES
            (1, 1, 'notice', '같은 팀 일반글', 'A센터장', 'director-a', '접수'),
            (2, 1, 'work_schedule', '같은 팀 근무표', 'A센터장', 'director-a', '접수'),
            (3, 1, 'billing', '같은 팀 청구', 'A센터장', 'director-a', '접수'),
            (4, 1, 'survey', '같은 팀 조사', 'A센터장', 'director-a', '접수'),
            (5, 2, 'work_schedule', '다른 팀 근무표', 'B센터장', 'director-b', '접수');
    ''')
    connection.commit()
    connection.close()


def login(client, emp_no, user_name, level, position=''):
    with client.session_transaction() as user_session:
        user_session.clear()
        user_session['emp_no'] = emp_no
        user_session['user_name'] = user_name
        user_session['user_level'] = level
        user_session['position'] = position


def test_team_scope_and_required_categories(app, path):
    connection = connect(path)
    original_category_access = school_routes.can_access_school_category
    try:
        notice = connection.execute('SELECT * FROM school_posts WHERE id=1').fetchone()
        work_schedule = connection.execute('SELECT * FROM school_posts WHERE id=2').fetchone()
        other_team = connection.execute('SELECT * FROM school_posts WHERE id=5').fetchone()

        assert post_matches_team(connection, notice, '별도A팀')
        assert not post_requires_team_review(connection, notice)
        assert post_requires_team_review(connection, work_schedule)
        assert not post_matches_team(connection, other_team, '별도A팀')

        count_query, data_query, params = build_team_review_post_queries('별도A팀')
        assert connection.execute(count_query, params).fetchone()[0] == 4
        rows = connection.execute(data_query, [*params, 20, 0]).fetchall()
        assert {row['id'] for row in rows} == {1, 2, 3, 4}
        assert school_routes.get_post_author_school_name(connection, notice) == 'A센터'

        # 메뉴권한 자체는 별도 테스트 대상이므로 여기서는 같은 팀 읽기 분기만 검사한다.
        school_routes.can_access_school_category = lambda category: True
        with app.test_request_context('/'):
            session['emp_no'] = 'leader-a'
            session['user_name'] = 'A팀장'
            session['user_level'] = 7
            session['position'] = '센터장(팀장)'
            assert school_routes.can_access_post(
                connection, notice['school_id'], notice['category'], post=notice
            )
    finally:
        school_routes.can_access_school_category = original_category_access
        connection.close()


def test_confirmation_endpoints(client, path):
    login(client, 'leader-a', 'A팀장', 7, '센터장(팀장)')

    connection = connect(path)
    pending = connection.execute(
        'SELECT status, team_reviewer, team_reviewed_at FROM school_posts WHERE id=2'
    ).fetchone()
    assert pending['status'] == '접수'
    assert pending['team_reviewer'] == ''
    assert pending['team_reviewed_at'] is None
    connection.close()

    response = client.post('/school/post/team-review', json={'post_ids': [1]})
    assert response.status_code == 403
    assert '근무표' in response.get_json()['message'] or '접수 상태' in response.get_json()['message']

    response = client.post('/school/post/team-review', json={'post_ids': [2]})
    assert response.status_code == 200, response.get_data(as_text=True)
    connection = connect(path)
    reviewed = connection.execute(
        'SELECT status, processor, team_reviewer, team_reviewed_at FROM school_posts WHERE id=2'
    ).fetchone()
    assert reviewed['status'] == '팀장확인'
    assert reviewed['processor'] == 'A팀장'
    assert reviewed['team_reviewer'] == 'A팀장'
    assert reviewed['team_reviewed_at']
    connection.close()

    login(client, 'hq-1', '본부담당자', 5, '과장')
    response = client.post(
        '/school/tasks/api/update_status',
        json={'post_ids': [1], 'status': '팀장확인'},
    )
    assert response.status_code == 400
    assert '근무표와 청구관련' in response.get_json()['message']

    response = client.post(
        '/school/tasks/api/update_status',
        json={'post_ids': [3], 'status': '처리중'},
    )
    assert response.status_code == 400
    assert '팀장확인이 필요한' in response.get_json()['message']

    response = client.post(
        '/school/tasks/api/update_status',
        json={'post_ids': [3], 'status': '팀장확인'},
    )
    assert response.status_code == 200
    response = client.post(
        '/school/tasks/api/update_status',
        json={'post_ids': [3], 'status': '완료'},
    )
    assert response.status_code == 200

    # 확인 대상 게시물을 일반 게시판으로 옮기면 팀장확인 상태가 남지 않는다.
    response = client.post(
        '/school/tasks/api/move_posts',
        json={'post_ids': [2], 'target_category': 'notice'},
    )
    assert response.status_code == 200
    assert response.get_json()['workflow_reset_ids'] == [2]
    connection = connect(path)
    moved = connection.execute(
        'SELECT category, status, processor, team_reviewer, team_reviewed_at '
        'FROM school_posts WHERE id=2'
    ).fetchone()
    assert moved['category'] == 'notice'
    assert moved['status'] == '접수'
    assert moved['processor'] == ''
    assert moved['team_reviewer'] == ''
    assert moved['team_reviewed_at'] is None
    connection.close()


def test_templates():
    task_template = (PROJECT_ROOT / 'templates' / 'school_task.html').read_text(encoding='utf-8')
    board_template = (PROJECT_ROOT / 'templates' / 'school_bp.html').read_text(encoding='utf-8')

    assert "changeStatus('팀장확인', this)" in task_template
    assert "changeSingleStatus('팀장확인', this)" in task_template
    assert '팀장확인대기' not in task_template
    assert 'count-team-required' not in task_template
    assert 'p.can_team_review' in board_template
    assert 'class="team-review-pending-btn"' in board_template
    assert '현재 본사접수 · 클릭하면 팀장확인 처리' in board_template
    assert '>본사접수</button>' in board_template
    assert '? `<button type="button" class="team-review-pending-btn"' in board_template
    assert 'class="board-author-school"' in board_template
    assert 'const authorSchoolMeta = isTeamReviewBoard && data.author_school_name' in board_template
    assert '<span class="read-meta-school">${escapeHtml(data.author_school_name)}</span>' in board_template
    assert '팀장확인대기' not in board_template


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-school-team-review-') as directory:
        path = Path(directory) / 'test.db'
        prepare_database(path)
        app = Flask(__name__, template_folder=str(PROJECT_ROOT / 'templates'))
        app.secret_key = 'school-team-review-test'
        app.register_blueprint(school_routes.school_bp, url_prefix='/school')
        app.register_blueprint(school_task_routes.school_task_bp, url_prefix='/school/tasks')

        original_school_get_db = school_routes.get_db
        original_task_get_db = school_task_routes.get_db
        school_routes.get_db = lambda: connect(path)
        school_task_routes.get_db = lambda: connect(path)
        try:
            test_team_scope_and_required_categories(app, path)
            test_confirmation_endpoints(app.test_client(), path)
        finally:
            school_routes.get_db = original_school_get_db
            school_task_routes.get_db = original_task_get_db

    test_templates()
    print('School team review flow test: PASS')


if __name__ == '__main__':
    main()
