from flask import Blueprint, render_template, request, redirect, url_for, Response, jsonify, abort, session
from .database import get_db
from .points import deduct_deleted_post_points
from .security import admin_required, load_credential_secret
from .storage import GALLERY_ROOT
from cryptography.fernet import Fernet
import base64
import hashlib
import os
import uuid
from io import BytesIO
from PIL import Image
from .secure_files import delete_file, encrypted_response, encrypted_storage_name, encrypt_stream, encrypt_upload, is_encrypted_file, original_filename

gallery_bp = Blueprint('gallery', __name__)

_secret_digest = hashlib.sha256(load_credential_secret().encode('utf-8')).digest()
cipher = Fernet(base64.urlsafe_b64encode(_secret_digest))

BASE_GALLERY_PATH = str(GALLERY_ROOT)
UPLOAD_FOLDER = os.path.join(BASE_GALLERY_PATH, 'uploads')
THUMB_FOLDER = os.path.join(BASE_GALLERY_PATH, 'thumbnails')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(THUMB_FOLDER, exist_ok=True)

def _ensure_gallery_schema(conn):
    columns = {row[1] for row in conn.execute('PRAGMA table_info(gallery)').fetchall()}
    if 'original_name' not in columns:
        conn.execute("ALTER TABLE gallery ADD COLUMN original_name TEXT NOT NULL DEFAULT ''")
    if 'uploaded_by' not in columns:
        conn.execute("ALTER TABLE gallery ADD COLUMN uploaded_by TEXT NOT NULL DEFAULT ''")
    if 'point_group' not in columns:
        conn.execute("ALTER TABLE gallery ADD COLUMN point_group TEXT NOT NULL DEFAULT ''")
    conn.commit()


def generate_thumb_from_upload(file_storage, filename):
    thumb_name = encrypted_storage_name(f"thumb_{filename}.jpg")
    thumb_path = os.path.join(THUMB_FOLDER, thumb_name)
    try:
        file_storage.stream.seek(0)
        with Image.open(file_storage.stream) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((500, 500))
            buffer = BytesIO()
            img.save(buffer, "JPEG", quality=85)
            buffer.seek(0)
            encrypt_stream(buffer, thumb_path)
        return thumb_name
    except Exception as e:
        print(f"썸네일 생성 오류: {e}")
    finally:
        file_storage.stream.seek(0)
    return None

@gallery_bp.route('/gallery')
def index():
    try:
        active_tab_id = request.args.get('tab_id', 1, type=int)
        
        conn = get_db()
        _ensure_gallery_schema(conn)
        
        # [수정] 탭 정보와 함께 각 탭에 속한 사진의 개수(photo_count)를 계산합니다.
        tabs_query = '''
            SELECT t.id, t.name, COUNT(g.id) as photo_count 
            FROM gallery_tabs t 
            LEFT JOIN gallery g ON t.id = g.tab_id 
            GROUP BY t.id 
            ORDER BY t.id ASC
        '''
        tabs_rows = conn.execute(tabs_query).fetchall()
        
        # 화면(HTML)에서 photo_count 값을 확실하게 읽을 수 있도록 딕셔너리로 변환합니다.
        tabs = [dict(row) for row in tabs_rows]
        
        # 만약 삭제된 탭 번호로 접근했다면 기본 탭(1)으로 롤백
        if not any(t['id'] == active_tab_id for t in tabs):
            active_tab_id = 1
            
        # 선택된 탭의 파일만 가져오기
        files = conn.execute('SELECT * FROM gallery WHERE tab_id = ? ORDER BY created_at DESC', (active_tab_id,)).fetchall()
        conn.close()
        
        return render_template('gallery.html', files=files, tabs=tabs, active_tab_id=active_tab_id)
    except Exception as e:
        return f"DB 에러: {e}. 'database.py'에서 init_db()가 실행되었는지 확인하세요."

@gallery_bp.route('/gallery/add_tab', methods=['POST'])
@admin_required
def add_tab():
    conn = get_db()
    cursor = conn.execute("INSERT INTO gallery_tabs (name) VALUES ('새 갤러리 탭')")
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return redirect(url_for('gallery.index', tab_id=new_id))

@gallery_bp.route('/gallery/rename_tab', methods=['POST'])
@admin_required
def rename_tab():
    data = request.json
    tab_id = data.get('id')
    new_name = data.get('name')
    if tab_id and new_name:
        conn = get_db()
        conn.execute('UPDATE gallery_tabs SET name = ? WHERE id = ?', (new_name, tab_id))
        conn.commit()
        conn.close()
    return jsonify({"status": "success"})

@gallery_bp.route('/gallery/delete_tab/<int:tab_id>', methods=['POST'])
@admin_required
def delete_tab(tab_id):
    if tab_id != 1: # 1번 기본탭은 절대 삭제 불가
        conn = get_db()
        # 탭을 지우면 안에 있던 사진은 안전하게 기본 탭으로 이동
        conn.execute('UPDATE gallery SET tab_id = 1 WHERE tab_id = ?', (tab_id,))
        conn.execute('DELETE FROM gallery_tabs WHERE id = ?', (tab_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('gallery.index'))

@gallery_bp.route('/gallery/upload', methods=['POST'])
@admin_required
def upload():
    active_tab_id = request.args.get('tab_id', 1, type=int)
    files = request.files.getlist('file')
    
    if not files or files[0].filename == '':
        return redirect(request.url)
    
    title_base = request.form.get('title', '')
    uploaded_by = session.get('user_name') or ''
    point_group = uuid.uuid4().hex

    for file in files:
        if file and file.filename != '':
            display_name = original_filename(file.filename)
            ext = os.path.splitext(display_name)[1].lstrip('.').lower()
            
            if ext not in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']:
                continue
                
            file_type = 'image'
            title = title_base if title_base else display_name
            filename = encrypted_storage_name(display_name)
            save_path = os.path.join(UPLOAD_FOLDER, filename)
            thumb_name = None
            try:
                thumb_name = generate_thumb_from_upload(file, filename)
                if not thumb_name:
                    continue
                encrypt_upload(file, save_path)

                conn = get_db()
                try:
                    _ensure_gallery_schema(conn)
                    conn.execute('''
                        INSERT INTO gallery
                            (title, filename, thumb_name, file_type, tab_id, original_name,
                             uploaded_by, point_group)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        title, filename, thumb_name, file_type, active_tab_id, display_name,
                        uploaded_by, point_group,
                    ))
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:
                delete_file(save_path)
                if thumb_name:
                    delete_file(os.path.join(THUMB_FOLDER, thumb_name))
                print(f"갤러리 파일 저장 오류: {e}")
            
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'Dropzone' in request.headers.get('User-Agent', ''):
        return jsonify({"status": "success"})
        
    return redirect(url_for('gallery.index', tab_id=active_tab_id))

@gallery_bp.route('/gallery/delete/<int:id>')
@admin_required
def delete(id):
    active_tab_id = request.args.get('tab_id', 1, type=int)
    conn = get_db()
    _ensure_gallery_schema(conn)
    file = conn.execute('SELECT * FROM gallery WHERE id = ?', (id,)).fetchone()
    
    if file:
        try:
            target_file = os.path.join(UPLOAD_FOLDER, file['filename'])
            target_thumb = os.path.join(THUMB_FOLDER, file['thumb_name'])
            delete_file(target_file)
            delete_file(target_thumb)
        except Exception as e:
            print(f"파일 삭제 오류: {e}")
            
        conn.execute('DELETE FROM gallery WHERE id = ?', (id,))
        conn.commit()
    
    conn.close()
    current_user = session.get('user_name')
    if file and file['uploaded_by'] and file['uploaded_by'] == current_user:
        deduct_deleted_post_points(
            current_user,
            'legacy-gallery',
            file['point_group'] or file['id'],
        )
    return redirect(url_for('gallery.index', tab_id=active_tab_id))

@gallery_bp.route('/gallery/raw/<filename>')
def serve_file(filename):
    conn = get_db()
    _ensure_gallery_schema(conn)
    row = conn.execute('SELECT title, original_name FROM gallery WHERE filename=?', (filename,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        return "파일을 찾을 수 없습니다.", 404
        
    display_name = row['original_name'] or row['title'] or filename
    if is_encrypted_file(file_path):
        return encrypted_response(file_path, display_name, as_attachment=False)
    try:
        with open(file_path, 'rb') as source:
            legacy_data = cipher.decrypt(source.read())
        mimetype = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','gif':'image/gif','webp':'image/webp','bmp':'image/bmp'}.get(os.path.splitext(display_name)[1].lstrip('.').lower(), 'application/octet-stream')
        return Response(legacy_data, mimetype=mimetype)
    except Exception:
        return "파일 복호화에 실패했습니다.", 500

@gallery_bp.route('/gallery/thumb/<filename>')
def serve_thumb(filename):
    conn = get_db()
    allowed = conn.execute('SELECT 1 FROM gallery WHERE thumb_name=?', (filename,)).fetchone()
    conn.close()
    if not allowed:
        abort(404)
    return encrypted_response(os.path.join(THUMB_FOLDER, filename), 'thumbnail.jpg', as_attachment=False, mimetype='image/jpeg')
