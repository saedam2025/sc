from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify, current_app, abort, send_file
from routes.database import get_db
from routes.organization import classify_organization_group
from routes.menu_access import center_director_mode_active, school_director_scope_enabled
import os
import math
import json
import secrets
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote, unquote
from routes.secure_files import delete_file, encrypted_file_is_readable, encrypted_response, encrypted_storage_name, encrypt_upload, original_filename, plaintext_size
from routes.storage import APP_ROOT, SCHOOL_UPLOADS, UPLOADS_ROOT

school_bp = Blueprint('school', __name__)

SHARED_BOARD_CATEGORIES = {'community', '본부공지사항', 'reference', '자료실'}
POST_MAX_FILES = 10
POST_MAX_TOTAL_SIZE = 15 * 1024 * 1024
FILENAME_ENCODING_PREFIX = '~e~'

def is_shared_board(category):
    """모든 센터장 업무공간에서 같은 게시물을 표시하는 게시판인지 반환한다."""
    return str(category or '').strip() in SHARED_BOARD_CATEGORIES


def build_school_post_list_queries(school_id, category, category_name, search_query=''):
    """공유 게시판은 학교 조건 없이, 개별 게시판은 해당 학교 조건으로 조회한다."""
    if is_shared_board(category):
        query_params = [category, category_name]
        where_clause = "(category = ? OR category = ?)"
    else:
        query_params = [school_id, category, category_name]
        where_clause = "school_id = ? AND (category = ? OR category = ?)"

    if search_query:
        where_clause += " AND (title LIKE ? OR author LIKE ? OR content LIKE ?)"
        search_value = f"%{search_query}%"
        query_params.extend([search_value, search_value, search_value])

    return (
        f"SELECT COUNT(*) FROM school_posts WHERE {where_clause}",
        f"SELECT * FROM school_posts WHERE {where_clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        query_params,
    )

def get_session_user_level(default=99):
    try:
        return int(session.get('user_level', default))
    except (TypeError, ValueError):
        return default

def can_manage_shared_board():
    return bool(session.get('user_name')) and 1 <= get_session_user_level() <= 5


def can_manage_schools():
    """본사 계정만 학교 기본정보를 관리할 수 있다."""
    if session.get('user_name') == 'admin':
        return True
    level = get_session_user_level()
    if not session.get('emp_no') or not 1 <= level <= 7:
        return False
    # 전용모드가 켜진 레벨 7 센터장(팀장)은 본사 관리자가 아니라
    # 담당 학교 센터장 권한으로 동작한다.
    return not center_director_mode_active(level)


def can_access_school(conn, school_id):
    """본사 계정 또는 해당 학교에 지정된 센터장만 접근을 허용한다."""
    if not session.get('emp_no'):
        return False
    assigned_school = conn.execute(
        """
        SELECT 1
        FROM schools
        WHERE id = ?
          AND center_director_id = ?
          AND COALESCE(is_active, 1) = 1
        """,
        (school_id, session.get('emp_no')),
    ).fetchone()
    if assigned_school is not None:
        return True
    if center_director_mode_active(get_session_user_level(), conn):
        return False
    if can_manage_schools():
        return True
    if get_session_user_level() != 8:
        return False
    if not school_director_scope_enabled(conn):
        return True
    return False


def can_access_post(conn, school_id, category):
    """본부공지사항·자료실은 전역 공개하고, 그 외 글은 담당 학교에만 공개한다."""
    if is_shared_board(category):
        if session.get('emp_no'):
            active_assignment = conn.execute(
                """
                SELECT 1
                FROM schools
                WHERE center_director_id = ?
                  AND COALESCE(is_active, 1) = 1
                LIMIT 1
                """,
                (session.get('emp_no'),),
            ).fetchone()
            if active_assignment is not None:
                return True
        if center_director_mode_active(get_session_user_level(), conn):
            return False
        if can_manage_schools():
            return True
        if not session.get('emp_no') or get_session_user_level() != 8:
            return False
        if not school_director_scope_enabled(conn):
            return True
        active_assignment = conn.execute(
            """
            SELECT 1
            FROM schools
            WHERE center_director_id = ?
              AND COALESCE(is_active, 1) = 1
            LIMIT 1
            """,
            (session.get('emp_no'),)
        ).fetchone()
        return active_assignment is not None
    return can_access_school(conn, school_id)


def get_school_access_key(conn, school_id):
    school = conn.execute(
        "SELECT access_key FROM schools WHERE id = ?",
        (school_id,)
    ).fetchone()
    return school['access_key'] if school else None


def redirect_to_school(school_id, **values):
    conn = get_db()
    access_key = get_school_access_key(conn, school_id)
    conn.close()
    if not access_key:
        return redirect(url_for('school.school_list'))
    return redirect(
        url_for('school.school_detail', school_key=access_key, **values)
    )


def school_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not can_manage_schools():
            return "학교 관리 권한이 없습니다.", 403
        return view(*args, **kwargs)
    return wrapped

def get_upload_dir():
    upload_dir = str(SCHOOL_UPLOADS)
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def _stored_name_from_reference(reference):
    return os.path.basename(str(reference or '').replace('\\', '/'))


def _encode_filename(name):
    """쉼표 구분형 레거시 DB에서도 실제 파일명을 손실 없이 보존한다."""
    return FILENAME_ENCODING_PREFIX + quote(str(name or ''), safe='')


def _decode_filename(name):
    value = str(name or '')
    if value.startswith(FILENAME_ENCODING_PREFIX):
        return unquote(value[len(FILENAME_ENCODING_PREFIX):])
    return value


def _stored_path_from_reference(reference):
    name = _stored_name_from_reference(reference)
    if not name:
        return ''
    persistent_path = os.path.join(get_upload_dir(), name)
    if os.path.exists(persistent_path):
        return persistent_path
    # 2026-08 이전 첨부는 Flask 정적 폴더에 저장되어 있었다. 새 파일은
    # 영속 저장소만 사용하되, 기존 자료의 다운로드/삭제 호환성은 유지한다.
    legacy_path = os.path.join(current_app.root_path, 'static', 'school_uploads', name)
    return legacy_path if os.path.exists(legacy_path) else persistent_path


def _delete_references(reference_csv):
    for reference in str(reference_csv or '').split(','):
        path = _stored_path_from_reference(reference.strip())
        if path:
            delete_file(path)


def _secure_reference_csv(reference_csv):
    """기존 정적 URL도 권한검사 다운로드 URL로 바꾸어 외부 노출을 막는다."""
    references = [item.strip() for item in str(reference_csv or '').split(',') if item.strip()]
    return ','.join(
        url_for('school.serve_school_file', stored_name=_stored_name_from_reference(item))
        for item in references
    )

def get_uploaded_file_size(file):
    """업로드 스트림 위치를 보존하면서 실제 바이트 크기를 계산한다."""
    stream = getattr(file, 'stream', None)
    if stream is None:
        return 0
    try:
        current_position = stream.tell()
    except (AttributeError, OSError):
        current_position = 0
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
    finally:
        stream.seek(current_position)
    return max(0, int(size))

def get_stored_file_size(reference):
    if not reference:
        return 0
    return plaintext_size(_stored_path_from_reference(reference))

def init_school_comment_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS school_post_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            parent_id INTEGER,
            content TEXT NOT NULL,
            author TEXT,
            filename TEXT,
            filepath TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT,
            FOREIGN KEY(post_id) REFERENCES school_posts(id) ON DELETE CASCADE,
            FOREIGN KEY(parent_id) REFERENCES school_post_comments(id) ON DELETE CASCADE
        )
    """)

    columns = [row[1] for row in conn.execute("PRAGMA table_info(school_post_comments)").fetchall()]
    if 'filename' not in columns:
        conn.execute("ALTER TABLE school_post_comments ADD COLUMN filename TEXT")
    if 'filepath' not in columns:
        conn.execute("ALTER TABLE school_post_comments ADD COLUMN filepath TEXT")
        
    columns_s = [row[1] for row in conn.execute("PRAGMA table_info(schools)").fetchall()]
    if 'is_active' not in columns_s:
        conn.execute("ALTER TABLE schools ADD COLUMN is_active INTEGER DEFAULT 1")
    if 'contract_subject' not in columns_s:
        conn.execute("ALTER TABLE schools ADD COLUMN contract_subject TEXT")
    if 'office_location' not in columns_s:
        conn.execute("ALTER TABLE schools ADD COLUMN office_location TEXT")
    if 'school_address' not in columns_s:
        conn.execute("ALTER TABLE schools ADD COLUMN school_address TEXT")
    if 'school_phone' not in columns_s:
        conn.execute("ALTER TABLE schools ADD COLUMN school_phone TEXT")
    if 'school_email' not in columns_s:
        conn.execute("ALTER TABLE schools ADD COLUMN school_email TEXT")
        
    # 🚀 [신규] 메인 캘린더와 완전히 분리된 학교 전용 일정 테이블 자동 생성
    conn.execute("""
        CREATE TABLE IF NOT EXISTS school_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            school_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            start_date TEXT NOT NULL,
            start_time TEXT,
            end_date TEXT NOT NULL,
            end_time TEXT,
            note TEXT,
            owner TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS weblinks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT,
            favicon_url TEXT,
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_weblink_order (
            user_name TEXT PRIMARY KEY,
            order_json TEXT
        )
    """)
    columns_w = [row[1] for row in conn.execute("PRAGMA table_info(weblinks)").fetchall()]
    if 'type' not in columns_w:
        conn.execute("ALTER TABLE weblinks ADD COLUMN type TEXT DEFAULT 'url'")
    if 'filename' not in columns_w:
        conn.execute("ALTER TABLE weblinks ADD COLUMN filename TEXT")
    if 'filepath' not in columns_w:
        conn.execute("ALTER TABLE weblinks ADD COLUMN filepath TEXT")
    conn.commit()

def save_uploaded_files(files):
    filenames, filepaths = [], []
    saved_paths = []
    try:
        for file in files:
            if file and file.filename:
                display_name = original_filename(file.filename)
                stored_name = encrypted_storage_name(display_name)
                save_path = os.path.join(get_upload_dir(), stored_name)
                encrypt_upload(file, save_path)
                saved_paths.append(save_path)
                filenames.append(_encode_filename(display_name))
                filepaths.append(url_for('school.serve_school_file', stored_name=stored_name))
    except Exception:
        for save_path in saved_paths:
            delete_file(save_path)
        raise
    return (",".join(filenames) if filenames else None, ",".join(filepaths) if filepaths else None)

@school_bp.route('/')
def school_list():
    conn = get_db()
    init_school_comment_table(conn)
    
    # 💡 세션에서 사용자 정보(레벨, 사번) 가져오기
    user_level = get_session_user_level()
    emp_no = session.get('emp_no')

    assigned_school = None
    if emp_no:
        assigned_school = conn.execute(
            """
            SELECT id, access_key
            FROM schools
            WHERE center_director_id = ? AND COALESCE(is_active, 1) = 1
            ORDER BY year DESC
            LIMIT 1
            """,
            (emp_no,),
        ).fetchone()

    # 메뉴 권한관리의 '센터장 지정공간 제한'이 켜진 경우만 담당 학교로 이동한다.
    director_scope_applies = center_director_mode_active(user_level, conn)
    # 직급/레벨 세션이 변경 전 값이어도 실제 담당 학교가 있는 일반 회원은
    # 자신의 학교로 이동시킨다. 레벨 1~7 관리자는 기존 전체 목록을 유지한다.
    assigned_member_scope_applies = (
        assigned_school is not None
        and user_level not in {7, 8}
        and not can_manage_schools()
    )
    if director_scope_applies or assigned_member_scope_applies:
        # 해당 사번(emp_no)이 센터장으로 지정된 활성 상태의 최신 학교를 찾습니다.
        my_school = assigned_school
        
        if my_school:
            conn.close()
            # 담당 학교의 예측 불가능한 접근 키로 상세 대시보드에 이동합니다.
            return redirect(
                url_for(
                    'school.school_detail',
                    school_key=my_school['access_key']
                )
            )
        else:
            conn.close()
            return "담당으로 지정된 학교가 없습니다. 본사 관리자에게 문의해주세요.", 403

    # 지정공간 제한을 끄면 레벨 7·8은 기존 레벨 권한으로 목록과 공간을 열람한다.
    # 학교 등록·수정·삭제는 school_admin_required로 계속 본사 권한만 허용된다.
    if not can_manage_schools() and user_level != 8:
        conn.close()
        return "학교 업무공간 접근 권한이 없습니다.", 403

    # 레벨 1~7 (본사 관리자 등)은 기존처럼 전체 학교 목록 표시
    rows = conn.execute('''
        SELECT s.*, u.name as director_name, u.phone as director_phone, u.profile_path as director_photo, u.profile_icon as director_icon
        FROM schools s
        LEFT JOIN users u ON s.center_director_id = u.emp_no
        ORDER BY s.year DESC, COALESCE(s.is_active, 1) DESC, s.school_name ASC
    ''').fetchall()
    conn.close()

    schools_by_year = {}
    for r in rows:
        row_dict = dict(r)
        year = row_dict['year']
        if year not in schools_by_year:
            schools_by_year[year] = []
        schools_by_year[year].append(row_dict)

    return render_template('school_bp.html', schools_by_year=schools_by_year, view_type='list')
@school_bp.route('/edit_school', methods=['POST'])
@school_admin_required
def edit_school():
    data = request.form
    school_id = data.get('school_id')
    
    conn = get_db()
    init_school_comment_table(conn)
    try:
        conn.execute('''
            UPDATE schools 
            SET year=?, school_name=?, contract_subject=?, office_phone=?, office_location=?,
                school_address=?, school_phone=?, school_email=?,
                neulbom_assistant=?, neulbom_manager=?, center_director_id=?
            WHERE id=?
        ''', (
            data.get('year'), data.get('school_name'), data.get('contract_subject', ''),
            data.get('office_phone', ''), data.get('office_location', ''),
            data.get('school_address', ''), data.get('school_phone', ''), data.get('school_email', ''),
            data.get('neulbom_assistant', ''), data.get('neulbom_manager', ''), data.get('center_director_id', ''),
            school_id
        ))
        conn.commit()
    except Exception as e:
        print(f"Error updating school: {e}")
    finally:
        conn.close()
        
    return redirect(url_for('school.school_list'))

@school_bp.route('/register', methods=['POST'])
@school_admin_required
def register_school():
    data = request.form
    conn = get_db()
    init_school_comment_table(conn)
    try:
        conn.execute('''
            INSERT INTO schools (
                access_key, year, school_name, contract_subject, office_phone, office_location,
                school_address, school_phone, school_email,
                neulbom_assistant, neulbom_manager, center_director_id, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (
            secrets.token_urlsafe(24),
            data.get('year'), data.get('school_name'), data.get('contract_subject', ''),
            data.get('office_phone', ''), data.get('office_location', ''),
            data.get('school_address', ''), data.get('school_phone', ''), data.get('school_email', ''),
            data.get('neulbom_assistant', ''), data.get('neulbom_manager', ''), data.get('center_director_id', '')
        ))
        conn.commit()
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
    return redirect(url_for('school.school_list'))

@school_bp.route('/delete_schools', methods=['POST'])
@school_admin_required
def delete_schools():
    school_ids = request.form.getlist('school_ids')
    if school_ids:
        conn = get_db()
        for sid in school_ids:
            conn.execute('DELETE FROM schools WHERE id = ?', (sid,))
        conn.commit()
        conn.close()
    return redirect(url_for('school.school_list'))

@school_bp.route('/toggle_schools', methods=['POST'])
@school_admin_required
def toggle_schools():
    school_ids = request.form.getlist('school_ids')
    if school_ids:
        conn = get_db()
        for sid in school_ids:
            conn.execute('UPDATE schools SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?', (sid,))
        conn.commit()
        conn.close()
    return redirect(url_for('school.school_list'))

@school_bp.route('/<string:school_key>')
def school_detail(school_key):
    # 기본 접속 메뉴를 'notice'에서 'community'(본부공지사항)로 변경
    category = request.args.get('category', 'community')
    page = max(1, request.args.get('page', 1, type=int) or 1)
    search_query = request.args.get('search', '').strip()

# 커뮤니티를 본부공지사항으로 변경하고 맨 앞으로 이동
    school_categories = [
        {'id': 'community', 'name': '본부공지사항', 'icon': 'fa-bullhorn'},
        {'id': 'notice', 'name': '수강안내문', 'icon': 'fa-circle-info'},
        {'id': 'weekly_report', 'name': '주간업무보고', 'icon': 'fa-list-check'},
        {'id': 'open_class', 'name': '공개수업', 'icon': 'fa-chalkboard-user'},
        {
            'id': 'expense',
            'name': '지출결의서',
            'icon': 'fa-file-invoice-dollar',
            'url': '/expense/submit'
        },
        {'id': 'item_request', 'name': '물품요청', 'icon': 'fa-box'},
        {'id': 'work_schedule', 'name': '근무표', 'icon': 'fa-calendar-days'},
        {'id': 'billing', 'name': '청구관련', 'icon': 'fa-receipt'},
        {'id': 'survey', 'name': '만족도조사', 'icon': 'fa-chart-simple'},
        {'id': 'reference', 'name': '자료실', 'icon': 'fa-file-zipper'}
    ]

    cat_id_to_name = {cat['id']: cat['name'] for cat in school_categories}
    cat_name_to_id = {cat['name']: cat['id'] for cat in school_categories}

    if category in cat_name_to_id:
         search_category = cat_name_to_id[category]
         current_category_name = category
    else:
         search_category = category
         current_category_name = cat_id_to_name.get(category, category)

    can_manage_current_board = (
        not is_shared_board(search_category)
        or can_manage_shared_board()
    )
    
    per_page = 7
    
    conn = get_db()
    init_school_comment_table(conn)
    
    school = conn.execute('''
        SELECT s.*, u.name as director_name, u.profile_path as director_photo, 
               u.position as director_pos, u.phone as director_phone, u.email as director_email, u.profile_icon as director_icon
        FROM schools s
        LEFT JOIN users u ON s.center_director_id = u.emp_no
        WHERE s.access_key = ?
    ''', (school_key,)).fetchone()
    
    school_dict = dict(school) if school else None

    if not school_dict:
        conn.close()
        return "학교 업무공간을 찾을 수 없습니다.", 404

    school_id = school_dict['id']
    if not can_access_school(conn, school_id):
        conn.close()
        return "담당 학교 업무공간만 이용할 수 있습니다.", 403

    if search_category == 'expense':
        conn.close()
        return redirect('/expense/submit')

    count_query, data_query, query_params = build_school_post_list_queries(
        school_id, search_category, current_category_name, search_query
    )
    
    total_posts = conn.execute(count_query, query_params).fetchone()[0]
    total_pages = math.ceil(total_posts / per_page)

    # 삭제나 잘못된 직접 URL로 현재 페이지가 범위를 벗어나도
    # 빈 목록 또는 음수 OFFSET 대신 마지막 유효 페이지를 표시한다.
    if total_pages and page > total_pages:
        page = total_pages
    offset = (page - 1) * per_page

    query_params.extend([per_page, offset])
    posts = conn.execute(data_query, query_params).fetchall()
    
    current_user_name = session.get('user_name')
    users_list = conn.execute('''
        SELECT name, profile_icon, profile_path, department, position, level
        FROM users 
        WHERE status = '승인' AND emp_no != 'admin'
        ORDER BY level ASC, name ASC
    ''').fetchall()

    chat_partners = conn.execute('''
        SELECT DISTINCT CASE WHEN sender = ? THEN receiver ELSE sender END AS name
        FROM messages 
        WHERE (sender = ? OR receiver = ?) AND name != 'admin'
    ''', (current_user_name, current_user_name, current_user_name)).fetchall()

    received_messages = conn.execute('''
        SELECT * FROM messages WHERE receiver = ? AND sender != 'admin' ORDER BY sent_at DESC LIMIT 50
    ''', (current_user_name,)).fetchall()

    sent_messages = conn.execute('''
        SELECT * FROM messages WHERE sender = ? AND receiver != 'admin' ORDER BY sent_at DESC LIMIT 50
    ''', (current_user_name,)).fetchall()

    user_rows = conn.execute("SELECT name, profile_icon FROM users WHERE emp_no != 'admin'").fetchall()
    user_icons = {row['name']: row['profile_icon'] or '👤' for row in user_rows}
    
    # [독립] 해당 학교에 종속된 전용 일정만 불러오기
    school_tasks_db = conn.execute("SELECT * FROM school_tasks WHERE school_id = ?", (school_id,)).fetchall()
    school_tasks = [dict(t) for t in school_tasks_db]

    # 전사 메인 달력에 본인이 등록한 오늘부터 7일간의 일정 요약
    weekly_task_groups = []
    weekly_group_map = {}
    today_date = datetime.now().date()
    week_end_date = today_date + timedelta(days=6)
    # school_calendar.html의 getTaskColor()와 같은 일정 분류 색상
    task_category_colors = {
        '수강생모집': '#059669',
        '추가모집': '#047857',
        '학교결재': '#d97706',
        '공개수업': '#7c3aed',
        '체험부스': '#db2777',
        '발표회': '#0284c7',
        '기타': '#475569',
    }

    def restore_legacy_korean(value):
        """과거 CP949 바이트가 Latin-1 문자로 저장된 일정 텍스트를 복원한다."""
        text = str(value or '')
        try:
            restored = text.encode('latin1').decode('cp949')
            return restored if restored else text
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text

    try:
        owner_variants = [current_user_name]
        try:
            legacy_owner = current_user_name.encode('cp949').decode('latin1')
            if legacy_owner not in owner_variants:
                owner_variants.append(legacy_owner)
        except (AttributeError, UnicodeEncodeError, UnicodeDecodeError):
            pass

        owner_placeholders = ','.join('?' for _ in owner_variants)
        my_week_tasks = conn.execute('''
            SELECT t.*
            FROM school_tasks t
            WHERE t.owner IN (''' + owner_placeholders + ''')
              AND t.start_date <= ?
              AND COALESCE(NULLIF(t.end_date, ''), t.start_date) >= ?
            ORDER BY t.start_date ASC, t.start_time ASC, t.id ASC
        ''', (*owner_variants, week_end_date.isoformat(), today_date.isoformat())).fetchall()
        weekday_names = ['월', '화', '수', '목', '금', '토', '일']
        for row in my_week_tasks:
            start_date = datetime.strptime(str(row['start_date'])[:10], '%Y-%m-%d').date()
            raw_end_date = str(row['end_date'] or row['start_date'])[:10]
            end_date = datetime.strptime(raw_end_date, '%Y-%m-%d').date()
            display_start_date = max(start_date, today_date)
            display_end_date = min(max(end_date, start_date), week_end_date)
            start_time = str(row['start_time'] or '').strip()
            end_time = str(row['end_time'] or '').strip()
            time_label = start_time
            if start_time and end_time:
                time_label = f'{start_time}~{end_time}'

            if display_end_date > display_start_date:
                date_label = (
                    f'{display_start_date.month}/{display_start_date.day}'
                    f'~{display_end_date.month}/{display_end_date.day}'
                )
                day_name = (
                    f'{weekday_names[display_start_date.weekday()]}'
                    f'~{weekday_names[display_end_date.weekday()]}'
                )
            else:
                date_label = f'{display_start_date.month}/{display_start_date.day}'
                day_name = weekday_names[display_start_date.weekday()]

            task_title = restore_legacy_korean(row['title']) or '일정'
            task_note = restore_legacy_korean(row['note']).strip()
            group_key = (date_label, day_name)
            if group_key not in weekly_group_map:
                weekly_group_map[group_key] = {
                    'date': display_start_date.isoformat(),
                    'day_name': day_name,
                    'date_label': date_label,
                    'events': [],
                }
                weekly_task_groups.append(weekly_group_map[group_key])

            weekly_group_map[group_key]['events'].append({
                'category': task_title,
                'title': task_note,
                'time': time_label,
                'color': task_category_colors.get(task_title, '#2563eb'),
            })
    except Exception as e:
        print(f"학교 업무공간 주간 업무 요약 로드 에러: {e}")

    weblinks_db = conn.execute("SELECT * FROM weblinks").fetchall()
    weblinks = [dict(row) for row in weblinks_db]
    order_row = conn.execute("SELECT order_json FROM user_weblink_order WHERE user_name = ?", (current_user_name,)).fetchone()
    if order_row and order_row['order_json']:
        try:
            order_list = json.loads(order_row['order_json'])
            order_dict = {int(id_val): index for index, id_val in enumerate(order_list)}
            weblinks.sort(key=lambda x: order_dict.get(x['id'], 999999))
        except Exception:
            pass

    # 모든 센터장이 공유하는 학교갤러리(범위 0) 최신 게시물 미리보기
    gallery_preview_items = []
    try:
        gallery_rows = conn.execute('''
            SELECT p.id, p.title, p.author, p.created_at, t.name AS tab_name,
                   (
                       SELECT COUNT(*)
                       FROM gall2 AS post_gallery
                       WHERE post_gallery.post_id = p.id
                   ) AS photo_count,
                   (
                       SELECT thumb_name
                       FROM gall2 AS cover_gallery
                       WHERE cover_gallery.post_id = p.id
                       ORDER BY cover_gallery.id ASC
                       LIMIT 1
                   ) AS thumb_name
            FROM gall2_posts p
            LEFT JOIN gall2_tabs t ON p.tab_id = t.id
            WHERE p.school_id = 0
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT 5
        ''').fetchall()
        gallery_preview_items = [dict(row) for row in gallery_rows]
    except Exception as e:
        print(f"학교 업무공간 학교갤러리 미리보기 로드 에러: {e}")
            
    conn.close()
    
    pagination = {
        'page': page, 'per_page': per_page, 'total_pages': total_pages,
        'start_page': ((page - 1) // 7) * 7 + 1,
        'end_page': min(((page - 1) // 7) * 7 + 7, total_pages),
        'has_prev': page > 1, 'has_next': page < total_pages,
        'search': search_query, 'total_posts': total_posts 
    }
    
    return render_template('school_bp.html', 
                           school=school, posts=posts, category=search_category,
                           school_categories=school_categories,
                           current_category_name=current_category_name,
                           users_list=users_list, 
                           chat_partners=chat_partners,
                           received_messages=received_messages,
                           sent_messages=sent_messages,
                            user_icons=user_icons,
                            pagination=pagination, 
                            school_tasks=school_tasks, # 미니 달력용 학교 전용 데이터 전달
                             weekly_task_groups=weekly_task_groups,
                             can_manage_current_board=can_manage_current_board,
                            weblinks=weblinks,
                            gallery_preview_items=gallery_preview_items,
                            view_type='detail')

@school_bp.route('/weblink-file/<int:link_id>')
def serve_weblink_file(link_id):
    conn = get_db()
    link = conn.execute(
        "SELECT type, filename, filepath FROM weblinks WHERE id=?",
        (link_id,)
    ).fetchone()
    conn.close()

    if not link or link['type'] != 'file' or not link['filepath']:
        return "파일을 찾을 수 없습니다.", 404

    stored_name = os.path.basename(str(link['filepath'] or '').replace('\\', '/'))
    file_path = os.path.join(str(UPLOADS_ROOT), stored_name) if stored_name else ''
    if not os.path.isfile(file_path):
        return "파일을 찾을 수 없습니다.", 404
    if not encrypted_file_is_readable(file_path):
        safe_name = original_filename(link['filename'] or os.path.basename(file_path))
        fallback = APP_ROOT / 'static' / safe_name
        if fallback.is_file() and fallback.stat().st_size == plaintext_size(file_path):
            return send_file(fallback, as_attachment=True, download_name=safe_name)
        return "이 파일은 이전 암호화 키로 저장되어 복구할 수 없습니다. 파일을 삭제한 뒤 다시 등록해 주세요.", 409
    return encrypted_response(file_path, link['filename'] or os.path.basename(file_path), as_attachment=True)


@school_bp.route('/file/<stored_name>')
def serve_school_file(stored_name):
    safe_stored_name = os.path.basename(stored_name)
    conn = get_db()
    try:
        rows = conn.execute('''
            SELECT p.school_id, p.category, p.filename, p.filepath
            FROM school_posts p
            WHERE COALESCE(p.filepath, '') LIKE ?
            UNION ALL
            SELECT p.school_id, p.category, c.filename, c.filepath
            FROM school_post_comments c
            JOIN school_posts p ON p.id=c.post_id
            WHERE COALESCE(c.filepath, '') LIKE ?
        ''', (f'%{safe_stored_name}%', f'%{safe_stored_name}%')).fetchall()
        for row in rows:
            if not can_access_post(conn, row['school_id'], row['category']):
                continue
            names = str(row['filename'] or '').split(',')
            paths = str(row['filepath'] or '').split(',')
            for index, reference in enumerate(paths):
                if _stored_name_from_reference(reference) != safe_stored_name:
                    continue
                display_name = _decode_filename(names[index]) if index < len(names) else safe_stored_name
                return encrypted_response(
                    _stored_path_from_reference(reference), display_name, as_attachment=True
                )
    finally:
        conn.close()
    abort(404)

# 🚀 [신규 API] 학교 전용 일정을 저장하는 완전히 분리된 라우터
@school_bp.route('/save_task', methods=['POST'])
def save_school_task():
    data = request.get_json()
    school_id = data.get('school_id')
    title = data.get('title')
    start_date = data.get('start_date')
    end_date = data.get('end_date')
    start_time = data.get('start_time')
    end_time = data.get('end_time')
    note = data.get('note')
    owner = session.get('user_name')

    if not title or not start_date or not school_id:
        return jsonify({'ok': False, 'message': '필수 값이 누락되었습니다.'}), 400

    conn = get_db()
    init_school_comment_table(conn)
    if not can_access_school(conn, school_id):
        conn.close()
        return jsonify({
            'ok': False,
            'message': '담당 학교 일정만 등록할 수 있습니다.'
        }), 403
    try:
        conn.execute('''
            INSERT INTO school_tasks (school_id, title, start_date, start_time, end_date, end_time, note, owner)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (school_id, title, start_date, start_time, end_date, end_time, note, owner))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'message': str(e)}), 500
    finally:
        conn.close()

@school_bp.route('/employee_search')
@school_admin_required
def employee_search():
    query = request.args.get('query', '')
    conn = get_db()
    users = conn.execute("""
        SELECT emp_no, name, position, department, level
        FROM users 
        WHERE (name LIKE ? OR emp_no LIKE ?) AND emp_no != 'admin'
        ORDER BY level ASC, name ASC
    """, (f'%{query}%', f'%{query}%')).fetchall()
    conn.close()
    result = []
    for user in users:
        item = dict(user)
        item['organization_group'] = classify_organization_group(
            item.get('department'), item.get('position')
        )
        result.append(item)
    return jsonify(result)

@school_bp.route('/post/api/<int:post_id>')
def get_post_api(post_id):
    conn = get_db()
    init_school_comment_table(conn)
    post = conn.execute("SELECT * FROM school_posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return jsonify({}), 404

    if not can_access_post(conn, post['school_id'], post['category']):
        conn.close()
        return jsonify({'message': '담당 학교 게시글만 볼 수 있습니다.'}), 403

    comment_count = conn.execute(
        "SELECT COUNT(*) FROM school_post_comments WHERE post_id=?",
        (post_id,)
    ).fetchone()[0]
    conn.close()

    data = dict(post)
    if data.get('filepath'):
        data['filepath'] = _secure_reference_csv(data['filepath'])
    data['comment_count'] = comment_count
    attachment_paths = [path for path in str(data.get('filepath') or '').split(',') if path]
    data['attachment_sizes'] = [get_stored_file_size(path) for path in attachment_paths]
    data['attachment_total_size'] = sum(data['attachment_sizes'])
    return jsonify(data)

@school_bp.route('/post/add', methods=['POST'])
def add_post():
    school_id = request.form.get('school_id')
    category = request.form.get('category')
    title = request.form.get('title')
    content = request.form.get('content')
    author = session.get('user_name')

    if is_shared_board(category) and not can_manage_shared_board():
        return "공유 게시판 글쓰기 권한이 없습니다.", 403

    conn = get_db()
    if not can_access_school(conn, school_id):
        conn.close()
        return "담당 학교 게시판에만 글을 등록할 수 있습니다.", 403
    
    files = request.files.getlist('file')
    files = [f for f in files if f and f.filename != '']
    if len(files) > POST_MAX_FILES:
        conn.close()
        return f"게시물 첨부파일은 최대 {POST_MAX_FILES}개까지 등록할 수 있습니다.", 400
    if sum(get_uploaded_file_size(file) for file in files) > POST_MAX_TOTAL_SIZE:
        conn.close()
        return "게시물 첨부파일의 총용량은 최대 15MB까지 등록할 수 있습니다.", 400
    filename_str, filepath_str = save_uploaded_files(files)
        
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO school_posts (school_id, category, title, content, author, filename, filepath)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (school_id, category, title, content, author, filename_str, filepath_str))
        new_post_id = cursor.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        _delete_references(filepath_str)
        raise
    finally:
        conn.close()
    
    return redirect_to_school(
        school_id,
        category=category,
        post_id=new_post_id
    )

@school_bp.route('/post/edit/<int:post_id>', methods=['POST'])
def edit_post(post_id):
    category = request.form.get('category')
    title = request.form.get('title')
    content = request.form.get('content')
    
    conn = get_db()
    post = conn.execute(
        """
        SELECT school_id, author, filename, filepath, category
        FROM school_posts
        WHERE id=?
        """,
        (post_id,)
    ).fetchone()

    if not post:
        conn.close()
        return "게시물을 찾을 수 없습니다.", 404

    school_id = post['school_id']
    if not can_access_school(conn, school_id):
        conn.close()
        return "담당 학교 게시글만 수정할 수 있습니다.", 403
    
    if is_shared_board(post['category']):
        has_edit_permission = can_manage_shared_board()
    else:
        has_edit_permission = (
            session.get('user_name') == post['author']
            or can_manage_schools()
        )

    if not has_edit_permission:
        conn.close()
        return "권한이 없습니다.", 403

    old_filenames_str = post['filename']
    old_filenames = old_filenames_str.split(',') if old_filenames_str else []
    old_filepaths_str = post['filepath']
    old_filepaths = old_filepaths_str.split(',') if old_filepaths_str else []
    requested_existing = request.form.getlist('existing_filenames')
    requested_existing_paths = request.form.getlist('existing_filepaths')
    old_pairs = list(zip(old_filenames, old_filepaths))
    requested_pairs = list(zip(requested_existing, requested_existing_paths))
    existing_pairs = []
    for pair in requested_pairs:
        if pair in old_pairs and pair not in existing_pairs:
            existing_pairs.append(pair)
    existing_filenames = [pair[0] for pair in existing_pairs]
    existing_filepaths = [pair[1] for pair in existing_pairs]

    files = [f for f in request.files.getlist('file') if f and f.filename != '']
    if len(existing_filenames) + len(files) > POST_MAX_FILES:
        conn.close()
        return f"게시물 첨부파일은 최대 {POST_MAX_FILES}개까지 등록할 수 있습니다.", 400

    existing_total_size = sum(get_stored_file_size(path) for path in existing_filepaths)
    uploaded_total_size = sum(get_uploaded_file_size(file) for file in files)
    if existing_total_size + uploaded_total_size > POST_MAX_TOTAL_SIZE:
        conn.close()
        return "게시물 첨부파일의 총용량은 최대 15MB까지 등록할 수 있습니다.", 400

    removed_references = [
        old_pair[1] or old_pair[0]
        for old_pair in old_pairs
        if old_pair not in existing_pairs
    ]

    new_filenames = existing_filenames.copy()
    new_filepaths = existing_filepaths.copy()
        
    added_names, added_paths = save_uploaded_files(files)
    if added_names:
        new_filenames.extend(added_names.split(','))
        new_filepaths.extend(added_paths.split(','))
            
    filename_str = ",".join(new_filenames) if new_filenames else None
    filepath_str = ",".join(new_filepaths) if new_filepaths else None
    
    try:
        conn.execute('UPDATE school_posts SET title=?, content=?, filename=?, filepath=? WHERE id=?',
                     (title, content, filename_str, filepath_str, post_id))
        conn.commit()
    except Exception:
        conn.rollback()
        _delete_references(added_paths)
        raise
    finally:
        conn.close()
    for reference in removed_references:
        delete_file(_stored_path_from_reference(reference))
    
    return redirect_to_school(
        school_id,
        category=category,
        post_id=post_id
    )

@school_bp.route('/post/<int:post_id>/comments')
def get_post_comments(post_id):
    conn = get_db()
    init_school_comment_table(conn)
    post = conn.execute(
        "SELECT school_id, category FROM school_posts WHERE id = ?",
        (post_id,)
    ).fetchone()
    if not post:
        conn.close()
        return jsonify({'message': '게시글을 찾을 수 없습니다.'}), 404
    if not can_access_post(conn, post['school_id'], post['category']):
        conn.close()
        return jsonify({'message': '담당 학교 댓글만 볼 수 있습니다.'}), 403

    rows = conn.execute("""
        SELECT id, post_id, parent_id, content, author, filename, filepath, created_at, updated_at
        FROM school_post_comments
        WHERE post_id = ?
        ORDER BY COALESCE(parent_id, id) ASC, parent_id IS NOT NULL ASC, created_at ASC
    """, (post_id,)).fetchall()
    conn.close()
    comments = []
    for row in rows:
        item = dict(row)
        if item.get('filepath'):
            item['filepath'] = _secure_reference_csv(item['filepath'])
        comments.append(item)
    return jsonify({'comments': comments})

@school_bp.route('/post/<int:post_id>/comments/add', methods=['POST'])
def add_post_comment(post_id):
    if request.is_json:
        data = request.get_json(silent=True) or {}
        files = []
    else:
        data = request.form
        files = request.files.getlist('file')

    content = (data.get('content') or '').strip()
    parent_id = data.get('parent_id') or None
    author = session.get('user_name') or '익명'

    if not content:
        return jsonify({'ok': False, 'message': '댓글 내용을 입력하세요.'}), 400

    conn = get_db()
    init_school_comment_table(conn)

    post = conn.execute(
        "SELECT id, school_id, category FROM school_posts WHERE id=?",
        (post_id,)
    ).fetchone()
    if not post:
        conn.close()
        return jsonify({'ok': False, 'message': '게시글을 찾을 수 없습니다.'}), 404
    if not can_access_post(conn, post['school_id'], post['category']):
        conn.close()
        return jsonify({
            'ok': False,
            'message': '담당 학교 게시글에만 댓글을 등록할 수 있습니다.'
        }), 403

    filename_str, filepath_str = save_uploaded_files(files)

    try:
        conn.execute("""
            INSERT INTO school_post_comments (post_id, parent_id, content, author, filename, filepath)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (post_id, parent_id, content, author, filename_str, filepath_str))
        conn.commit()
    except Exception:
        conn.rollback()
        _delete_references(filepath_str)
        raise
    finally:
        conn.close()
    return jsonify({'ok': True})

@school_bp.route('/comments/<int:comment_id>/edit', methods=['POST'])
def edit_post_comment(comment_id):
    data = request.get_json(silent=True) or request.form
    content = (data.get('content') or '').strip()
    if not content:
        return jsonify({'ok': False, 'message': '댓글 내용을 입력하세요.'}), 400

    conn = get_db()
    init_school_comment_table(conn)
    comment = conn.execute(
        """
        SELECT c.id, c.author, c.filepath, p.school_id, p.category
        FROM school_post_comments c
        JOIN school_posts p ON p.id = c.post_id
        WHERE c.id=?
        """,
        (comment_id,)
    ).fetchone()

    if not comment:
        conn.close()
        return jsonify({'ok': False, 'message': '댓글을 찾을 수 없습니다.'}), 404

    if not can_access_post(
        conn,
        comment['school_id'],
        comment['category']
    ):
        conn.close()
        return jsonify({'ok': False, 'message': '담당 학교 댓글만 수정할 수 있습니다.'}), 403

    if session.get('user_name') != comment['author'] and not can_manage_schools():
        conn.close()
        return jsonify({'ok': False, 'message': '권한이 없습니다.'}), 403

    conn.execute(
        "UPDATE school_post_comments SET content=?, updated_at=datetime('now', 'localtime') WHERE id=?",
        (content, comment_id)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@school_bp.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_post_comment(comment_id):
    conn = get_db()
    init_school_comment_table(conn)
    comment = conn.execute(
        """
        SELECT c.author, p.school_id, p.category
        FROM school_post_comments c
        JOIN school_posts p ON p.id = c.post_id
        WHERE c.id=?
        """,
        (comment_id,)
    ).fetchone()

    if not comment:
        conn.close()
        return jsonify({'ok': False, 'message': '댓글을 찾을 수 없습니다.'}), 404

    if not can_access_post(
        conn,
        comment['school_id'],
        comment['category']
    ):
        conn.close()
        return jsonify({'ok': False, 'message': '담당 학교 댓글만 삭제할 수 있습니다.'}), 403

    if session.get('user_name') != comment['author'] and not can_manage_schools():
        conn.close()
        return jsonify({'ok': False, 'message': '권한이 없습니다.'}), 403

    file_rows = conn.execute(
        "SELECT filepath FROM school_post_comments WHERE id=? OR parent_id=?",
        (comment_id, comment_id),
    ).fetchall()
    conn.execute("DELETE FROM school_post_comments WHERE id=? OR parent_id=?", (comment_id, comment_id))
    conn.commit()
    conn.close()
    for row in file_rows:
        _delete_references(row['filepath'])
    return jsonify({'ok': True})

@school_bp.route('/post/delete/<int:post_id>', methods=['POST'])
def delete_post(post_id):
    category = request.form.get('category')
    conn = get_db()
    post = conn.execute(
        """
        SELECT school_id, author, filepath, category
        FROM school_posts
        WHERE id=?
        """,
        (post_id,)
    ).fetchone()
    if not post:
        conn.close()
        return "게시물을 찾을 수 없습니다.", 404

    school_id = post['school_id']
    if not can_access_school(conn, school_id):
        conn.close()
        return "담당 학교 게시글만 삭제할 수 있습니다.", 403

    if is_shared_board(post['category']):
        has_delete_permission = can_manage_shared_board()
    else:
        has_delete_permission = (
            session.get('user_name') == post['author']
            or can_manage_schools()
        )

    if not has_delete_permission:
        conn.close()
        return "권한이 없습니다.", 403

    comment_files = conn.execute(
        'SELECT filepath FROM school_post_comments WHERE post_id=?', (post_id,)
    ).fetchall()
    conn.execute('DELETE FROM school_post_comments WHERE post_id=?', (post_id,))
    conn.execute('DELETE FROM school_posts WHERE id=?', (post_id,))
    conn.commit()
    conn.close()
    _delete_references(post['filepath'])
    for row in comment_files:
        _delete_references(row['filepath'])
    return redirect_to_school(school_id, category=category)

@school_bp.route('/post/delete_multi', methods=['POST'])
def delete_multi():
    school_id = request.form.get('school_id')
    category = request.form.get('category')
    post_ids = request.form.getlist('post_ids')
    conn = get_db()
    if not can_access_school(conn, school_id):
        conn.close()
        return "담당 학교 게시글만 삭제할 수 있습니다.", 403

    posts_to_delete = []
    for pid in post_ids:
        post = conn.execute(
            """
            SELECT id, school_id, author, category, filepath
            FROM school_posts
            WHERE id=?
            """,
            (pid,)
        ).fetchone()
        if post and (
            is_shared_board(post['category'])
            or str(post['school_id']) == str(school_id)
        ):
            posts_to_delete.append(post)

    if any(is_shared_board(post['category']) for post in posts_to_delete) and not can_manage_shared_board():
        conn.close()
        return "공유 게시판 삭제 권한이 없습니다.", 403

    deleted_references = []
    for post in posts_to_delete:
        if (
            is_shared_board(post['category'])
            or session.get('user_name') == post['author']
            or can_manage_schools()
        ):
            pid = post['id']
            comment_files = conn.execute(
                'SELECT filepath FROM school_post_comments WHERE post_id=?', (pid,)
            ).fetchall()
            conn.execute("DELETE FROM school_post_comments WHERE post_id=?", (pid,))
            conn.execute("DELETE FROM school_posts WHERE id=?", (pid,))
            deleted_references.append(post['filepath'])
            for row in comment_files:
                deleted_references.append(row['filepath'])
    conn.commit()
    conn.close()
    for references in deleted_references:
        _delete_references(references)
    return redirect_to_school(school_id, category=category)

# -----------------------------------------------------------
# [추가할 부분] - school_bp.py 파일에 일정 수정/삭제 라우터 추가
# -----------------------------------------------------------
@school_bp.route('/edit_task/<int:task_id>', methods=['POST'])
def edit_task(task_id):
    data = request.get_json()
    user_name = session.get('user_name')
    user_level = int(session.get('user_level', 99))
    
    conn = get_db()
    task = conn.execute(
        "SELECT school_id, owner FROM school_tasks WHERE id=?",
        (task_id,)
    ).fetchone()
    
    # 본인이 등록한 일정이거나, 센터장보다 높은 권한(7 이하)인 경우에만 수정 가능
    if (
        not task
        or not can_access_school(conn, task['school_id'])
        or (task['owner'] != user_name and user_level > 7)
    ):
        conn.close()
        return jsonify({'ok': False, 'message': '권한이 없습니다.'}), 403
        
    conn.execute('''
        UPDATE school_tasks
        SET title=?, start_date=?, start_time=?, end_date=?, end_time=?, note=?
        WHERE id=?
    ''', (data.get('title'), data.get('start_date'), data.get('start_time'), 
          data.get('end_date'), data.get('end_time'), data.get('note'), task_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@school_bp.route('/delete_task/<int:task_id>', methods=['POST'])
def delete_task(task_id):
    user_name = session.get('user_name')
    user_level = int(session.get('user_level', 99))
    
    conn = get_db()
    task = conn.execute(
        "SELECT school_id, owner FROM school_tasks WHERE id=?",
        (task_id,)
    ).fetchone()
    
    if (
        not task
        or not can_access_school(conn, task['school_id'])
        or (task['owner'] != user_name and user_level > 7)
    ):
        conn.close()
        return jsonify({'ok': False, 'message': '권한이 없습니다.'}), 403
        
    conn.execute('DELETE FROM school_tasks WHERE id=?', (task_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# 캘린더 기능 추가--------------------------------

@school_bp.route('/calendar')
def full_calendar():
    conn = get_db()
    
    # 세션에서 권한 및 사용자 정보 가져오기
    # 값이 없을 경우를 대비해 기본 레벨을 99(가장 낮은 권한)로 설정
    user_level = int(session.get('user_level', 99)) 
    user_name = session.get('user_name')
    
    query = """
        SELECT t.*, s.school_name 
        FROM school_tasks t
        JOIN schools s ON t.school_id = s.id
    """
    params = []
    
    # 센터장(레벨 8)이거나 그 이하 권한(숫자가 8 이상)인 경우 본인 일정만 조회
    # 센터장보다 높은 권한(숫자가 8 미만)이거나 admin인 경우 모든 일정 조회
    if user_level >= 8 and user_name != 'admin':
        query += " WHERE t.owner = ?"
        params.append(user_name)
        
    query += " ORDER BY t.start_date ASC, t.start_time ASC"
    
    tasks_db = conn.execute(query, params).fetchall()
    conn.close()
    
    all_tasks = [dict(t) for t in tasks_db]
    return render_template('school_calendar.html', all_tasks=all_tasks)
