from flask import Blueprint, render_template, request, session, redirect, url_for, jsonify, current_app
from routes.database import get_db
import os
import math
from werkzeug.utils import secure_filename
from datetime import datetime

school_bp = Blueprint('school', __name__)

def get_upload_dir():
    """안전한 파일 업로드 경로 반환 및 폴더 생성"""
    upload_dir = os.path.join(current_app.root_path, 'static', 'school_uploads')
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def init_school_comment_table(conn):
    """학교 게시판 댓글/대댓글 테이블이 없으면 자동 생성하고, 기존 DB에는 파일 컬럼을 자동 추가"""
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

    # 이미 테이블이 만들어진 운영 DB에서는 ALTER TABLE로 첨부파일 컬럼을 보강
    columns = [row[1] for row in conn.execute("PRAGMA table_info(school_post_comments)").fetchall()]
    if 'filename' not in columns:
        conn.execute("ALTER TABLE school_post_comments ADD COLUMN filename TEXT")
    if 'filepath' not in columns:
        conn.execute("ALTER TABLE school_post_comments ADD COLUMN filepath TEXT")
    conn.commit()


def save_uploaded_files(files):
    """여러 첨부파일 저장 후 DB에 저장할 filename/filepath 문자열 반환"""
    filenames, filepaths = [], []
    for file in files:
        if file and file.filename:
            # secure_filename은 한글을 날려버리므로, 단순 경로 이탈 방지 처리만 수행하여 한글 파일명 유지
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
    """학교 목록 페이지"""
    conn = get_db()
    init_school_comment_table(conn)
    # 센터장 이름과 사진/아이콘 정보를 함께 가져옴
    schools = conn.execute('''
        SELECT s.*, u.name as director_name, u.profile_path as director_photo, u.profile_icon as director_icon
        FROM schools s
        LEFT JOIN users u ON s.center_director_id = u.emp_no
        ORDER BY s.year DESC, s.school_name ASC
    ''').fetchall()
    conn.close()
    return render_template('school_bp.html', schools=schools, view_type='list')

@school_bp.route('/register', methods=['POST'])
def register_school():
    """학교 신규 등록"""
    data = request.form
    conn = get_db()
    init_school_comment_table(conn)
    try:
        conn.execute('''
            INSERT INTO schools (year, school_name, office_phone, office_location, neulbom_assistant, neulbom_manager, center_director_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (data['year'], data['school_name'], data['office_phone'], data['office_location'], 
              data['neulbom_assistant'], data['neulbom_manager'], data['center_director_id']))
        conn.commit()
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()
    return redirect(url_for('school.school_list'))

@school_bp.route('/<int:school_id>')
def school_detail(school_id):
    """10개 메뉴가 적용된 개별 학교 업무 공간"""
    # 기본 카테고리를 'notice'(수강안내문)로 설정
    category = request.args.get('category', 'notice')
    page = request.args.get('page', 1, type=int)
    search_query = request.args.get('search', '').strip()

    # 통합메뉴 카테고리 10개 정의
    school_categories = [
        {'id': 'notice', 'name': '수강안내문', 'icon': 'fa-circle-info'},
        {'id': 'weekly_report', 'name': '주간업무보고', 'icon': 'fa-list-check'},
        {'id': 'open_class', 'name': '공개수업', 'icon': 'fa-chalkboard-user'},
        {'id': 'expense', 'name': '지출결의서', 'icon': 'fa-file-invoice-dollar'},
        {'id': 'item_request', 'name': '물품요청', 'icon': 'fa-box'},
        {'id': 'work_schedule', 'name': '근무표', 'icon': 'fa-calendar-days'},
        {'id': 'billing', 'name': '청구관련', 'icon': 'fa-receipt'},
        {'id': 'survey', 'name': '만족도조사', 'icon': 'fa-chart-simple'},
        {'id': 'reference', 'name': '자료실', 'icon': 'fa-file-zipper'},
        {'id': 'community', 'name': '커뮤니티', 'icon': 'fa-users'}
    ]

    cat_id_to_name = {cat['id']: cat['name'] for cat in school_categories}
    cat_name_to_id = {cat['name']: cat['id'] for cat in school_categories}

    if category in cat_name_to_id:
         search_category = cat_name_to_id[category]
         current_category_name = category
    else:
         search_category = category
         current_category_name = cat_id_to_name.get(category, category)
    
    per_page = 10 
    offset = (page - 1) * per_page
    
    conn = get_db()
    init_school_comment_table(conn)
    
    # 1. 학교 및 센터장 정보 
    school = conn.execute('''
        SELECT s.*, u.name as director_name, u.profile_path as director_photo, 
               u.position as director_pos, u.profile_icon as director_icon
        FROM schools s
        LEFT JOIN users u ON s.center_director_id = u.emp_no
        WHERE s.id = ?
    ''', (school_id,)).fetchone()
    
    if not school:
        conn.close()
        return "학교를 찾을 수 없습니다.", 404

    # 2. 카테고리별 게시글 목록
    query_params = [school_id, search_category, current_category_name]
    count_query = "SELECT COUNT(*) FROM school_posts WHERE school_id = ? AND (category = ? OR category = ?)"
    data_query = "SELECT * FROM school_posts WHERE school_id = ? AND (category = ? OR category = ?)"
    
    if search_query:
        search_filter = " AND (title LIKE ? OR author LIKE ?)"
        count_query += search_filter
        data_query += search_filter
        query_params.extend([f"%{search_query}%", f"%{search_query}%"])
        
    data_query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    
    total_posts = conn.execute(count_query, query_params).fetchone()[0]
    total_pages = math.ceil(total_posts / per_page)
    
    query_params.extend([per_page, offset])
    posts = conn.execute(data_query, query_params).fetchall()
    
    # 3. 사이드바 조직도 데이터 (admin 제외, 레벨 낮은순, 이름 가나다순)
    current_user_name = session.get('user_name')
    users_list = conn.execute('''
        SELECT name, profile_icon, profile_path, department, position, level
        FROM users 
        WHERE status = '승인' AND emp_no != 'admin'
        ORDER BY level ASC, name ASC
    ''').fetchall()

    # 4. 쪽지 데이터 조회 (admin 제외)
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

    # 사용자 아이콘 매핑 (admin 제외)
    user_rows = conn.execute("SELECT name, profile_icon FROM users WHERE emp_no != 'admin'").fetchall()
    user_icons = {row['name']: row['profile_icon'] or '👤' for row in user_rows}
    
    conn.close()
    
    pagination = {
        'page': page, 'total_pages': total_pages, 'start_page': (math.ceil(page / 10) - 1) * 10 + 1, 
        'end_page': min((math.ceil(page / 10) - 1) * 10 + 10, total_pages), 
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
                           pagination=pagination, view_type='detail')

@school_bp.route('/employee_search')
def employee_search():
    """조직도 검색 시 admin 제외 및 레벨순 정렬"""
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
    return jsonify(data)

@school_bp.route('/post/add', methods=['POST'])
def add_post():
    school_id = request.form.get('school_id')
    category = request.form.get('category')
    title = request.form.get('title')
    content = request.form.get('content')
    author = session.get('user_name')
    
    files = request.files.getlist('file')
    files = [f for f in files if f and f.filename != '']
    if len(files) > 5:
        return "게시물 첨부파일은 최대 5개까지 등록할 수 있습니다.", 400
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
    post = conn.execute("SELECT author, filename, filepath FROM school_posts WHERE id=?", (post_id,)).fetchone()
    
    user_level = session.get('user_level', 1)
    if session.get('user_name') != post['author'] and user_level < 3:
        conn.close()
        return "권한이 없습니다.", 403

    existing_filenames = request.form.getlist('existing_filenames')
    existing_filepaths = request.form.getlist('existing_filepaths')

    old_filenames_str = post['filename']
    old_filenames = old_filenames_str.split(',') if old_filenames_str else []
    for old_file in old_filenames:
        if old_file and old_file not in existing_filenames:
            file_path = os.path.join(get_upload_dir(), old_file)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception as e:
                print(f"File delete error: {e}")

    new_filenames = existing_filenames.copy()
    new_filepaths = existing_filepaths.copy()

    files = [f for f in request.files.getlist('file') if f and f.filename != '']
    if len(new_filenames) + len(files) > 5:
        conn.close()
        return "게시물 첨부파일은 최대 5개까지 등록할 수 있습니다.", 400
        
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
    post = conn.execute("SELECT author, filepath FROM school_posts WHERE id=?", (post_id,)).fetchone()
    if session.get('user_name') == post['author'] or session.get('user_level', 1) >= 3:
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
    for pid in post_ids:
        post = conn.execute("SELECT author FROM school_posts WHERE id=?", (pid,)).fetchone()
        if session.get('user_name') == post['author'] or session.get('user_level', 1) >= 3:
            conn.execute("DELETE FROM school_post_comments WHERE post_id=?", (pid,))
            conn.execute("DELETE FROM school_posts WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return redirect(url_for('school.school_detail', school_id=school_id, category=category))