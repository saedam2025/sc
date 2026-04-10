from flask import Blueprint, render_template, request, redirect, url_for, send_from_directory, Response
from werkzeug.utils import secure_filename
from .database import get_db
from cryptography.fernet import Fernet
import os
import cv2
from PIL import Image

gallery_bp = Blueprint('gallery', __name__)

# [보안] 암호화 키 설정 (환경 변수 권장, 없으면 고정키 사용)
# 실제 운영 시에는 os.environ.get("GALLERY_KEY") 등으로 관리하세요.
KEY = b'saedam_secure_gallery_key_2026_1234=' 
cipher = Fernet(KEY)

# [경로 설정] 요청하신 mnt/data/gallery 구조
BASE_GALLERY_PATH = "/mnt/data/gallery"
UPLOAD_FOLDER = os.path.join(BASE_GALLERY_PATH, 'uploads')
THUMB_FOLDER = os.path.join(BASE_GALLERY_PATH, 'thumbnails')

# 폴더 자동 생성
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(THUMB_FOLDER, exist_ok=True)

def generate_thumb(filename, file_type):
    source = os.path.join(UPLOAD_FOLDER, filename)
    thumb_name = f"thumb_{os.path.splitext(filename)[0]}.jpg"
    thumb_path = os.path.join(THUMB_FOLDER, thumb_name)
    
    # 1. 암호화된 파일 복호화하여 임시 처리
    with open(source, 'rb') as f:
        try:
            decrypted_data = cipher.decrypt(f.read())
        except:
            return None # 복호화 실패 시 처리

    temp_path = os.path.join(BASE_GALLERY_PATH, "temp_proc")
    with open(temp_path, 'wb') as f:
        f.write(decrypted_data)

    try:
        if file_type == 'image':
            img = Image.open(temp_path)
            img.thumbnail((400, 400))
            img.save(thumb_path, "JPEG")
        elif file_type == 'video':
            cap = cv2.VideoCapture(temp_path)
            fps = cap.get(cv2.CAP_PROP_FPS)
            # 동영상 10초 지점 캡처 (10초 미만일 경우 대비 예외처리)
            target_frame = int(fps * 10) if fps > 0 else 0
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            success, frame = cap.read()
            if not success: # 10초 지점 실패 시 첫 프레임
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                success, frame = cap.read()
            if success:
                cv2.imwrite(thumb_path, frame)
            cap.release()
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    
    return thumb_name

@gallery_bp.route('/gallery')
def index():
    conn = get_db()
    files = conn.execute('SELECT * FROM gallery ORDER BY created_at DESC').fetchall()
    conn.close()
    return render_template('gallery.html', files=files)

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
        
        # 2. 파일 암호화 후 저장
        file_content = file.read()
        encrypted_content = cipher.encrypt(file_content)
        
        save_path = os.path.join(UPLOAD_FOLDER, filename)
        with open(save_path, 'wb') as f:
            f.write(encrypted_content)
        
        # 3. 썸네일 생성
        thumb_name = generate_thumb(filename, file_type)
        
        # 4. DB 기록
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
        # 물리적 파일 삭제 (암호화 원본 & 썸네일)
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, file['filename']))
            os.remove(os.path.join(THUMB_FOLDER, file['thumb_name']))
        except Exception as e:
            print(f"파일 삭제 오류: {e}")
            
        conn.execute('DELETE FROM gallery WHERE id = ?', (id,))
        conn.commit()
    
    conn.close()
    return redirect(url_for('gallery.index'))

# --- 미디어 서빙 라우트 ---

@gallery_bp.route('/gallery/raw/<filename>')
def serve_file(filename):
    """암호화된 파일을 복호화하여 브라우저에 스트리밍"""
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(file_path):
        return "파일을 찾을 수 없습니다.", 404
        
    with open(file_path, 'rb') as f:
        decrypted_data = cipher.decrypt(f.read())
    
    # 확장자에 따른 MIME 타입 추론 (간단히 octet-stream 처리 또는 확장자 체크)
    return Response(decrypted_data, mimetype='application/octet-stream')

@gallery_bp.route('/gallery/thumb/<filename>')
def serve_thumb(filename):
    """썸네일 이미지는 암호화하지 않았으므로 직접 전송"""
    return send_from_directory(THUMB_FOLDER, filename)