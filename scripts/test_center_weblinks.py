import io
import os
import sqlite3
import sys
import tempfile

from flask import Flask


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import routes.school_bp as school_routes
import routes.main as main_routes


def connect(database):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    return connection


def create_schema(database):
    connection = connect(database)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            level INTEGER NOT NULL
        );
        CREATE TABLE weblinks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT,
            favicon_url TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            type TEXT DEFAULT 'url',
            filename TEXT,
            filepath TEXT
        );
        CREATE TABLE center_weblinks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT,
            favicon_url TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            type TEXT DEFAULT 'url',
            filename TEXT,
            filepath TEXT
        );
        CREATE TABLE center_user_weblink_order (
            user_name TEXT PRIMARY KEY,
            order_json TEXT
        );
        INSERT INTO users(name, level) VALUES
            ('본부4', 4),
            ('본부5', 5),
            ('센터장1', 8),
            ('센터장2', 8),
            ('일반직원', 9);
        INSERT INTO weblinks(title, created_by) VALUES ('메인 전용 링크', '본부5');
        """
    )
    connection.commit()
    connection.close()


def login(client, name, level):
    with client.session_transaction() as session:
        session.clear()
        session['user_name'] = name
        session['user_level'] = level


def insert_center_link(database, title, creator):
    connection = connect(database)
    cursor = connection.execute(
        """
        INSERT INTO center_weblinks(title, url, favicon_url, created_by, type)
        VALUES (?, 'https://example.com', '', ?, 'url')
        """,
        (title, creator),
    )
    connection.commit()
    link_id = cursor.lastrowid
    connection.close()
    return link_id


def test_center_weblink_permissions(database):
    create_schema(database)
    original_get_db = school_routes.get_db
    original_main_get_db = main_routes.get_db
    school_routes.get_db = lambda: connect(database)
    main_routes.get_db = lambda: connect(database)
    try:
        app = Flask(__name__)
        app.secret_key = 'center-weblink-test'
        app.register_blueprint(school_routes.school_bp, url_prefix='/school')
        app.register_blueprint(main_routes.main_bp)
        client = app.test_client()

        # 센터장(레벨 8)까지 등록할 수 있으며 메인 링크 테이블은 변경하지 않는다.
        login(client, '센터장1', 8)
        response = client.post(
            '/school/center-weblinks',
            data={'title': '센터 전용 링크', 'type': 'url', 'url': 'example.com'},
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        connection = connect(database)
        assert connection.execute('SELECT COUNT(*) FROM weblinks').fetchone()[0] == 1
        center_link = connection.execute(
            'SELECT * FROM center_weblinks WHERE title = ?',
            ('센터 전용 링크',),
        ).fetchone()
        assert center_link['created_by'] == '센터장1'
        assert center_link['url'] == 'http://example.com'
        connection.close()

        # 센터장 전용 링크는 5MB를 초과한 파일을 서버에서도 거부한다.
        response = client.post(
            '/school/center-weblinks',
            data={
                'title': '센터 대용량 파일',
                'type': 'file',
                'file': (io.BytesIO(b'x' * (5 * 1024 * 1024 + 1)), 'large.bin'),
            },
            content_type='multipart/form-data',
        )
        assert response.status_code == 413
        assert '5MB 이하' in response.get_json()['message']

        # 본인이 작성한 링크는 같은 레벨이어도 삭제할 수 있다.
        response = client.delete(f"/school/center-weblinks/{center_link['id']}")
        assert response.status_code == 200

        # 작성자보다 낮은 권한(숫자가 큰 레벨)은 삭제할 수 없다.
        hq_link_id = insert_center_link(database, '본부 작성', '본부5')
        login(client, '센터장1', 8)
        response = client.delete(f'/school/center-weblinks/{hq_link_id}')
        assert response.status_code == 403

        # 작성자보다 높은 권한(숫자가 작은 레벨)은 삭제할 수 있다.
        login(client, '본부4', 4)
        response = client.delete(f'/school/center-weblinks/{hq_link_id}')
        assert response.status_code == 200

        # 같은 레벨의 다른 작성자 글은 삭제할 수 없고, 상위 레벨은 가능하다.
        director_link_id = insert_center_link(database, '센터장 작성', '센터장1')
        login(client, '센터장2', 8)
        assert client.delete(f'/school/center-weblinks/{director_link_id}').status_code == 403
        login(client, '본부5', 5)
        assert client.delete(f'/school/center-weblinks/{director_link_id}').status_code == 200

        # 센터장보다 낮은 권한은 등록할 수 없다.
        login(client, '일반직원', 9)
        response = client.post(
            '/school/center-weblinks',
            data={'title': '권한 없음', 'type': 'url', 'url': 'example.com'},
        )
        assert response.status_code == 403

        # 메인 링크도 같은 5MB 서버 제한을 적용한다.
        login(client, '본부5', 5)
        response = client.post(
            '/save_weblink',
            data={
                'title': '메인 대용량 파일',
                'type': 'file',
                'file': (io.BytesIO(b'x' * (5 * 1024 * 1024 + 1)), 'large.bin'),
            },
            content_type='multipart/form-data',
        )
        assert response.status_code == 413
        assert '5MB 이하' in response.get_json()['message']
    finally:
        school_routes.get_db = original_get_db
        main_routes.get_db = original_main_get_db


def test_center_template_uses_separate_endpoints():
    template_path = os.path.join(PROJECT_ROOT, 'templates', 'school_bp.html')
    with open(template_path, encoding='utf-8') as template_file:
        template = template_file.read()

    assert 'school.save_center_weblink' in template
    assert 'school.update_center_weblink_order' in template
    assert 'school.delete_center_weblink' in template
    assert 'school.serve_center_weblink_file' in template
    assert 'font-size: 90%' in template
    assert 'file.size > 5 * 1024 * 1024' in template

    main_template_path = os.path.join(PROJECT_ROOT, 'templates', 'main.html')
    with open(main_template_path, encoding='utf-8') as template_file:
        main_template = template_file.read()
    assert 'file.size > 5 * 1024 * 1024' in main_template


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-center-weblink-test-') as directory:
        test_center_weblink_permissions(os.path.join(directory, 'center-weblinks.db'))
    test_center_template_uses_separate_endpoints()
    print('Center Web / File Link test: PASS')


if __name__ == '__main__':
    main()
