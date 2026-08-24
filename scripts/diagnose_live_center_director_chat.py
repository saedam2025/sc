import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import app
from routes.database import get_db


def main():
    with app.app_context():
        connection = get_db()
        director = connection.execute(
            """
            SELECT u.emp_no, u.name, u.level, s.access_key, s.school_name
            FROM users u
            JOIN schools s ON u.emp_no IN (s.center_director_id, s.center_director_id_2)
            WHERE u.level = 8
              AND u.status = '승인'
              AND COALESCE(s.is_active, 1) = 1
            ORDER BY s.id DESC
            LIMIT 1
            """
        ).fetchone()
        connection.close()

    if not director:
        raise AssertionError('진단 가능한 승인 센터장과 활성 학교가 없습니다.')

    client = app.test_client()
    with client.session_transaction() as login_session:
        login_session['emp_no'] = director['emp_no']
        login_session['user_name'] = director['name']
        login_session['user_level'] = director['level']

    organization = client.get(
        '/api/chat/organization', headers={'Accept': 'application/json'}
    )
    page = client.get(
        f"/school/{director['access_key']}?category=community"
    )

    print('director:', director['name'], '/', director['school_name'])
    print('organization:', organization.status_code, organization.content_type)
    print('organization body:', organization.get_data(as_text=True)[:500])
    print('workspace:', page.status_code, page.content_type, len(page.data))
    print('organization script included:', b'/api/chat/organization' in page.data)
    print('organization container included:', b'orgChartListContainer' in page.data)

    assert organization.status_code == 200
    payload = organization.get_json()
    assert payload and payload.get('status') == 'success'
    assert payload.get('users')
    assert page.status_code == 200
    assert b'/api/chat/organization' in page.data
    assert b'orgChartListContainer' in page.data
    print('Live center director chat diagnostic: PASS')


if __name__ == '__main__':
    main()
