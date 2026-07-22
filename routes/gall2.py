from flask import Blueprint, render_template, request, redirect, url_for, send_from_directory, Response, jsonify, session
from werkzeug.utils import secure_filename
from .database import get_db
from cryptography.fernet import Fernet
import os
from datetime import datetime
from io import BytesIO
from PIL import Image, ImageOps, UnidentifiedImageError

gall2_bp = Blueprint('gall2', __name__)

# [보안] Fernet 키 규격 준수 (기존과 동일한 키 사용)
KEY = b'uV5Z9X-o3J-7S-9k_L6_QW0Xm8k9V8P4f1L2M3N4O5A=' 
cipher = Fernet(KEY)

# 저장 경로를 gall2 전용으로 변경
BASE_GALLERY_PATH = "/mnt/data/gall2"
UPLOAD_FOLDER = os.path.join(BASE_GALLERY_PATH, 'uploads')
THUMB_FOLDER = os.path.join(BASE_GALLERY_PATH, 'thumbnails')
GALLERY_IMAGE_MAX_SIZE = (1920, 1080)
GALLERY_IMAGE_QUALITY = 85
POSTS_PER_PAGE = 18

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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
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

def get_or_create_gallery_post(conn, title, content, author, tab_id, upload_token):
    if upload_token:
        conn.execute('''
            INSERT OR IGNORE INTO gall2_posts (title, content, author, tab_id, upload_token)
            VALUES (?, ?, ?, ?, ?)
        ''', (title, content, author, tab_id, upload_token))
        existing = conn.execute("SELECT id FROM gall2_posts WHERE upload_token = ?", (upload_token,)).fetchone()
        if existing:
            return existing['id']

    cursor = conn.execute('''
        INSERT INTO gall2_posts (title, content, author, tab_id, upload_token)
        VALUES (?, ?, ?, ?, ?)
    ''', (title, content, author, tab_id, upload_token or None))
    return cursor.lastrowid

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

        total_posts = conn.execute('SELECT COUNT(*) FROM gall2_posts').fetchone()[0]
        total_pages = max((total_posts + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE, 1)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * POSTS_PER_PAGE
        block_start = ((page - 1) // 10) * 10 + 1
        block_end = min(block_start + 9, total_pages)

        posts_rows = conn.execute('''
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
            GROUP BY p.id
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT ? OFFSET ?
        ''', (POSTS_PER_PAGE, offset)).fetchall()
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
        conn.close()

        pagination = {
            "page": page,
            "total_pages": total_pages,
            "total_posts": total_posts,
            "block_start": block_start,
            "block_end": block_end,
            "has_prev_block": block_start > 1,
            "has_next_block": block_end < total_pages,
            "prev_block_page": max(1, block_start - 10),
            "next_block_page": min(total_pages, block_start + 10),
        }
        
        return render_template('gall2.html', posts=posts, tabs=[], active_tab_id=active_tab_id, pagination=pagination)
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
    ensure_gall2_schema()
    active_tab_id = 1
    files = request.files.getlist('file')
    
    if not files or files[0].filename == '':
        return redirect(request.url)
    
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
            post_id = get_or_create_gallery_post(conn, title_base, content, author, active_tab_id, upload_token)
            conn.execute('INSERT INTO gall2 (title, filename, thumb_name, file_type, tab_id, post_id) VALUES (?, ?, ?, ?, ?, ?)',
                         (title, filename, thumb_name, file_type, active_tab_id, post_id))
            conn.commit()
            conn.close()
            
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'Dropzone' in request.headers.get('User-Agent', ''):
        return jsonify({"status": "success"})
        
    return redirect(url_for('gall2.index'))

@gall2_bp.route('/gall2/post/<int:post_id>/update', methods=['POST'])
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
        WHERE id = ?
    ''', (title, content, post_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@gall2_bp.route('/gall2/post/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    ensure_gall2_schema()
    page = request.args.get('page', 1, type=int)
    conn = get_db()
    delete_gallery_post(conn, post_id)
    conn.commit()
    conn.close()
    return redirect(url_for('gall2.index', page=page))

@gall2_bp.route('/gall2/delete/<int:id>')
def delete(id):
    conn = get_db()
    file = conn.execute('SELECT * FROM gall2 WHERE id = ?', (id,)).fetchone()
    
    if file:
        delete_gallery_file(file)
            
        conn.execute('DELETE FROM gall2 WHERE id = ?', (id,))
        conn.commit()
    
    conn.close()
    return redirect(url_for('gall2.index'))

# [신규 추가] 다중 선택 삭제 처리
@gall2_bp.route('/gall2/delete_bulk', methods=['POST'])
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
            delete_gallery_post(conn, int(post_id))
        except (TypeError, ValueError):
            continue

    for file_id in ids:
        try:
            file_id = int(file_id)
        except (TypeError, ValueError):
            continue
        file = conn.execute('SELECT * FROM gall2 WHERE id = ?', (file_id,)).fetchone()
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
    return send_from_directory(THUMB_FOLDER, filename)
