import os
import sqlite3
import sys
import tempfile

from flask import Flask


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import routes.chat as chat
import routes.menu_access as menu_access
from routes.school_bp import build_school_post_list_queries, is_shared_board


def connect(database):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return connection


def test_chat_organization(database):
    connection = connect(database)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_no TEXT,
            name TEXT,
            department TEXT,
            position TEXT,
            level INTEGER,
            profile_icon TEXT,
            status TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users(emp_no, name, department, position, level, profile_icon, status) VALUES
            ('admin', 'admin', '본부', '대표이사', 1, 'A', '승인'),
            ('hq-1', '본부직원', '본부', '사원', 5, 'H', '승인'),
            ('dir-1', '센터장사용자', '파견', '센터장', 8, 'D', '승인'),
            ('wait-1', '승인대기', '파견', '강사', 10, 'W', '대기');
        """
    )
    connection.commit()
    connection.close()

    original_get_db = chat.get_db
    chat.get_db = lambda: connect(database)
    try:
        app = Flask(__name__)
        app.secret_key = 'center-director-test'
        app.register_blueprint(chat.chat_bp)
        client = app.test_client()

        assert client.get('/api/chat/organization').status_code == 401
        with client.session_transaction() as session:
            session['user_name'] = '센터장사용자'
            session['emp_no'] = 'dir-1'
            session['user_level'] = 8

        response = client.get('/api/chat/organization')
        assert response.status_code == 200, response.get_data(as_text=True)
        users = response.get_json()['users']
        assert [user['name'] for user in users] == ['본부직원', '센터장사용자']
        assert next(user for user in users if user['name'] == '센터장사용자')['organization_group'] == '센터장'
        assert all('email' not in user and 'phone' not in user for user in users)
    finally:
        chat.get_db = original_get_db


def query_posts(connection, school_id, category, category_name):
    count_query, data_query, params = build_school_post_list_queries(
        school_id, category, category_name
    )
    count = connection.execute(count_query, params).fetchone()[0]
    rows = connection.execute(data_query, [*params, 20, 0]).fetchall()
    return count, [row['title'] for row in rows]


def test_shared_boards(database):
    connection = connect(database)
    connection.executescript(
        """
        CREATE TABLE school_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            author TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO school_posts(school_id, category, title, content, author) VALUES
            (1, 'community', '전역 본부공지', '', '본부'),
            (1, 'reference', '전역 자료 1', '', '본부'),
            (2, '자료실', '전역 자료 2', '', '본부'),
            (1, 'notice', '1번 학교 안내', '', '센터장1'),
            (2, 'notice', '2번 학교 안내', '', '센터장2');
        """
    )
    connection.commit()

    assert is_shared_board('community') and is_shared_board('본부공지사항')
    assert is_shared_board('reference') and is_shared_board('자료실')
    assert not is_shared_board('notice')

    for school_id in (1, 2):
        count, titles = query_posts(connection, school_id, 'reference', '자료실')
        assert count == 2
        assert set(titles) == {'전역 자료 1', '전역 자료 2'}

    assert query_posts(connection, 1, 'notice', '수강안내문')[1] == ['1번 학교 안내']
    assert query_posts(connection, 2, 'notice', '수강안내문')[1] == ['2번 학교 안내']
    connection.close()


def test_org_chart_function_names_are_isolated():
    template_directory = os.path.join(PROJECT_ROOT, 'templates')
    with open(os.path.join(template_directory, 'chat_widget.html'), encoding='utf-8') as file:
        chat_template = file.read()
    with open(os.path.join(template_directory, 'school_bp.html'), encoding='utf-8') as file:
        school_template = file.read()

    assert 'async function loadOrgChart()' in chat_template
    assert 'async function loadOrgChart(' not in school_template
    assert 'async function loadDirectorAssignmentOrgChart(' in school_template
    assert "loadDirectorAssignmentOrgChart('regOrgChart'" in school_template
    assert "loadDirectorAssignmentOrgChart('editOrgChart'" in school_template
    assert '센터장 이름 검색' in school_template
    assert '센터장 지정 해제' in school_template
    assert 'id="edit_selectedDirId" required' not in school_template
    assert 'function filterDirectorCandidates(' in school_template


def test_assigned_level_7_can_open_school_workspace(database):
    connection = connect(database)
    connection.executescript(
        """
        CREATE TABLE schools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            center_director_id TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE menu_access_permissions (
            menu_key TEXT PRIMARY KEY,
            max_level INTEGER NOT NULL,
            updated_by TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO schools(center_director_id, is_active) VALUES ('dir-team-1', 1);
        INSERT INTO menu_access_permissions(menu_key, max_level) VALUES
            ('school_group', 6),
            ('school_workspace', 6),
            ('school_calendar', 6);
        """
    )
    connection.commit()
    connection.close()

    original_get_db = menu_access.get_db
    menu_access.get_db = lambda: connect(database)
    try:
        app = Flask(__name__)
        app.secret_key = 'level-7-school-director-test'
        with app.test_request_context('/school'):
            from flask import session

            session['user_name'] = '센터장팀장'
            session['emp_no'] = 'dir-team-1'
            session['user_level'] = 7

            access = menu_access.build_menu_access(7)
            assert access['school_group'] is True
            assert access['school_workspace'] is True
            assert access['school_calendar'] is True
            assert menu_access.enforce_request_menu_access() is None
    finally:
        menu_access.get_db = original_get_db


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-center-director-test-') as directory:
        test_chat_organization(os.path.join(directory, 'chat.db'))
        test_shared_boards(os.path.join(directory, 'school.db'))
        test_org_chart_function_names_are_isolated()
        test_assigned_level_7_can_open_school_workspace(
            os.path.join(directory, 'level-7-director.db')
        )
    print('Center director workspace test: PASS')


if __name__ == '__main__':
    main()
