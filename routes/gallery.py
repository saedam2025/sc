from flask import Blueprint, render_template, request, redirect, url_for, send_from_directory, Response
from werkzeug.utils import secure_filename
from .database import get_db
from cryptography.fernet import Fernet
import os
import cv2
from PIL import Image

gallery_bp = Blueprint('gallery', __name__)

# [보안] Fernet 키 규격 준수
KEY = b'uV5Z9X-o3J-7S-9k_L6_QW0Xm8k9V8P4f1L2M3N4O5A=' 
cipher = Fernet(KEY)

# [경로 설정]
BASE_GALLERY_PATH = "/mnt/data/gallery"
UPLOAD_FOLDER = os.path.join(BASE_GALLERY_PATH, 'uploads')
THUMB_FOLDER = os.path.join(BASE_GALLERY_PATH, 'thumbnails')

# 폴더 자동 생성
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(THUMB_FOLDER, exist_ok=True)

def generate_thumb_from_raw(temp_path, filename, file_type):
    """(최적화) 암호화되기 전의 원본 파일에서 바로 썸네일을 생성합니다."""
    thumb_name = f"thumb_{os.path.splitext(filename)[0]}.jpg"
    thumb_path = os.path.join(THUMB_FOLDER, thumb_name)
    
    try:
        if file_type == 'image':
            with Image.open(temp_path) as img:
                # 정사각형 크롭을 위해 썸네일 방식 개선
                img.thumbnail((500, 500))
                img.save(thumb_path, "JPEG", quality=85)
                
        elif file_type == 'video':
            cap = cv2.VideoCapture(temp_path)
            fps = cap.get(cv2.CAP_PROP_FPS)
            target_frame = int(fps * 10) if fps > 0 else 0
            
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            success, frame = cap.read()
            if not success:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                success, frame = cap.read()
                
            if success:
                # 썸네일 용량 최적화를 위해 리사이즈
                height, width = frame.shape[:2]
                if max(height, width) > 500:
                    scale = 500 / max(height, width)
                    frame = cv2.resize(frame, (int(width * scale), int(height * scale)))
                cv2.imwrite(thumb_path, frame)
            cap.release()
            
    except Exception as e:
        print(f"썸네일 생성 오류: {e}")
    
    return thumb_name

@gallery_bp.route('/gallery')
def index():
    try:
        conn = get_db()
        files = conn.execute('SELECT * FROM gallery ORDER BY created_at DESC').fetchall()
        conn.close()
        return render_template('gallery.html', files=files)
    except Exception as e:
        return f"DB 에러: {e}. 'database.py'에서 init_db()가 실행되었는지 확인하세요."

@gallery_bp.route('/gallery/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return redirect(request.url)
    
    file = request.files['file']
    title = request.form.get('title', '제목 없음')
    
    if file and file.filename != '':
        filename = secure_filename(file.filename)
        ext = filename.split('.')[-1].lower()
        file_type = 'video' if ext in ['mp4', 'mov', 'avi', 'mkv', 'wmv'] else 'image'
        
        # [메모리 최적화 로직] 
        # 1. 원본 파일을 임시로 저장합니다. (RAM 부하 방지)
        temp_path = os.path.join(BASE_GALLERY_PATH, f"temp_{filename}")
        file.save(temp_path)
        
        # 2. 임시 원본 파일로 썸네일을 즉시 생성합니다.
        thumb_name = generate_thumb_from_raw(temp_path, filename, file_type)
        
        # 3. 임시 파일을 읽어와 한 번만 암호화하고 최종 저장합니다.
        try:
            with open(temp_path, 'rb') as f:
                encrypted_data = cipher.encrypt(f.read())
                
            save_path = os.path.join(UPLOAD_FOLDER, filename)
            with open(save_path, 'wb') as f:
                f.write(encrypted_data)
        except Exception as e:
            print(f"암호화 오류: {e}")
        finally:
            # 4. 임시 파일 삭제 (용량 확보)
            if os.path.exists(temp_path):
                os.remove(temp_path)
        
        # DB 기록
        conn = get_db()
        conn.execute('INSERT INTO gallery (title, filename, thumb_name, file_type) VALUES (?, ?, ?, ?)',
                     (title, filename, thumb_name, file_type))
        conn.commit()
        conn.close()
        
    return redirect(url_for('gallery.index'))

@gallery_bp.route('/gallery/delete/<int:id>')
def delete(id):
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
    return redirect(url_for('gallery.index'))

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
        'mp4': 'video/mp4', 'mov': 'video/quicktime', 'avi': 'video/x-msvideo',
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif'
    }
    mimetype = mimetypes.get(ext, 'application/octet-stream')
    
    return Response(decrypted_data, mimetype=mimetype)

@gallery_bp.route('/gallery/thumb/<filename>')
def serve_thumb(filename):
    return send_from_directory(THUMB_FOLDER, filename)