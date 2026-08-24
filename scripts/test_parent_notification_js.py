"""학부모 알림 화면의 렌더링된 인라인 JavaScript 문법 검사."""

import subprocess
import sys
from pathlib import Path

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as application  # noqa: E402


NODE = Path(
    r'C:\Users\lunch\.cache\codex-runtimes\codex-primary-runtime'
    r'\dependencies\node\bin\node.exe'
)


def check_html(name, html):
    soup = BeautifulSoup(html, 'html.parser')
    scripts = [tag.string or tag.get_text() for tag in soup.find_all('script') if not tag.get('src')]
    for index, script in enumerate(scripts, 1):
        result = subprocess.run(
            [str(NODE), '--check'], input=script, text=True,
            capture_output=True, encoding='utf-8',
        )
        if result.returncode:
            raise AssertionError(f'{name} script {index}: {result.stderr}')
    print(f'PASS: {name} 인라인 JavaScript {len(scripts)}개')


with application.app.test_request_context('/parent-notifications'):
    from flask import render_template

    check_html('관리자 화면', render_template('parent_notifications/admin.html'))
    check_html('학부모 등록 화면', render_template(
        'parent_notifications/register.html', invalid=False,
        invite={'guardian_name': '홍길동'}, children=[], notices=[], registered=False,
        token='test-token', push_configured=False,
    ))
    check_html('강사 출결 화면', render_template(
        'parent_notifications/instructor.html', invalid=False, logged_in=True,
        forbidden=False, authorized=True,
        class_info={'class_name': '로봇과학 A반', 'school_name': '새담초등학교',
                    'department': '과학', 'instructor_name': '이강사'},
        students=[], token='test-token',
        notice_kinds=['출석', '지각', '결석', '하원', '준비물', '강사공지'],
    ))

print('ALL PARENT NOTIFICATION JAVASCRIPT TESTS PASSED')
