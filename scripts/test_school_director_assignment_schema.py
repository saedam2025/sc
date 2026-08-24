import os
import sqlite3
import sys
import tempfile
import types


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import routes.database as database_routes


def expect_assignment_conflict(connection, sql, params):
    try:
        connection.execute(sql, params)
    except sqlite3.IntegrityError as error:
        assert 'CENTER_DIRECTOR_ALREADY_ASSIGNED' in str(error)
        connection.rollback()
        return
    raise AssertionError('센터장 중복 지정이 차단되지 않았습니다.')


def test_center_menu_permissions(test_db):
    flask_stub = types.ModuleType('flask')
    flask_stub.session = {}
    flask_stub.jsonify = lambda *args, **kwargs: (args, kwargs)
    flask_stub.redirect = lambda *args, **kwargs: (args, kwargs)
    flask_stub.request = types.SimpleNamespace(
        path='/', endpoint='', view_args={}, form={}, args={}, is_json=False,
        accept_mimetypes=types.SimpleNamespace(best='text/html'),
    )
    sys.modules['flask'] = flask_stub

    import routes.menu_access as menu_access

    connection = sqlite3.connect(test_db)
    connection.execute(
        """
        INSERT INTO menu_access_permissions(menu_key, max_level) VALUES
            ('school_group', 5),
            ('school_center_boards', 14),
            ('school_center_shared', 8),
            ('school_center_shared_read', 8),
            ('school_center_shared_write', 5),
            ('school_center_shared_delete', 5),
            ('school_center_shared_comment', 8)
        ON CONFLICT(menu_key) DO UPDATE SET max_level=excluded.max_level
        """
    )
    connection.execute(
        """
        INSERT INTO admin_settings(key, value) VALUES ('school_director_scope_enabled', '1')
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """
    )
    connection.commit()
    connection.close()

    def connect_test_db():
        result = sqlite3.connect(test_db)
        result.row_factory = sqlite3.Row
        return result

    menu_access.get_db = connect_test_db
    flask_stub.session.update({
        'emp_no': 'director-b',
        'user_name': '두번째센터장',
        'user_level': 8,
    })
    levels = menu_access.load_menu_max_levels()
    assert menu_access.has_active_school_assignment(8) is True
    assert menu_access.menu_is_allowed('school_center_boards', 8, levels) is True
    assert menu_access.menu_is_allowed('school_center_shared', 8, levels) is True
    assert menu_access.shared_board_action_is_allowed('read', 8, levels) is True
    assert menu_access.shared_board_action_is_allowed('comment', 8, levels) is True
    assert menu_access.shared_board_action_is_allowed('write', 8, levels) is False
    assert menu_access.shared_board_action_is_allowed('delete', 8, levels) is False

    # 센터장 화면의 공용 미리보기 API도 expense_main이 아닌 센터장
    # 지출결의 권한으로 통과해야 한다.
    flask_stub.request.path = '/expense/api/preview'
    flask_stub.request.form = {'expense_submit_channel': 'center'}
    assert menu_access.enforce_request_menu_access() is None

    # 로그인 세션 레벨이 늦게 갱신돼도 실제 학교 배정은 상위 학교관리
    # 권한 때문에 전체 센터장 메뉴가 사라지는 일을 막는다.
    flask_stub.session['user_level'] = 12
    assert menu_access.menu_is_allowed('school_center_boards', 12, levels) is True


def main():
    original_db_file = database_routes.DB_FILE
    with tempfile.TemporaryDirectory(prefix='saedam-director-schema-') as directory:
        test_db = os.path.join(directory, 'legacy.db')
        connection = sqlite3.connect(test_db)
        connection.executescript(
            """
            CREATE TABLE schools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER,
                school_name TEXT,
                center_director_id TEXT,
                is_active INTEGER DEFAULT 1
            );
            """
        )
        connection.close()

        try:
            database_routes.DB_FILE = test_db
            database_routes.init_db()
        finally:
            database_routes.DB_FILE = original_db_file

        connection = sqlite3.connect(test_db)
        columns = {
            row[1] for row in connection.execute('PRAGMA table_info(schools)').fetchall()
        }
        assert 'center_director_id_2' in columns
        permission_values = dict(connection.execute(
            """
            SELECT menu_key, max_level
            FROM menu_access_permissions
            WHERE menu_key LIKE 'school_center_shared%'
            """
        ).fetchall())
        assert permission_values == {
            'school_center_shared': 8,
            'school_center_shared_read': 8,
            'school_center_shared_write': 5,
            'school_center_shared_delete': 5,
            'school_center_shared_comment': 8,
        }

        connection.execute(
            "INSERT INTO schools(school_name, center_director_id, center_director_id_2) VALUES (?, ?, ?)",
            ('공동담당학교', 'director-a', 'director-b'),
        )
        connection.execute(
            "INSERT INTO schools(school_name, center_director_id, center_director_id_2) VALUES (?, ?, ?)",
            ('다른학교', 'director-c', ''),
        )
        connection.commit()

        expect_assignment_conflict(
            connection,
            "INSERT INTO schools(school_name, center_director_id) VALUES (?, ?)",
            ('중복학교', 'director-b'),
        )
        expect_assignment_conflict(
            connection,
            "UPDATE schools SET center_director_id_2=? WHERE school_name=?",
            ('director-a', '다른학교'),
        )
        expect_assignment_conflict(
            connection,
            "INSERT INTO schools(school_name, center_director_id, center_director_id_2) VALUES (?, ?, ?)",
            ('동일인중복', 'director-d', 'director-d'),
        )
        connection.close()

        test_center_menu_permissions(test_db)

    print('School director assignment schema test: PASS')


if __name__ == '__main__':
    main()
