"""학부모 Web Push·강사 출결 기능의 독립 회귀 테스트."""

import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from flask import Flask
from openpyxl import Workbook


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"PASS: {message}")


def json_response(response):
    payload = response.get_json(silent=True)
    check(isinstance(payload, dict), f"JSON 응답 확인 ({response.status_code})")
    return payload


def run():
    os.environ.pop('PARENT_SMS_WEBHOOK_URL', None)
    os.environ.pop('VAPID_PUBLIC_KEY', None)
    os.environ.pop('VAPID_PRIVATE_KEY', None)
    os.environ.pop('VAPID_SUBJECT', None)

    import routes.parent_notifications as feature
    from routes.menu_access import resolve_request_menu

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / 'parent-test.db'

        def connection():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            return conn

        conn = connection()
        conn.executescript('''
            CREATE TABLE schools (
                id INTEGER PRIMARY KEY, school_name TEXT, year TEXT,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE users (
                emp_no TEXT PRIMARY KEY, name TEXT, phone TEXT
            );
            INSERT INTO schools VALUES (1, '새담초등학교', '2026', 1);
            INSERT INTO users VALUES ('T001', '이강사', '01099998888');
        ''')
        feature.ensure_parent_notification_schema(conn)
        conn.close()
        feature.get_db = connection

        app = Flask(
            __name__,
            template_folder=str(ROOT / 'templates'),
            static_folder=str(ROOT / 'static'),
        )
        app.secret_key = 'parent-feature-test'

        @app.route('/login_page')
        def login_page():
            return 'login'

        app.register_blueprint(feature.parent_notification_bp)
        client = app.test_client()

        with client.session_transaction() as session:
            session['emp_no'] = 'ADMIN01'
            session['user_name'] = '관리자'
            session['user_level'] = 2

        response = client.post('/parent-notifications/api/classes', json={
            'school_id': 1,
            'school_name': '새담초등학교',
            'department': '과학',
            'class_name': '로봇과학 A반',
            'instructor_emp_no': 'T001',
            'instructor_name': '이강사',
        })
        check(response.status_code == 200, '학교·부서별 강사 전용페이지 생성')
        class_data = json_response(response)
        class_id = class_data['id']
        instructor_token = class_data['url'].rsplit('/', 1)[-1]

        response = client.post('/parent-notifications/api/parents', json={
            'name': '홍길동',
            'phone': '010-1234-5678',
            'email': 'parent@example.com',
            'children': [
                {'name': '김민준', 'school_name': '새담초등학교', 'grade': '3',
                 'classroom': '1', 'class_id': class_id},
                {'name': '김서연', 'school_name': '새담초등학교', 'grade': '1',
                 'classroom': '2', 'class_id': class_id},
            ],
        })
        check(response.status_code == 200, '한 학부모에게 다자녀 등록')
        parent_id = json_response(response)['id']

        bootstrap = json_response(client.get('/parent-notifications/api/bootstrap'))
        check(len(bootstrap['guardians']) == 1, '관리자 학부모 목록 조회')
        check(len(bootstrap['guardians'][0]['children']) == 2, '다자녀 연결 유지')
        check(str(class_id) in bootstrap['guardians'][0]['children'][0]['class_ids'],
              '학생의 강좌 연결 정보 조회')
        invite_token = bootstrap['guardians'][0]['invite_token']

        with client.session_transaction() as session:
            session.clear()
        response = client.get(f'/parent/register/{invite_token}')
        check(response.status_code == 200 and '회원가입은 필요하지 않습니다' in response.get_data(as_text=True),
              '비회원 학부모 등록 페이지')
        check(client.get(f'/parent/api/push/public-key/{invite_token}').status_code == 503,
              'VAPID 미설정 상태 안내')
        response = client.post(f'/parent/api/push/subscribe/{invite_token}', json={
            'endpoint': 'https://push.example.test/one',
            'keys': {'p256dh': 'test-p256dh', 'auth': 'test-auth'},
        })
        check(response.status_code == 200, '인트라넷 회원과 분리된 학부모 푸시 구독 저장')

        response = client.get(f'/parent-notifications/instructor/{instructor_token}')
        check(response.status_code == 200 and '강사 로그인이 필요합니다' in response.get_data(as_text=True),
              '비로그인 강사에게 로그인 안내')
        with client.session_transaction() as session:
            session['emp_no'] = 'T001'
            session['user_name'] = '이강사'
            session['user_level'] = 12
        response = client.get(f'/parent-notifications/instructor/{instructor_token}')
        html = response.get_data(as_text=True)
        check(response.status_code == 200 and '김민준' in html and '김서연' in html,
              '지정 강사의 반별 학생 출결 화면')

        conn = connection()
        student_id = conn.execute(
            "SELECT id FROM parent_students WHERE name='김민준'"
        ).fetchone()['id']
        conn.close()
        response = client.post(
            f'/parent-notifications/instructor/{instructor_token}/attendance',
            json={'student_id': student_id, 'status': '출석'},
        )
        check(response.status_code == 200, '강사 출석 처리와 학부모 푸시 발송 기록')
        attendance = json_response(response)
        check(attendance['status'] == '출석', '출석 상태 응답')
        response = client.post(
            f'/parent-notifications/instructor/{instructor_token}/send',
            json={'kind': '준비물', 'title': '준비물 안내', 'body': '로봇 키트를 준비해주세요.'},
        )
        check(response.status_code == 200, '강사의 강좌 전체 공지 발송')

        with client.session_transaction() as session:
            session['emp_no'] = 'ADMIN01'
            session['user_name'] = '관리자'
            session['user_level'] = 2
        response = client.post(f'/parent-notifications/api/parents/{parent_id}/sms', json={})
        sms_data = json_response(response)
        check(response.status_code == 503 and '/parent/register/' in sms_data['sms_message'],
              'SMS 미연동 시 최초 안내문·링크 생성')
        response = client.post(f'/parent-notifications/api/classes/{class_id}/sms', json={})
        class_sms = json_response(response)
        check(response.status_code == 503 and '강사 전용 출결 페이지' in class_sms['sms_message'],
              '강사 전용페이지 SMS 문구 생성')

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['학부모명', '휴대폰', '학생명', '학교명', '학년', '반',
                      '강좌명', '부서', '강사사번', '강사명'])
        sheet.append(['박보호', '01011112222', '박지우', '새담초등학교', '2', '3',
                      '창의미술', '예술', '', ''])
        stream = io.BytesIO()
        workbook.save(stream)
        stream.seek(0)
        response = client.post('/parent-notifications/api/upload', data={
            'file': (stream, 'parents.xlsx'),
        }, content_type='multipart/form-data')
        upload = json_response(response)
        check(response.status_code == 200 and upload['counts']['rows'] == 1,
              '엑셀 학부모 명단 일괄등록')
        check(client.get('/parent-notifications/template.xlsx').status_code == 200,
              '엑셀 업로드 양식 다운로드')

        conn = connection()
        check(conn.execute('SELECT COUNT(*) FROM parent_notifications').fetchone()[0] >= 2,
              '푸시 발송 기록 저장')
        check(conn.execute('SELECT COUNT(*) FROM parent_attendance').fetchone()[0] == 1,
              '학생 출결 이력 저장')
        conn.close()

        check(resolve_request_menu('/parent/register/token') is None,
              '학부모 비회원 경로 메뉴권한 분리')
        check(resolve_request_menu('/parent-notifications/instructor/token') is None,
              '강사 전용 경로 관리자 메뉴권한 분리')
        check(resolve_request_menu('/parent-notifications') == 'parent_notifications',
              '관리자 페이지 업무지원 메뉴권한 연결')

    print('ALL PARENT NOTIFICATION TESTS PASSED')


if __name__ == '__main__':
    run()
