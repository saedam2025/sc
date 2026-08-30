import os
import sqlite3
import sys
import tempfile

from flask import Flask


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import routes.chat as chat
import routes.expense as expense_routes
import routes.menu_access as menu_access
import routes.school_bp as school_routes
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
            profile_path TEXT,
            status TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath TEXT,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users(emp_no, name, department, position, level, profile_icon, profile_path, status) VALUES
            ('admin', 'admin', '본부', '대표이사', 1, 'A', '', '승인'),
            ('hq-1', '본부직원', '본부', '사원', 5, 'H', '', '승인'),
            ('dir-1', '센터장사용자', '파견', '센터장', 8, 'D', '', '승인'),
            ('wait-1', '승인대기', '파견', '강사', 10, 'W', '', '대기');
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
            (2, 'notice', '2번 학교 안내', '', '센터장2'),
            (1, 'open_class', '강사정보 ID 자료', '', '센터장1'),
            (1, '공개수업', '강사정보 구 명칭 자료', '', '센터장1'),
            (1, '강사정보현황', '강사정보 새 명칭 자료', '', '센터장1'),
            (1, 'survey', '통합조사 ID 자료', '', '센터장1'),
            (1, '만족도조사', '통합조사 구 명칭 자료', '', '센터장1'),
            (1, '공개수업&만족도조사', '통합조사 새 명칭 자료', '', '센터장1'),
            (1, 'director_resources', '1번 학교 기타자료', '', '센터장1'),
            (2, '센터장 기타자료', '2번 학교 기타자료', '', '센터장2');
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
    assert set(query_posts(connection, 1, 'open_class', '강사정보현황')[1]) == {
        '강사정보 ID 자료', '강사정보 구 명칭 자료', '강사정보 새 명칭 자료'
    }
    assert set(query_posts(connection, 1, 'survey', '공개수업&만족도조사')[1]) == {
        '통합조사 ID 자료', '통합조사 구 명칭 자료', '통합조사 새 명칭 자료'
    }
    assert query_posts(connection, 1, 'director_resources', '센터장 기타자료')[1] == ['1번 학교 기타자료']
    assert query_posts(connection, 2, 'director_resources', '센터장 기타자료')[1] == ['2번 학교 기타자료']
    assert school_routes.SCHOOL_CATEGORY_ALIASES['강사정보현황'] == 'open_class'
    assert school_routes.SCHOOL_CATEGORY_ALIASES['공개수업&만족도조사'] == 'survey'
    assert school_routes.SCHOOL_CATEGORY_ALIASES['센터장 기타자료'] == 'director_resources'
    assert not is_shared_board('director_resources')
    connection.close()


def test_reference_upload_limits():
    megabyte = 1024 * 1024
    validate = school_routes.validate_post_attachment_sizes

    assert validate('reference', [100 * megabyte]) == ''
    assert '파일당 100MB 이하' in validate('reference', [100 * megabyte + 1])
    assert validate('자료실', [100 * megabyte] * 10) == ''
    assert '최대 10개' in validate('reference', [1] * 11)
    assert validate('notice', [15 * megabyte]) == ''
    assert '총용량은 최대 15MB' in validate('notice', [15 * megabyte + 1])
    assert school_routes.format_school_file_size(0) == '0B'
    assert school_routes.format_school_file_size(1536) == '2KB'
    assert school_routes.format_school_file_size(10 * megabyte) == '10.0MB'


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

    assert '#orgChartModal .org-status-item' in chat_template
    assert '#orgChartModal .org-status-dot' in chat_template
    assert '#orgChartModal .status-online' in chat_template
    assert 'animation: chat-presence-pulse' in chat_template
    assert "await updateOrgOnlineStatus();" in chat_template

    assert '@media (min-width: 1101px) and (max-width: 2000px) and (max-height: 1200px)' in school_template
    assert 'grid-template-columns: 278px minmax(420px, 1fr) 282px' in school_template
    assert '.dashboard-container.school-detail-spacing .school-chat-card { height: 300px; }' in school_template
    assert '{% with hide_chat_title_icon = true %}' in school_template
    assert '{% if not hide_chat_title_icon %}<i class="fa-regular fa-comment-dots"' in chat_template
    assert "{% block body_class %}{% if view_type == 'detail' %}school-detail-page{% endif %}{% endblock %}" in school_template
    assert '.school-detail-page .main-content { padding-top: 8.4px; }' in school_template
    assert '.school-detail-page .content-container { padding-top: 12.6px; }' in school_template
    assert '.dashboard-container.school-detail-spacing { padding-top: 8.4px; padding-bottom: 10px; }' in school_template
    assert '.dashboard-container.school-detail-spacing .school-container { padding-top: 10.5px; padding-bottom: 12.5px; }' in school_template
    assert '.dashboard-container.school-detail-spacing .school-workspace-shell { padding-top: 13px; padding-bottom: 12px; }' in school_template
    assert "$('#readTitle').text(data.title || '제목 없음');" in school_template
    assert '<span class="read-status-slot">${statusMeta}</span>' in school_template
    assert '<div class="read-action-group">' in school_template
    assert 'class="read-outside-actions"' not in school_template
    assert school_template.index('<div id="commentList" class="comment-list"></div>') < school_template.index('<div class="comment-write"')
    assert 'class="comment-file-picker"' in school_template
    assert 'function updateCommentFileLabel(input, targetId)' in school_template
    assert 'margin: 1px auto 0; padding: 3px 16px 12px;' in school_template
    assert 'function getInstructorStatusTemplate()' in school_template
    assert "const isInstructorStatus = cat === 'open_class';" in school_template
    assert '? getInstructorStatusTemplate()' in school_template
    assert "? '(      )월 강사현황보고'" in school_template
    assert 'function getWeeklyReportTemplate()' in school_template
    assert "const isWeeklyReport = cat === 'weekly_report';" in school_template
    assert '? getWeeklyReportTemplate()' in school_template
    assert "(isWeeklyReport ? '(        )월   (       )주차 주간업무보고' : '')" in school_template
    assert 'function getItemRequestTemplate()' in school_template
    assert "const isItemRequest = cat === 'item_request';" in school_template
    assert '(isItemRequest ? getItemRequestTemplate() : \'\')' in school_template
    assert 'Array.from({ length: 5 }' in school_template
    assert '>요청물품</th>' in school_template
    assert '>사용처</th>' in school_template
    assert '>비고</th>' in school_template
    assert "setInstructorStatusWriteWidth(false);" in school_template
    assert '.board-write-modal-panel.instructor-status-write-wide' in school_template
    assert 'max-width: min(1364px, calc(100vw - 36px));' in school_template
    assert '수익자(선택형) 강사 현황' in school_template
    assert '맞춤형 강사 현황' in school_template
    assert '기타 강사전달사항' in school_template
    assert "['주민번호 (-제외/숫자만기재)', '16%']" in school_template
    assert "['계좌번호 (-제외/숫자만기재)', '17%']" in school_template
    assert 'const beneficiaryRows = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 22, 23, 24];' in school_template
    assert "beneficiaryRows, '#dbeafe'" in school_template
    assert "customizedRows, '#dcfce7'" in school_template
    assert "buildInstructorStatusTable('수익자(선택형) 강사 현황', beneficiaryRows, '#dbeafe', true)" in school_template
    assert "const headingSpacer = extraTopSpace ? '<p style=\"height:1.5em;margin:0;\">&nbsp;</p>' : '';" in school_template
    assert "const monthBlank" not in school_template
    assert '엑셀에서 내용 복사하여 위의 표에 붙여넣기가 가능합니다.' in school_template
    assert '위 형식이 필요없을 시 글과 표를 다 삭제하고 작성하세요.' in school_template
    assert '필요 시 파일첨부가 가능합니다.' in school_template
    assert '파일당 100MB 이하 · 대용량 업로드 진행률 표시' in school_template
    assert 'const REFERENCE_POST_MAX_FILE_SIZE = 100 * 1024 * 1024;' in school_template
    assert 'function submitReferencePostWithProgress(form, btn)' in school_template
    assert "xhr.upload.addEventListener('progress'" in school_template
    assert "$('#postCategory').val(activePostCategory);" in school_template
    assert 'board-list-attachment-size' in school_template
    assert 'const sizes = Array.isArray(data.attachment_sizes) ? data.attachment_sizes : [];' in school_template
    assert 'class="read-file-size">(${sizeLabel})' in school_template
    assert "if(event.target.classList.contains('board-modal-shell')) showBoardList();" not in school_template
    assert '모달은 배경 클릭이나 드래그 후 클릭으로 닫지 않는다.' in school_template


def test_board_list_title_and_status_badge_display():
    template_path = os.path.join(PROJECT_ROOT, 'templates', 'school_bp.html')
    with open(template_path, encoding='utf-8') as file:
        school_template = file.read()

    assert 'class="board-post-title" title="{{ p.title }}"' in school_template
    assert "{{ p.title[:40] }}{% if p.title|length > 40 %}...{% endif %}" in school_template
    assert 'table-layout:fixed;' in school_template
    assert '.board-title-line { display:flex; align-items:center; min-width:0; white-space:nowrap; }' in school_template
    assert '.board-post-title { display:block; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }' in school_template
    assert "{{ '본사접수' if (p.status or '접수') == '접수' else p.status }}" in school_template
    assert "const statusLabel = statusText === '접수' ? '본사접수' : statusText;" in school_template
    assert 'width:64px; min-width:64px; height:30px;' in school_template
    assert 'border:0; background:transparent; box-shadow:none;' in school_template
    assert 'table.board-table .team-review-pending-btn:hover { border:0; background:transparent; color:#b45309; }' in school_template


def test_center_expense_form_prefill(database):
    connection = connect(database)
    connection.executescript(
        """
        CREATE TABLE users (
            emp_no TEXT PRIMARY KEY,
            name TEXT,
            email TEXT,
            bank_account TEXT
        );
        CREATE TABLE schools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_name TEXT,
            center_director_id TEXT,
            center_director_id_2 TEXT,
            year INTEGER,
            is_active INTEGER DEFAULT 1
        );
        INSERT INTO users(emp_no, name, email, bank_account) VALUES
            ('dir-prefill', '자동입력센터장', 'director@example.com', '새담은행 123-456 자동입력센터장');
        INSERT INTO schools(school_name, center_director_id, center_director_id_2, year, is_active) VALUES
            ('이전학교', 'other-director-1', '', 2025, 1),
            ('현재담당학교', 'other-director-2', 'dir-prefill', 2026, 1),
            ('비활성학교', 'other-director-3', '', 2027, 0);
        """
    )
    connection.commit()
    connection.close()

    original_get_db = expense_routes.get_db
    expense_routes.get_db = lambda: connect(database)
    try:
        app = Flask(__name__)
        app.secret_key = 'center-expense-prefill-test'
        with app.test_request_context('/expense/submit/center'):
            from flask import session

            session['emp_no'] = 'dir-prefill'
            session['user_name'] = '자동입력센터장'
            context = expense_routes._expense_submit_page_context(prefer_assigned_school=True)
            assert context['expense_submit_school_name'] == '현재담당학교'
            assert context['expense_submit_manager'] == '자동입력센터장'
            assert context['expense_submit_email'] == 'director@example.com'
            assert context['expense_submit_payment_account'] == '새담은행 123-456 자동입력센터장'

            blank_context = expense_routes._expense_submit_page_context(prefer_assigned_school=False)
            assert blank_context['expense_submit_school_name'] == ''
            assert blank_context['expense_submit_manager'] == ''
            assert blank_context['expense_submit_email'] == ''
            assert blank_context['expense_submit_payment_account'] == ''
    finally:
        expense_routes.get_db = original_get_db


def test_assigned_level_7_can_open_school_workspace(database):
    connection = connect(database)
    connection.executescript(
        """
        CREATE TABLE schools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_name TEXT,
            center_director_id TEXT,
            center_director_id_2 TEXT,
            is_active INTEGER DEFAULT 1
        );
        CREATE TABLE users (
            emp_no TEXT PRIMARY KEY,
            name TEXT,
            status TEXT
        );
        CREATE TABLE menu_access_permissions (
            menu_key TEXT PRIMARY KEY,
            max_level INTEGER NOT NULL,
            updated_by TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users(emp_no, name, status) VALUES
            ('primary-director', '첫번째센터장', '승인'),
            ('dir-team-1', '두번째센터장', '승인'),
            ('another-director', '다른학교센터장', '승인'),
            ('free-director', '미지정센터장', '승인');
        INSERT INTO schools(school_name, center_director_id, center_director_id_2, is_active) VALUES
            ('공동담당학교', 'primary-director', 'dir-team-1', 1),
            ('다른학교', 'another-director', '', 1);
        INSERT INTO admin_settings(key, value) VALUES
            ('school_director_scope_enabled', '1');
        INSERT INTO menu_access_permissions(menu_key, max_level) VALUES
            ('school_group', 5),
            ('school_workspace', 6),
            ('school_calendar', 6),
            ('workspace_group', -1),
            ('memo_main', -1),
            ('school_center_boards', 14),
            ('school_center_shared', 8),
            ('school_center_shared_read', 8),
            ('school_center_shared_write', 5),
            ('school_center_shared_delete', 5),
            ('school_center_shared_comment', 8);
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
            assert menu_access.center_director_mode_active(7) is True
            assert menu_access.enforce_request_menu_access() is None
            assert school_routes.can_manage_schools() is False

            # 상단 업무공간/화이트보드 메뉴 권한이 차단돼 있어도 전용모드에서는
            # 프로필 카드 아이콘을 통한 개인화이트보드 직접 접속을 허용한다.
            with app.test_request_context('/memo/'):
                session['user_name'] = '센터장팀장'
                session['emp_no'] = 'dir-team-1'
                session['user_level'] = 7
                assert menu_access.center_director_mode_active(7) is True
                assert menu_access.enforce_request_menu_access() is None

            connection = connect(database)
            assert school_routes.can_access_school(connection, 1) is True
            assert school_routes.can_access_school(connection, 2) is False
            assert school_routes._validate_school_directors(
                connection, ['primary-director', 'free-director'], school_id=1
            ) == ''
            duplicate_error = school_routes._validate_school_directors(
                connection, ['free-director', 'dir-team-1'], school_id=2
            )
            assert '이미 공동담당학교 센터장으로 지정' in duplicate_error
            assert '중복 지정' in school_routes._validate_school_directors(
                connection, ['free-director', 'free-director'], school_id=1
            )
            connection.close()
            # 상위 학교관리 권한(레벨 5)이 더 낮아도 센터장 전용 행의
            # 권한이 우선하므로 담당 학교의 메뉴가 사라지지 않는다.
            assert school_routes.can_access_school_category('notice') is True
            assert school_routes.can_access_school_category('community') is True
            assert menu_access.shared_board_action_is_allowed('read') is True
            assert menu_access.shared_board_action_is_allowed('comment') is True
            assert menu_access.shared_board_action_is_allowed('write') is False
            assert menu_access.shared_board_action_is_allowed('delete') is False

            # 일반 9개 메뉴의 일괄 권한을 끄면 표시와 직접 URL이 함께 막힌다.
            # 본부 공용 2개 메뉴는 접근/읽기/쓰기/삭제/댓글을 별도 제어한다.
            connection = connect(database)
            connection.execute(
                "UPDATE menu_access_permissions SET max_level=14 WHERE menu_key='school_group'"
            )
            connection.execute(
                "UPDATE menu_access_permissions SET max_level=-1 WHERE menu_key='school_center_boards'"
            )
            connection.commit()
            connection.close()
            assert school_routes.can_access_school_category('notice') is False
            assert school_routes.can_access_school_category('expense') is False
            assert school_routes.can_access_school_category('director_resources') is False
            assert school_routes.can_access_school_category('community') is True
            assert school_routes.can_access_school_category('reference') is True
            assert menu_access.shared_board_action_is_allowed('access') is True
            assert menu_access.shared_board_action_is_allowed('read') is True
            assert menu_access.shared_board_action_is_allowed('comment') is True
            assert menu_access.shared_board_action_is_allowed('write') is False
            assert menu_access.shared_board_action_is_allowed('delete') is False

            with app.test_request_context('/expense/submit/center'):
                session['user_name'] = '센터장팀장'
                session['emp_no'] = 'dir-team-1'
                session['user_level'] = 7
                denied = menu_access.enforce_request_menu_access()
                assert denied is not None
                assert denied[1] == 403

            # 체크를 끄면 레벨 7의 기존 본사 권한으로 되돌아간다.
            connection = connect(database)
            connection.execute(
                "UPDATE admin_settings SET value='0' WHERE key='school_director_scope_enabled'"
            )
            connection.commit()
            connection.close()
            assert menu_access.center_director_mode_active(7) is False
            assert school_routes.can_manage_schools() is True
            with app.test_request_context('/memo/'):
                session['user_name'] = '센터장팀장'
                session['emp_no'] = 'dir-team-1'
                session['user_level'] = 7
                denied = menu_access.enforce_request_menu_access()
                assert denied is not None
                assert denied[1] == 403
            connection = connect(database)
            assert school_routes.can_access_school(connection, 2) is True
            connection.execute(
                "UPDATE admin_settings SET value='1' WHERE key='school_director_scope_enabled'"
            )
            connection.commit()
            connection.close()

            # 직급 변경 후 재로그인 전처럼 세션 레벨이 오래된 값이어도
            # 실제 학교 지정이 접근권한보다 우선해야 한다.
            session['user_level'] = 12
            access = menu_access.build_menu_access(12)
            assert access['school_group'] is True
            assert access['school_workspace'] is True
            assert menu_access.enforce_request_menu_access() is None

            connection = connect(database)
            assert school_routes.can_access_school(connection, 1) is True
            connection.close()
    finally:
        menu_access.get_db = original_get_db


def main():
    with tempfile.TemporaryDirectory(prefix='saedam-center-director-test-') as directory:
        test_chat_organization(os.path.join(directory, 'chat.db'))
        test_shared_boards(os.path.join(directory, 'school.db'))
        test_reference_upload_limits()
        test_org_chart_function_names_are_isolated()
        test_board_list_title_and_status_badge_display()
        test_center_expense_form_prefill(os.path.join(directory, 'expense-prefill.db'))
        test_assigned_level_7_can_open_school_workspace(
            os.path.join(directory, 'level-7-director.db')
        )
    print('Center director workspace test: PASS')


if __name__ == '__main__':
    main()
