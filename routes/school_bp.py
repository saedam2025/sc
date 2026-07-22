from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify, current_app, send_from_directory
from routes.database import get_db
import os
import math
import json
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta

school_bp = Blueprint('school', __name__)

HEADQUARTERS_BOARD_CATEGORIES = {'community', '본부공지사항'}
POST_MAX_FILES = 10
POST_MAX_TOTAL_SIZE = 15 * 1024 * 1024

def is_headquarters_board(category):
    return str(category or '').strip() in HEADQUARTERS_BOARD_CATEGORIES

def get_session_user_level(default=99):
    try:
        return int(session.get('user_level', default))
    except (TypeError, ValueError):
        return default

def can_manage_headquarters_board():
    return bool(session.get('user_name')) and 1 <= get_session_user_level() <= 5

def get_upload_dir():
    upload_dir = os.path.join(current_app.root_path, 'static', 'school_uploads')
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir

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

def get_stored_file_size(filename):
    if not filename:
        return 0
    file_path = os.path.join(get_upload_dir(), os.path.basename(filename))
    try:
        return os.path.getsize(file_path) if os.path.isfile(file_path) else 0
    except OSError:
        return 0

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
    for file in files:
        if file and file.filename:
            original_filename = file.filename.replace('/', '_').replace('\\', '_')
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
            safe_filename = f"{timestamp}_{original_filename}"
            save_path = os.path.join(get_upload_dir(), safe_filename)
            file.save(save_path)
            filenames.append(safe_filename)
            filepaths.append(f"/static/school_uploads/{safe_filename}")
    return (",".join(filenames) if filenames else None, ",".join(filepaths) if filepaths else None)

@school_bp.route('/')
def school_list():
    conn = get_db()
    init_school_comment_table(conn)
    
    # 💡 세션에서 사용자 정보(레벨, 사번) 가져오기
    user_level = session.get('user_level', 99)
    emp_no = session.get('emp_no')

    # 💡 [핵심 추가] 레벨 8(센터장)인 경우, 목록을 보여주지 않고 담당 학교 메인으로 즉시 강제 이동
    if user_level == 8:
        # 해당 사번(emp_no)이 센터장으로 지정된 활성 상태의 최신 학교를 찾습니다.
        my_school = conn.execute(
            "SELECT id FROM schools WHERE center_director_id = ? ORDER BY is_active DESC, year DESC LIMIT 1", 
            (emp_no,)
        ).fetchone()
        
        if my_school:
            conn.close()
            # 담당 학교의 고유 ID를 이용해 첨부하신 이미지의 상세 대시보드 화면으로 바로 쏴줍니다.
            return redirect(url_for('school.school_detail', school_id=my_school['id']))
        else:
            conn.close()
            return "담당으로 지정된 학교가 없습니다. 본사 관리자에게 문의해주세요.", 403

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
def register_school():
    data = request.form
    conn = get_db()
    init_school_comment_table(conn)
    try:
        conn.execute('''
            INSERT INTO schools (
                year, school_name, contract_subject, office_phone, office_location,
                school_address, school_phone, school_email,
                neulbom_assistant, neulbom_manager, center_director_id, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (
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
def toggle_schools():
    school_ids = request.form.getlist('school_ids')
    if school_ids:
        conn = get_db()
        for sid in school_ids:
            conn.execute('UPDATE schools SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?', (sid,))
        conn.commit()
        conn.close()
    return redirect(url_for('school.school_list'))

@school_bp.route('/<int:school_id>')
def school_detail(school_id):
    # 기본 접속 메뉴를 'notice'에서 'community'(본부공지사항)로 변경
    category = request.args.get('category', 'community')
    page = request.args.get('page', 1, type=int)
    search_query = request.args.get('search', '').strip()

# 커뮤니티를 본부공지사항으로 변경하고 맨 앞으로 이동
    school_categories = [
        {'id': 'community', 'name': '본부공지사항', 'icon': 'fa-bullhorn'},
        {'id': 'notice', 'name': '수강안내문', 'icon': 'fa-circle-info'},
        {'id': 'weekly_report', 'name': '주간업무보고', 'icon': 'fa-list-check'},
        {'id': 'open_class', 'name': '공개수업', 'icon': 'fa-chalkboard-user'},
        {'id': 'expense', 'name': '지출결의서', 'icon': 'fa-file-invoice-dollar'},
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
        not is_headquarters_board(search_category)
        or can_manage_headquarters_board()
    )
    
    per_page = 7
    offset = (page - 1) * per_page
    
    conn = get_db()
    init_school_comment_table(conn)
    
    school = conn.execute('''
        SELECT s.*, u.name as director_name, u.profile_path as director_photo, 
               u.position as director_pos, u.phone as director_phone, u.email as director_email, u.profile_icon as director_icon
        FROM schools s
        LEFT JOIN users u ON s.center_director_id = u.emp_no
        WHERE s.id = ?
    ''', (school_id,)).fetchone()
    
    school_dict = dict(school) if school else None
    
    if not school_dict or (not school_dict.get('is_active', 1) and session.get('user_level', 1) < 3 and session.get('user_name') != 'admin'):
        conn.close()
        return "비활성화 처리되어 접근할 수 없는 학교입니다.", 403

    if search_category == 'community':
        query_params = [search_category, current_category_name]
        count_query = "SELECT COUNT(*) FROM school_posts WHERE (category = ? OR category = ?)"
        data_query = "SELECT * FROM school_posts WHERE (category = ? OR category = ?)"
        
        if search_query:
            search_filter = " AND (title LIKE ? OR author LIKE ? OR content LIKE ?)"
            count_query += search_filter
            data_query += search_filter
            query_params.extend([f"%{search_query}%", f"%{search_query}%", f"%{search_query}%"])
            
        data_query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    else:
        query_params = [school_id, search_category, current_category_name]
        count_query = "SELECT COUNT(*) FROM school_posts WHERE school_id = ? AND (category = ? OR category = ?)"
        data_query = "SELECT * FROM school_posts WHERE school_id = ? AND (category = ? OR category = ?)"
        
        if search_query:
            search_filter = " AND (title LIKE ? OR author LIKE ? OR content LIKE ?)"
            count_query += search_filter
            data_query += search_filter
            query_params.extend([f"%{search_query}%", f"%{search_query}%", f"%{search_query}%"])
            
        data_query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    
    total_posts = conn.execute(count_query, query_params).fetchone()[0]
    total_pages = math.ceil(total_posts / per_page)
    
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

    # 메인 화면과 동일한 사내갤러리(gall2) 최신 게시물 미리보기
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
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT 5
        ''').fetchall()
        gallery_preview_items = [dict(row) for row in gallery_rows]
    except Exception as e:
        print(f"학교 업무공간 사내갤러리 미리보기 로드 에러: {e}")
            
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

    normalized_path = str(link['filepath']).replace('\\', os.sep).replace('/', os.sep)
    file_path = os.path.abspath(normalized_path)
    if not os.path.isfile(file_path):
        return "파일을 찾을 수 없습니다.", 404

    return send_from_directory(
        os.path.dirname(file_path),
        os.path.basename(file_path),
        as_attachment=False,
        download_name=link['filename'] or os.path.basename(file_path)
    )

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
    return jsonify([dict(u) for u in users])

@school_bp.route('/post/api/<int:post_id>')
def get_post_api(post_id):
    conn = get_db()
    init_school_comment_table(conn)
    post = conn.execute("SELECT * FROM school_posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return jsonify({}), 404

    comment_count = conn.execute(
        "SELECT COUNT(*) FROM school_post_comments WHERE post_id=?",
        (post_id,)
    ).fetchone()[0]
    conn.close()

    data = dict(post)
    data['comment_count'] = comment_count
    attachment_names = [name for name in str(data.get('filename') or '').split(',') if name]
    data['attachment_sizes'] = [get_stored_file_size(name) for name in attachment_names]
    data['attachment_total_size'] = sum(data['attachment_sizes'])
    return jsonify(data)

@school_bp.route('/post/add', methods=['POST'])
def add_post():
    school_id = request.form.get('school_id')
    category = request.form.get('category')
    title = request.form.get('title')
    content = request.form.get('content')
    author = session.get('user_name')

    if is_headquarters_board(category) and not can_manage_headquarters_board():
        return "본부공지사항 글쓰기 권한이 없습니다.", 403
    
    files = request.files.getlist('file')
    files = [f for f in files if f and f.filename != '']
    if len(files) > POST_MAX_FILES:
        return f"게시물 첨부파일은 최대 {POST_MAX_FILES}개까지 등록할 수 있습니다.", 400
    if sum(get_uploaded_file_size(file) for file in files) > POST_MAX_TOTAL_SIZE:
        return "게시물 첨부파일의 총용량은 최대 15MB까지 등록할 수 있습니다.", 400
    filenames, filepaths = [], []
    
    for file in files:
        if file and file.filename != '':
            original_filename = file.filename.replace('/', '_').replace('\\', '_')
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            safe_filename = f"{timestamp}_{original_filename}"
            save_path = os.path.join(get_upload_dir(), safe_filename)
            file.save(save_path)
            filenames.append(safe_filename) 
            filepaths.append(f"/static/school_uploads/{safe_filename}")
            
    filename_str = ",".join(filenames) if filenames else None
    filepath_str = ",".join(filepaths) if filepaths else None
        
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO school_posts (school_id, category, title, content, author, filename, filepath)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (school_id, category, title, content, author, filename_str, filepath_str))
    
    new_post_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return redirect(url_for('school.school_detail', school_id=school_id, category=category, post_id=new_post_id))

@school_bp.route('/post/edit/<int:post_id>', methods=['POST'])
def edit_post(post_id):
    school_id = request.form.get('school_id')
    category = request.form.get('category')
    title = request.form.get('title')
    content = request.form.get('content')
    
    conn = get_db()
    post = conn.execute("SELECT author, filename, filepath, category FROM school_posts WHERE id=?", (post_id,)).fetchone()

    if not post:
        conn.close()
        return "게시물을 찾을 수 없습니다.", 404
    
    user_level = get_session_user_level(1)
    if is_headquarters_board(post['category']):
        has_edit_permission = can_manage_headquarters_board()
    else:
        has_edit_permission = session.get('user_name') == post['author'] or user_level >= 3

    if not has_edit_permission:
        conn.close()
        return "권한이 없습니다.", 403

    old_filenames_str = post['filename']
    old_filenames = old_filenames_str.split(',') if old_filenames_str else []
    old_filepaths_str = post['filepath']
    old_filepaths = old_filepaths_str.split(',') if old_filepaths_str else []
    old_filepath_by_name = {
        filename: (old_filepaths[index] if index < len(old_filepaths) else '')
        for index, filename in enumerate(old_filenames)
        if filename
    }

    requested_existing = request.form.getlist('existing_filenames')
    existing_filenames = []
    for filename in requested_existing:
        if filename in old_filepath_by_name and filename not in existing_filenames:
            existing_filenames.append(filename)
    existing_filepaths = [old_filepath_by_name[filename] for filename in existing_filenames]

    files = [f for f in request.files.getlist('file') if f and f.filename != '']
    if len(existing_filenames) + len(files) > POST_MAX_FILES:
        conn.close()
        return f"게시물 첨부파일은 최대 {POST_MAX_FILES}개까지 등록할 수 있습니다.", 400

    existing_total_size = sum(get_stored_file_size(filename) for filename in existing_filenames)
    uploaded_total_size = sum(get_uploaded_file_size(file) for file in files)
    if existing_total_size + uploaded_total_size > POST_MAX_TOTAL_SIZE:
        conn.close()
        return "게시물 첨부파일의 총용량은 최대 15MB까지 등록할 수 있습니다.", 400

    for old_file in old_filenames:
        if old_file and old_file not in existing_filenames:
            file_path = os.path.join(get_upload_dir(), old_file)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception as e:
                pass

    new_filenames = existing_filenames.copy()
    new_filepaths = existing_filepaths.copy()
        
    for file in files:
        if file and file.filename != '':
            original_filename = file.filename.replace('/', '_').replace('\\', '_')
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            safe_filename = f"{timestamp}_{original_filename}"
            save_path = os.path.join(get_upload_dir(), safe_filename)
            file.save(save_path)
            new_filenames.append(safe_filename)
            new_filepaths.append(f"/static/school_uploads/{safe_filename}")
            
    filename_str = ",".join(new_filenames) if new_filenames else None
    filepath_str = ",".join(new_filepaths) if new_filepaths else None
    
    conn.execute('UPDATE school_posts SET title=?, content=?, filename=?, filepath=? WHERE id=?', 
                 (title, content, filename_str, filepath_str, post_id))
    conn.commit()
    conn.close()
    
    return redirect(url_for('school.school_detail', school_id=school_id, category=category, post_id=post_id))

@school_bp.route('/post/<int:post_id>/comments')
def get_post_comments(post_id):
    conn = get_db()
    init_school_comment_table(conn)
    rows = conn.execute("""
        SELECT id, post_id, parent_id, content, author, filename, filepath, created_at, updated_at
        FROM school_post_comments
        WHERE post_id = ?
        ORDER BY COALESCE(parent_id, id) ASC, parent_id IS NOT NULL ASC, created_at ASC
    """, (post_id,)).fetchall()
    conn.close()
    comments = [dict(r) for r in rows]
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

    post = conn.execute("SELECT id FROM school_posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return jsonify({'ok': False, 'message': '게시글을 찾을 수 없습니다.'}), 404

    filename_str, filepath_str = save_uploaded_files(files)

    conn.execute("""
        INSERT INTO school_post_comments (post_id, parent_id, content, author, filename, filepath)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (post_id, parent_id, content, author, filename_str, filepath_str))
    conn.commit()
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
        "SELECT author FROM school_post_comments WHERE id=?",
        (comment_id,)
    ).fetchone()

    if not comment:
        conn.close()
        return jsonify({'ok': False, 'message': '댓글을 찾을 수 없습니다.'}), 404

    if session.get('user_name') != comment['author'] and session.get('user_level', 1) < 3:
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
        "SELECT author FROM school_post_comments WHERE id=?",
        (comment_id,)
    ).fetchone()

    if not comment:
        conn.close()
        return jsonify({'ok': False, 'message': '댓글을 찾을 수 없습니다.'}), 404

    if session.get('user_name') != comment['author'] and session.get('user_level', 1) < 3:
        conn.close()
        return jsonify({'ok': False, 'message': '권한이 없습니다.'}), 403

    conn.execute("DELETE FROM school_post_comments WHERE id=? OR parent_id=?", (comment_id, comment_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@school_bp.route('/post/delete/<int:post_id>', methods=['POST'])
def delete_post(post_id):
    school_id = request.form.get('school_id')
    category = request.form.get('category')
    conn = get_db()
    post = conn.execute("SELECT author, filepath, category FROM school_posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return "게시물을 찾을 수 없습니다.", 404

    if is_headquarters_board(post['category']):
        has_delete_permission = can_manage_headquarters_board()
    else:
        has_delete_permission = (
            session.get('user_name') == post['author']
            or get_session_user_level(1) >= 3
        )

    if not has_delete_permission:
        conn.close()
        return "권한이 없습니다.", 403

    conn.execute('DELETE FROM school_post_comments WHERE post_id=?', (post_id,))
    conn.execute('DELETE FROM school_posts WHERE id=?', (post_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('school.school_detail', school_id=school_id, category=category))

@school_bp.route('/post/delete_multi', methods=['POST'])
def delete_multi():
    school_id = request.form.get('school_id')
    category = request.form.get('category')
    post_ids = request.form.getlist('post_ids')
    conn = get_db()
    posts_to_delete = []
    for pid in post_ids:
        post = conn.execute("SELECT id, author, category FROM school_posts WHERE id=?", (pid,)).fetchone()
        if post:
            posts_to_delete.append(post)

    if any(is_headquarters_board(post['category']) for post in posts_to_delete) and not can_manage_headquarters_board():
        conn.close()
        return "본부공지사항 삭제 권한이 없습니다.", 403

    user_level = get_session_user_level(1)
    for post in posts_to_delete:
        if (
            is_headquarters_board(post['category'])
            or session.get('user_name') == post['author']
            or user_level >= 3
        ):
            pid = post['id']
            conn.execute("DELETE FROM school_post_comments WHERE post_id=?", (pid,))
            conn.execute("DELETE FROM school_posts WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return redirect(url_for('school.school_detail', school_id=school_id, category=category))

# -----------------------------------------------------------
# [추가할 부분] - school_bp.py 파일에 일정 수정/삭제 라우터 추가
# -----------------------------------------------------------
@school_bp.route('/edit_task/<int:task_id>', methods=['POST'])
def edit_task(task_id):
    data = request.get_json()
    user_name = session.get('user_name')
    user_level = int(session.get('user_level', 99))
    
    conn = get_db()
    task = conn.execute("SELECT owner FROM school_tasks WHERE id=?", (task_id,)).fetchone()
    
    # 본인이 등록한 일정이거나, 센터장보다 높은 권한(7 이하)인 경우에만 수정 가능
    if not task or (task['owner'] != user_name and user_level > 7):
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
    task = conn.execute("SELECT owner FROM school_tasks WHERE id=?", (task_id,)).fetchone()
    
    if not task or (task['owner'] != user_name and user_level > 7):
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
