from flask import Blueprint, render_template, request, redirect, url_for, send_from_directory, Response, jsonify
from werkzeug.utils import secure_filename
from .database import get_db
from cryptography.fernet import Fernet
import os
from PIL import Image

gallery_bp = Blueprint('gallery', __name__)

# [보안] Fernet 키 규격 준수
KEY = b'uV5Z9X-o3J-7S-9k_L6_QW0Xm8k9V8P4f1L2M3N4O5A=' 
cipher = Fernet(KEY)

BASE_GALLERY_PATH = "/mnt/data/gallery"
UPLOAD_FOLDER = os.path.join(BASE_GALLERY_PATH, 'uploads')
THUMB_FOLDER = os.path.join(BASE_GALLERY_PATH, 'thumbnails')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(THUMB_FOLDER, exist_ok=True)

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

@gallery_bp.route('/gallery')
def index():
    try:
        # 현재 선택된 탭 ID (기본값 1)
        active_tab_id = request.args.get('tab_id', 1, type=int)
        
        conn = get_db()
        tabs = conn.execute('SELECT * FROM gallery_tabs ORDER BY id ASC').fetchall()
        
        # 만약 삭제된 탭 번호로 접근했다면 기본 탭(1)으로 롤백
        if not any(t['id'] == active_tab_id for t in tabs):
            active_tab_id = 1
            
        # 선택된 탭의 파일만 가져오기
        files = conn.execute('SELECT * FROM gallery WHERE tab_id = ? ORDER BY created_at DESC', (active_tab_id,)).fetchall()
        conn.close()
        
        return render_template('gallery.html', files=files, tabs=tabs, active_tab_id=active_tab_id)
    except Exception as e:
        return f"DB 에러: {e}. 'database.py'에서 init_db()가 실행되었는지 확인하세요."

# [신규] 탭 추가
@gallery_bp.route('/gallery/add_tab', methods=['POST'])
def add_tab():
    conn = get_db()
    cursor = conn.execute("INSERT INTO gallery_tabs (name) VALUES ('새 갤러리 탭')")
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return redirect(url_for('gallery.index', tab_id=new_id))

# [신규] 탭 이름 변경 (AJAX)
@gallery_bp.route('/gallery/rename_tab', methods=['POST'])
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

# [신규] 탭 삭제
@gallery_bp.route('/gallery/delete_tab/<int:tab_id>', methods=['POST'])
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
def upload():
    # 현재 선택된 탭 정보 받아오기
    active_tab_id = request.args.get('tab_id', 1, type=int)
    files = request.files.getlist('file')
    
    if not files or files[0].filename == '':
        return redirect(request.url)
    
    title_base = request.form.get('title', '')

    for file in files:
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            ext = filename.split('.')[-1].lower()
            
            if ext not in ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp']:
                continue
                
            file_type = 'image'
            title = title_base if title_base else filename
            
            temp_path = os.path.join(BASE_GALLERY_PATH, f"temp_{filename}")
            file.save(temp_path)
            
            thumb_name = generate_thumb_from_raw(temp_path, filename)
            
            try:
                with open(temp_path, 'rb') as f:
                    encrypted_data = cipher.encrypt(f.read())
                    
                save_path = os.path.join(UPLOAD_FOLDER, filename)
                with open(save_path, 'wb') as f:
                    f.write(encrypted_data)
            except Exception as e:
                print(f"암호화 오류: {e}")
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            
            # [수정] 업로드 시 tab_id 도 함께 저장
            conn = get_db()
            conn.execute('INSERT INTO gallery (title, filename, thumb_name, file_type, tab_id) VALUES (?, ?, ?, ?, ?)',
                         (title, filename, thumb_name, file_type, active_tab_id))
            conn.commit()
            conn.close()
            
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'Dropzone' in request.headers.get('User-Agent', ''):
        return jsonify({"status": "success"})
        
    return redirect(url_for('gallery.index', tab_id=active_tab_id))

@gallery_bp.route('/gallery/delete/<int:id>')
def delete(id):
    active_tab_id = request.args.get('tab_id', 1, type=int)
    conn = get_db()
    file = conn.execute('SELECT * FROM gallery WHERE id = ?', (id,)).fetchone()
    
    if file:
        try:
            target_file = os.path.join(UPLOAD_FOLDER, file['filename'])
            target_thumb = os.path.join(THUMB_FOLDER, file['thumb_name'])
            if os.path.exists(target_file): os.remove(target_file)
            if os.path.exists(target_thumb): os.remove(target_thumb)
        except Exception as e:
            print(f"파일 삭제 오류: {e}")
            
        conn.execute('DELETE FROM gallery WHERE id = ?', (id,))
        conn.commit()
    
    conn.close()
    return redirect(url_for('gallery.index', tab_id=active_tab_id))

@gallery_bp.route('/gallery/raw/<filename>')
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

@gallery_bp.route('/gallery/thumb/<filename>')
def serve_thumb(filename):
    return send_from_directory(THUMB_FOLDER, filename)