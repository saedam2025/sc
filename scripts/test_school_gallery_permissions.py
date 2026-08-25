import os
import sqlite3
import sys
import tempfile

from flask import Blueprint, Flask


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import routes.gall2 as gallery_routes


def connect(database):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    return connection


def create_schema(database):
    connection = connect(database)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_no TEXT,
            name TEXT,
            level INTEGER
        );
        CREATE TABLE schools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_name TEXT,
            access_key TEXT UNIQUE,
            center_director_id TEXT,
            center_director_id_2 TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE gall2_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT,
            author TEXT,
            tab_id INTEGER NOT NULL DEFAULT 1,
            upload_token TEXT UNIQUE,
            school_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE gall2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            filename TEXT NOT NULL,
            thumb_name TEXT NOT NULL,
            file_type TEXT,
            tab_id INTEGER NOT NULL DEFAULT 1,
            post_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users(emp_no, name, level) VALUES
            ('hq5', '본부5', 5),
            ('dir-a', '센터장A', 8),
            ('dir-b', '센터장B', 8),
            ('staff9', '일반9', 9);
        INSERT INTO schools(school_name, access_key, center_director_id, center_director_id_2) VALUES
            ('A학교', 'school-a', 'dir-a', ''),
            ('B학교', 'school-b', 'dir-b', ''),
            ('낮은권한학교', 'school-low', 'staff9', '');
        INSERT INTO gall2_posts(title, content, author, school_id) VALUES
            ('센터장 게시물', '원문', '센터장A', 0),
            ('본부 게시물', '본부 원문', '본부5', 0),
            ('삭제 대상', '삭제', '센터장A', 0);
        """
    )
    connection.commit()
    connection.close()


def login(client, name, emp_no, level):
    with client.session_transaction() as session:
        session.clear()
        session['user_name'] = name
        session['emp_no'] = emp_no
        session['user_level'] = level


def test_permissions_views_and_comments(database):
    create_schema(database)
    original_get_db = gallery_routes.get_db
    gallery_routes.get_db = lambda: connect(database)
    try:
        app = Flask(__name__, template_folder=os.path.join(PROJECT_ROOT, 'templates'))
        app.secret_key = 'school-gallery-permission-test'
        school_stub = Blueprint('school', __name__)

        @school_stub.route('/school/<string:school_key>')
        def school_detail(school_key):
            return school_key

        app.register_blueprint(school_stub)
        app.register_blueprint(gallery_routes.gall2_bp)
        client = app.test_client()

        gallery_routes.ensure_gall2_schema()
        connection = connect(database)
        columns = {row['name'] for row in connection.execute('PRAGMA table_info(gall2_posts)')}
        assert 'view_count' in columns
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gall2_comments'"
        ).fetchone()
        connection.close()

        # 레벨 9는 학교에 지정되어 있어도 읽기와 쓰기 모두 거부된다.
        login(client, '일반9', 'staff9', 9)
        assert client.get('/school/school-low/gallery/post/1/comments').status_code == 403
        assert client.post('/school/school-low/gallery/upload').status_code == 403

        # 센터장(8)은 읽고 쓸 수 있으며 본인 게시물을 수정할 수 있다.
        login(client, '센터장A', 'dir-a', 8)
        assert client.get('/school/school-a/gallery/post/1/comments').status_code == 200
        response = client.post(
            '/school/school-a/gallery/post/1/update',
            json={'title': '본인 수정', 'content': '수정됨'},
        )
        assert response.status_code == 200

        # 같은 레벨의 다른 센터장도 요청한 "본인 이상 레벨"에 포함된다.
        login(client, '센터장B', 'dir-b', 8)
        response = client.post(
            '/school/school-b/gallery/post/1/update',
            json={'title': '동급 수정', 'content': '동급 수정됨'},
        )
        assert response.status_code == 200

        # 레벨 8은 더 높은 권한인 레벨 5 작성자의 게시물을 수정·삭제할 수 없다.
        response = client.post(
            '/school/school-b/gallery/post/2/update',
            json={'title': '권한 위반', 'content': ''},
        )
        assert response.status_code == 403
        response = client.post(
            '/school/school-b/gallery/delete_bulk',
            json={'post_ids': [2, 3]},
        )
        assert response.status_code == 403
        connection = connect(database)
        assert connection.execute('SELECT COUNT(*) FROM gall2_posts WHERE id IN (2, 3)').fetchone()[0] == 2
        connection.close()

        # 상위 레벨 5는 센터장 작성 게시물을 수정할 수 있다.
        login(client, '본부5', 'hq5', 5)
        response = client.post(
            '/school/school-a/gallery/post/1/update',
            json={'title': '상위 수정', 'content': '상위 권한 수정'},
        )
        assert response.status_code == 200

        # 조회수는 같은 세션에서 같은 게시물을 여러 번 열어도 한 번만 증가한다.
        login(client, '센터장A', 'dir-a', 8)
        first_view = client.post('/school/school-a/gallery/post/1/view')
        second_view = client.post('/school/school-a/gallery/post/1/view')
        assert first_view.status_code == 200 and first_view.get_json()['view_count'] == 1
        assert second_view.status_code == 200 and second_view.get_json()['view_count'] == 1

        # 상세 사진 모달에서 댓글을 등록하고 다시 조회할 수 있다.
        response = client.post(
            '/school/school-a/gallery/post/1/comments',
            json={'content': '학교갤러리 댓글입니다.'},
        )
        assert response.status_code == 200
        comments = client.get('/school/school-a/gallery/post/1/comments').get_json()['comments']
        assert len(comments) == 1
        assert comments[0]['author'] == '센터장A'
        assert comments[0]['content'] == '학교갤러리 댓글입니다.'

        # 같은 레벨의 다른 센터장은 센터장 작성 게시물을 삭제할 수 있다.
        login(client, '센터장B', 'dir-b', 8)
        response = client.post('/school/school-b/gallery/post/3/delete')
        assert response.status_code == 302
        connection = connect(database)
        assert connection.execute('SELECT 1 FROM gall2_posts WHERE id=3').fetchone() is None
        connection.close()
    finally:
        gallery_routes.get_db = original_get_db


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-school-gallery-test-') as directory:
        test_permissions_views_and_comments(os.path.join(directory, 'gallery.db'))
    print('School gallery permissions/views/comments test: PASS')


if __name__ == '__main__':
    main()
