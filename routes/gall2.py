from flask import Blueprint, abort, render_template, request, redirect, url_for, send_from_directory, Response, jsonify, session
from werkzeug.utils import secure_filename
from .database import get_db
from .security import admin_required, load_credential_secret
from .storage import GALL2_ROOT
from cryptography.fernet import Fernet
import base64
import hashlib
import os
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageOps, UnidentifiedImageError

gall2_bp = Blueprint('gall2', __name__)

_secret_digest = hashlib.sha256(load_credential_secret().encode('utf-8')).digest()
cipher = Fernet(base64.urlsafe_b64encode(_secret_digest))

# 저장 경로를 gall2 전용으로 변경
BASE_GALLERY_PATH = str(GALL2_ROOT)
UPLOAD_FOLDER = os.path.join(BASE_GALLERY_PATH, 'uploads')
THUMB_FOLDER = os.path.join(BASE_GALLERY_PATH, 'thumbnails')
GALLERY_IMAGE_MAX_SIZE = (1920, 1080)
GALLERY_IMAGE_QUALITY = 85
POSTS_PER_PAGE = 18
SCHOOL_GALLERY_SCOPE_ID = 0

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(THUMB_FOLDER, exist_ok=True)

def optimize_gallery_image(file, temp_path):
    """업로드 이미지를 웹용 크기와 용량으로 줄여 임시 JPG 파일로 저장합니다."""
    file.stream.seek(0)
    img = Image.open(file.stream)
    img = ImageOps.exif_transpose(img)
    img.thumbnail(GALLERY_IMAGE_MAX_SIZE, Image.Resampling.LANCZOS)

    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        alpha = img.getchannel("A") if img.mode == "RGBA" else img.getchannel("A")
        background.paste(img, mask=alpha)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    buffer = BytesIO()
    img.save(buffer, format="JPEG", optimize=True, quality=GALLERY_IMAGE_QUALITY)
    buffer.seek(0)
    with open(temp_path, "wb") as f:
        f.write(buffer.read())

def build_gallery_filename(original_filename):
    safe_name = secure_filename(original_filename)
    save_root = os.path.splitext(safe_name)[0] or "gallery_image"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{save_root}_{timestamp}.jpg"

def format_file_size(size):
    if size is None:
        return "0 KB"
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024

def get_gallery_file_size(filename):
    path = os.path.join(UPLOAD_FOLDER, filename)
    try:
        return os.path.getsize(path)
    except OSError:
        return 0

def ensure_gall2_schema():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gall2_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT,
            author TEXT,
            tab_id INTEGER NOT NULL DEFAULT 1,
            upload_token TEXT UNIQUE,
            school_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    post_columns = {
        row['name'] if hasattr(row, 'keys') else row[1]
        for row in conn.execute('PRAGMA table_info(gall2_posts)').fetchall()
    }
    if 'school_id' not in post_columns:
        conn.execute('ALTER TABLE gall2_posts ADD COLUMN school_id INTEGER')
    # 초기 센터별 시범 데이터가 있다면 공용 학교갤러리로 합친다.
    conn.execute(
        'UPDATE gall2_posts SET school_id=? WHERE school_id IS NOT NULL AND school_id<>?',
        (SCHOOL_GALLERY_SCOPE_ID, SCHOOL_GALLERY_SCOPE_ID),
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_gall2_posts_school_id ON gall2_posts(school_id, created_at)')
    try:
        conn.execute("ALTER TABLE gall2 ADD COLUMN post_id INTEGER")
    except Exception:
        pass

    orphan_rows = conn.execute('''
        SELECT id, title, tab_id, created_at
        FROM gall2
        WHERE post_id IS NULL
        ORDER BY id ASC
    ''').fetchall()
    for row in orphan_rows:
        cursor = conn.execute('''
            INSERT INTO gall2_posts (title, content, author, tab_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            row['title'] or '사진 게시물',
            '',
            '관리자',
            row['tab_id'] or 1,
            row['created_at'],
            row['created_at']
        ))
        conn.execute("UPDATE gall2 SET post_id = ? WHERE id = ?", (cursor.lastrowid, row['id']))
    conn.commit()
    conn.close()

def get_or_create_gallery_post(conn, title, content, author, tab_id, upload_token, school_id=None):
    if upload_token:
        conn.execute('''
            INSERT OR IGNORE INTO gall2_posts (title, content, author, tab_id, upload_token, school_id)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (title, content, author, tab_id, upload_token, school_id))
        existing = conn.execute("SELECT id FROM gall2_posts WHERE upload_token = ?", (upload_token,)).fetchone()
        if existing:
            return existing['id']

    cursor = conn.execute('''
        INSERT INTO gall2_posts (title, content, author, tab_id, upload_token, school_id)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (title, content, author, tab_id, upload_token or None, school_id))
    return cursor.lastrowid


def load_gallery_page(conn, page, school_id=None):
    """사내갤러리와 센터장 공유 학교갤러리의 게시물을 범위별로 불러온다."""
    if school_id is None:
        scope_sql = 'p.school_id IS NULL'
        count_sql = 'school_id IS NULL'
        scope_params = ()
    else:
        scope_sql = 'p.school_id = ?'
        count_sql = 'school_id = ?'
        scope_params = (int(school_id),)

    total_posts = conn.execute(
        f'SELECT COUNT(*) FROM gall2_posts WHERE {count_sql}',
        scope_params,
    ).fetchone()[0]
    total_pages = max((total_posts + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE, 1)
    page = max(1, min(int(page or 1), total_pages))
    offset = (page - 1) * POSTS_PER_PAGE
    block_start = ((page - 1) // 10) * 10 + 1
    block_end = min(block_start + 9, total_pages)

    posts_rows = conn.execute(f'''
        SELECT p.*,
               COUNT(g.id) AS photo_count,
               (
                   SELECT thumb_name
                   FROM gall2
                   WHERE post_id = p.id
                   ORDER BY id ASC
                   LIMIT 1
               ) AS cover_thumb
        FROM gall2_posts p
        LEFT JOIN gall2 g ON g.post_id = p.id
        WHERE {scope_sql}
        GROUP BY p.id
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT ? OFFSET ?
    ''', (*scope_params, POSTS_PER_PAGE, offset)).fetchall()
    posts = [dict(row) for row in posts_rows]
    for post in posts:
        image_rows = conn.execute('''
            SELECT id, title, filename, thumb_name, created_at
            FROM gall2
            WHERE post_id = ?
            ORDER BY id ASC
        ''', (post['id'],)).fetchall()
        post['images'] = []
        for row in image_rows:
            image = dict(row)
            file_size = get_gallery_file_size(image['filename'])
            image['file_size'] = file_size
            image['file_size_label'] = format_file_size(file_size)
            post['images'].append(image)

    pagination = {
        'page': page,
        'total_pages': total_pages,
        'total_posts': total_posts,
        'block_start': block_start,
        'block_end': block_end,
        'has_prev_block': block_start > 1,
        'has_next_block': block_end < total_pages,
        'prev_block_page': max(1, block_start - 10),
        'next_block_page': min(total_pages, block_start + 10),
    }
    return posts, pagination

def delete_gallery_file(file_row):
    try:
        target_file = os.path.join(UPLOAD_FOLDER, file_row['filename'])
        target_thumb = os.path.join(THUMB_FOLDER, file_row['thumb_name'])
        if os.path.exists(target_file): os.remove(target_file)
        if os.path.exists(target_thumb): os.remove(target_thumb)
    except Exception as e:
        print(f"파일 삭제 오류: {e}")

def delete_gallery_post(conn, post_id):
    files = conn.execute('SELECT * FROM gall2 WHERE post_id = ?', (post_id,)).fetchall()
    for file in files:
        delete_gallery_file(file)
    conn.execute('DELETE FROM gall2 WHERE post_id = ?', (post_id,))
    conn.execute('DELETE FROM gall2_posts WHERE id = ?', (post_id,))

def generate_thumb_from_raw(temp_path, filename):
    thumb_name = f"thumb_{os.path.splitext(filename)[0]}.jpg"
    thumb_path = os.path.join(THUMB_FOLDER, thumb_name)
    
    try:
        with Image.open(temp_path) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((500, 500))
            img.save(thumb_path, "JPEG", quality=85)
    except Exception as e:
        print(f"썸네일 생성 오류: {e}")
    
    return thumb_name

@gall2_bp.route('/gall2')
def index():
    try:
        ensure_gall2_schema()
        active_tab_id = 1
        page = request.args.get('page', 1, type=int) or 1
        
        conn = get_db()

        posts, pagination = load_gallery_page(conn, page)
        conn.close()
        
        return render_template(
            'gall2.html', posts=posts, tabs=[], active_tab_id=active_tab_id,
            pagination=pagination, school_gallery=None,
            gallery_title='사내 갤러리', gallery_help='사내 갤러리 업로드 안내',
        )
    except Exception as e:
        return f"DB 에러: {e}. 'database.py'에서 init_db()에 gall2 테이블이 생성되었는지 확인하세요."

@gall2_bp.route('/gall2/add_tab', methods=['POST'])
def add_tab():
    return redirect(url_for('gall2.index'))

@gall2_bp.route('/gall2/rename_tab', methods=['POST'])
def rename_tab():
    return jsonify({"status": "disabled"})

@gall2_bp.route('/gall2/delete_tab/<int:tab_id>', methods=['POST'])
def delete_tab(tab_id):
    return redirect(url_for('gall2.index'))

@gall2_bp.route('/gall2/upload', methods=['POST'])
def upload():
    result = save_gallery_upload_request(school_id=None)
    if result is not None:
        return result
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'Dropzone' in request.headers.get('User-Agent', ''):
        return jsonify({"status": "success"})
    return redirect(url_for('gall2.index'))


def save_gallery_upload_request(school_id=None):
    """현재 업로드 요청을 사내갤러리 또는 공유 학교갤러리에 저장한다."""
    ensure_gall2_schema()
    active_tab_id = 1
    files = request.files.getlist('file')
    
    if not files or files[0].filename == '':
        return jsonify({"status": "error", "message": "업로드할 사진을 선택해 주세요."}), 400
    
    title_base = (request.form.get('title') or '').strip()
    content = (request.form.get('content') or '').strip()
    upload_token = (request.form.get('upload_token') or '').strip()
    author = session.get('user_name') or session.get('name') or '관리자'

    if not title_base:
        return jsonify({"status": "error", "message": "게시물 제목을 입력해 주세요."}), 400

    for file in files:
        if file and file.filename != '':
            original_filename = file.filename
            ext = original_filename.split('.')[-1].lower()
            
            if ext not in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']:
                continue
                 
            file_type = 'image'
            filename = build_gallery_filename(original_filename)
            title = original_filename
            
            temp_path = os.path.join(BASE_GALLERY_PATH, f"temp_{filename}")
            try:
                optimize_gallery_image(file, temp_path)
            except (UnidentifiedImageError, OSError, ValueError) as e:
                print(f"이미지 최적화 오류: {e}")
                continue
             
            thumb_name = generate_thumb_from_raw(temp_path, filename)
            
            saved_ok = False
            try:
                with open(temp_path, 'rb') as f:
                    encrypted_data = cipher.encrypt(f.read())
                     
                save_path = os.path.join(UPLOAD_FOLDER, filename)
                with open(save_path, 'wb') as f:
                    f.write(encrypted_data)
                saved_ok = True
            except Exception as e:
                print(f"암호화 오류: {e}")
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

            if not saved_ok:
                continue
             
            conn = get_db()
            post_id = get_or_create_gallery_post(
                conn, title_base, content, author, active_tab_id, upload_token,
                school_id=school_id,
            )
            conn.execute('INSERT INTO gall2 (title, filename, thumb_name, file_type, tab_id, post_id) VALUES (?, ?, ?, ?, ?, ?)',
                         (title, filename, thumb_name, file_type, active_tab_id, post_id))
            conn.commit()
            conn.close()
            
    return None


def require_school_gallery_access(conn, school_key):
    """본사 담당자 또는 해당 센터에 실제 지정된 레벨 8 센터장만 허용한다."""
    school = conn.execute('''
        SELECT id, school_name, access_key, center_director_id, COALESCE(is_active, 1) AS is_active
        FROM schools
        WHERE access_key = ?
    ''', (school_key,)).fetchone()
    if not school:
        abort(404)

    try:
        user_level = int(session.get('user_level', 99))
    except (TypeError, ValueError):
        user_level = 99
    is_headquarters = (
        session.get('user_name') == 'admin'
        or (session.get('emp_no') and 1 <= user_level <= 7)
    )
    is_assigned_director = (
        user_level == 8
        and str(school['center_director_id'] or '') == str(session.get('emp_no') or '')
        and int(school['is_active'] or 0) == 1
    )
    if not is_headquarters and not is_assigned_director:
        abort(403)
    return school


@gall2_bp.route('/school/<string:school_key>/gallery')
def school_gallery(school_key):
    ensure_gall2_schema()
    page = request.args.get('page', 1, type=int) or 1
    conn = get_db()
    try:
        school = require_school_gallery_access(conn, school_key)
        posts, pagination = load_gallery_page(conn, page, school_id=SCHOOL_GALLERY_SCOPE_ID)
        school_info = dict(school)
    finally:
        conn.close()
    return render_template(
        'gall2.html', posts=posts, tabs=[], active_tab_id=1,
        pagination=pagination, school_gallery=school_info,
        gallery_title='학교갤러리',
        gallery_help='센터장 공유 학교갤러리 업로드 안내',
    )


@gall2_bp.route('/school/<string:school_key>/gallery/upload', methods=['POST'])
def school_gallery_upload(school_key):
    ensure_gall2_schema()
    conn = get_db()
    try:
        school = require_school_gallery_access(conn, school_key)
        school_id = SCHOOL_GALLERY_SCOPE_ID
    finally:
        conn.close()
    result = save_gallery_upload_request(school_id=school_id)
    if result is not None:
        return result
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'Dropzone' in request.headers.get('User-Agent', ''):
        return jsonify({'status': 'success'})
    return redirect(url_for('gall2.school_gallery', school_key=school_key))


@gall2_bp.route('/school/<string:school_key>/gallery/post/<int:post_id>/update', methods=['POST'])
def school_gallery_update_post(school_key, post_id):
    ensure_gall2_schema()
    data = request.get_json(silent=True) or request.form
    title = (data.get('title') or '').strip()
    content = (data.get('content') or '').strip()
    if not title:
        return jsonify({'status': 'error', 'message': '제목을 입력해 주세요.'}), 400
    conn = get_db()
    try:
        school = require_school_gallery_access(conn, school_key)
        cursor = conn.execute('''
            UPDATE gall2_posts
            SET title=?, content=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND school_id=?
        ''', (title, content, post_id, SCHOOL_GALLERY_SCOPE_ID))
        if cursor.rowcount != 1:
            return jsonify({'status': 'error', 'message': '게시물을 찾을 수 없습니다.'}), 404
        conn.commit()
        return jsonify({'status': 'success'})
    finally:
        conn.close()


@gall2_bp.route('/school/<string:school_key>/gallery/post/<int:post_id>/delete', methods=['POST'])
def school_gallery_delete_post(school_key, post_id):
    ensure_gall2_schema()
    page = request.args.get('page', 1, type=int) or 1
    conn = get_db()
    try:
        school = require_school_gallery_access(conn, school_key)
        post = conn.execute(
            'SELECT id FROM gall2_posts WHERE id=? AND school_id=?',
            (post_id, SCHOOL_GALLERY_SCOPE_ID),
        ).fetchone()
        if not post:
            abort(404)
        delete_gallery_post(conn, post_id)
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('gall2.school_gallery', school_key=school_key, page=page))


@gall2_bp.route('/school/<string:school_key>/gallery/delete_bulk', methods=['POST'])
def school_gallery_delete_bulk(school_key):
    ensure_gall2_schema()
    data = request.get_json(silent=True) or request.form
    post_ids = data.get('post_ids', [])
    if hasattr(data, 'getlist'):
        post_ids = data.getlist('post_ids') or post_ids
    if isinstance(post_ids, str):
        post_ids = [post_ids]
    conn = get_db()
    try:
        school = require_school_gallery_access(conn, school_key)
        for raw_post_id in post_ids:
            try:
                post_id = int(raw_post_id)
            except (TypeError, ValueError):
                continue
            owned = conn.execute(
                'SELECT id FROM gall2_posts WHERE id=? AND school_id=?',
                (post_id, SCHOOL_GALLERY_SCOPE_ID),
            ).fetchone()
            if owned:
                delete_gallery_post(conn, post_id)
        conn.commit()
        return jsonify({'status': 'success'})
    finally:
        conn.close()


def require_school_gallery_file(conn, school_id, column, filename):
    if column not in {'filename', 'thumb_name'}:
        abort(400)
    row = conn.execute(f'''
        SELECT 1
        FROM gall2 g
        JOIN gall2_posts p ON p.id=g.post_id
        WHERE p.school_id=? AND g.{column}=?
        LIMIT 1
    ''', (school_id, filename)).fetchone()
    if not row:
        abort(404)


@gall2_bp.route('/school/<string:school_key>/gallery/raw/<filename>')
def school_gallery_serve_file(school_key, filename):
    ensure_gall2_schema()
    conn = get_db()
    try:
        school = require_school_gallery_access(conn, school_key)
        require_school_gallery_file(conn, SCHOOL_GALLERY_SCOPE_ID, 'filename', filename)
    finally:
        conn.close()
    return serve_encrypted_gallery_file(filename)


@gall2_bp.route('/school/<string:school_key>/gallery/thumb/<filename>')
def school_gallery_serve_thumb(school_key, filename):
    ensure_gall2_schema()
    conn = get_db()
    try:
        school = require_school_gallery_access(conn, school_key)
        require_school_gallery_file(conn, SCHOOL_GALLERY_SCOPE_ID, 'thumb_name', filename)
    finally:
        conn.close()
    return serve_gallery_thumb_file(filename)

@gall2_bp.route('/gall2/post/<int:post_id>/update', methods=['POST'])
@admin_required
def update_post(post_id):
    ensure_gall2_schema()
    data = request.get_json(silent=True) or request.form
    title = (data.get('title') or '').strip()
    content = (data.get('content') or '').strip()
    if not title:
        return jsonify({"status": "error", "message": "제목을 입력해 주세요."}), 400

    conn = get_db()
    conn.execute('''
        UPDATE gall2_posts
        SET title = ?, content = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND school_id IS NULL
    ''', (title, content, post_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@gall2_bp.route('/gall2/post/<int:post_id>/delete', methods=['POST'])
@admin_required
def delete_post(post_id):
    ensure_gall2_schema()
    page = request.args.get('page', 1, type=int)
    conn = get_db()
    post = conn.execute(
        'SELECT id FROM gall2_posts WHERE id=? AND school_id IS NULL',
        (post_id,),
    ).fetchone()
    if not post:
        conn.close()
        abort(404)
    delete_gallery_post(conn, post_id)
    conn.commit()
    conn.close()
    return redirect(url_for('gall2.index', page=page))

@gall2_bp.route('/gall2/delete/<int:id>')
@admin_required
def delete(id):
    conn = get_db()
    file = conn.execute('''
        SELECT g.*
        FROM gall2 g
        JOIN gall2_posts p ON p.id=g.post_id
        WHERE g.id=? AND p.school_id IS NULL
    ''', (id,)).fetchone()
    
    if file:
        delete_gallery_file(file)
            
        conn.execute('DELETE FROM gall2 WHERE id = ?', (id,))
        conn.commit()
    
    conn.close()
    return redirect(url_for('gall2.index'))

# [신규 추가] 다중 선택 삭제 처리
@gall2_bp.route('/gall2/delete_bulk', methods=['POST'])
@admin_required
def delete_bulk():
    data = request.get_json(silent=True) or request.form
    post_ids = data.get('post_ids', [])
    if hasattr(data, 'getlist'):
        post_ids = data.getlist('post_ids') or post_ids
    ids = data.get('ids', [])
    if hasattr(data, 'getlist'):
        ids = data.getlist('ids') or ids

    if isinstance(post_ids, str):
        post_ids = [post_ids]
    if isinstance(ids, str):
        ids = [ids]

    if not post_ids and not ids:
        return jsonify({"status": "error", "message": "선택된 파일이 없습니다."})

    conn = get_db()
    for post_id in post_ids:
        try:
            post_id = int(post_id)
        except (TypeError, ValueError):
            continue
        global_post = conn.execute(
            'SELECT id FROM gall2_posts WHERE id=? AND school_id IS NULL',
            (post_id,),
        ).fetchone()
        if global_post:
            delete_gallery_post(conn, post_id)

    for file_id in ids:
        try:
            file_id = int(file_id)
        except (TypeError, ValueError):
            continue
        file = conn.execute('''
            SELECT g.*
            FROM gall2 g
            JOIN gall2_posts p ON p.id=g.post_id
            WHERE g.id=? AND p.school_id IS NULL
        ''', (file_id,)).fetchone()
        if file:
            post_id = file['post_id']
            delete_gallery_file(file)
            conn.execute('DELETE FROM gall2 WHERE id = ?', (file_id,))
            if post_id:
                remaining = conn.execute('SELECT COUNT(*) FROM gall2 WHERE post_id = ?', (post_id,)).fetchone()[0]
                if remaining == 0:
                    conn.execute('DELETE FROM gall2_posts WHERE id = ?', (post_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@gall2_bp.route('/gall2/raw/<filename>')
def serve_file(filename):
    conn = get_db()
    allowed = conn.execute('''
        SELECT 1
        FROM gall2 g
        JOIN gall2_posts p ON p.id=g.post_id
        WHERE p.school_id IS NULL AND g.filename=?
        LIMIT 1
    ''', (filename,)).fetchone()
    conn.close()
    if not allowed:
        abort(404)
    return serve_encrypted_gallery_file(filename)


def serve_encrypted_gallery_file(filename):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        return "파일을 찾을 수 없습니다.", 404
        
    with open(file_path, 'rb') as f:
        try:
            decrypted_data = cipher.decrypt(f.read())
        except:
            return "파일 복호화에 실패했습니다.", 500
    
    ext = filename.split('.')[-1].lower()
    mimetypes = {
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'
    }
    mimetype = mimetypes.get(ext, 'application/octet-stream')
    
    return Response(decrypted_data, mimetype=mimetype)

@gall2_bp.route('/gall2/thumb/<filename>')
def serve_thumb(filename):
    conn = get_db()
    allowed = conn.execute('''
        SELECT 1
        FROM gall2 g
        JOIN gall2_posts p ON p.id=g.post_id
        WHERE p.school_id IS NULL AND g.thumb_name=?
        LIMIT 1
    ''', (filename,)).fetchone()
    conn.close()
    if not allowed:
        abort(404)
    return serve_gallery_thumb_file(filename)


def serve_gallery_thumb_file(filename):
    return send_from_directory(THUMB_FOLDER, filename)
