from flask import Blueprint, render_template, jsonify, session, request, send_file
import os
import uuid
import io
from cryptography.fernet import Fernet
from .database import get_db

memo_bp = Blueprint('memo', __name__)

# 이미지, 일반파일 구분 없이 'memoup' 단일 폴더에 모두 저장
UPLOAD_FOLDER = '/mnt/data/memoup'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# [보안] 1. 암호화 키 설정
# Render 환경변수에 FERNET_SECRET_KEY가 없으면 기본 키를 사용합니다.
SECRET_KEY = os.environ.get('FERNET_SECRET_KEY', b'qQp_5wD1uO2-wWzL7vI2jN6_bH9T5_R-3gH8uO1mVpI=') 
cipher_suite = Fernet(SECRET_KEY)

@memo_bp.route('/', strict_slashes=False)
def memo_board():
    current_user = session.get('user_name')
    if not current_user:
        return "로그인이 필요합니다.", 401
    
    conn = get_db()
    
    # [DB 테이블 자동 확인 및 생성]
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS whiteboard_memos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT,
                type TEXT, 
                content TEXT, 
                filepath TEXT,
                color TEXT DEFAULT '#fff9b1',
                pos_x INTEGER DEFAULT 50,
                pos_y INTEGER DEFAULT 50,
                z_index INTEGER DEFAULT 1,
                width INTEGER,
                height INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    except Exception:
        pass

    # [마이그레이션] 기존 테이블에 크기 조절을 위한 width, height 컬럼 안전하게 추가
    for col in ['width', 'height']:
        try:
            conn.execute(f"ALTER TABLE whiteboard_memos ADD COLUMN {col} INTEGER")
            conn.commit()
        except Exception:
            pass

    # 현재 로그인된 사용자가 저장해둔 메모와 파일들만 로드
    memos = conn.execute("SELECT * FROM whiteboard_memos WHERE owner = ?", (current_user,)).fetchall()
    
    # [상단바 대응] user_icons 정보 가져오기
    try:
        db_users = conn.execute("SELECT name, profile_icon FROM users WHERE status='승인'").fetchall()
        user_icons = {u['name']: (u['profile_icon'] if u['profile_icon'] else '👤') for u in db_users}
    except Exception:
        user_icons = {}
        
    if current_user not in user_icons:
        user_icons[current_user] = '👤'

    conn.close()
    
    # 템플릿으로 데이터를 넘길 때 dict 형태로 변환
    memos_list = [dict(row) for row in memos]
    return render_template('memo.html', memos=memos_list, user_icons=user_icons)


@memo_bp.route('/add_postit', methods=['POST'])
def memo_add_postit():
    current_user = session.get('user_name')
    data = request.get_json()
    color = data.get('color', '#fff9b1')
    
    conn = get_db()
    cursor = conn.cursor()
    
    # [최상단 배치] 현재 가장 높은 z-index 값 찾기
    row = cursor.execute("SELECT MAX(z_index) as max_z FROM whiteboard_memos WHERE owner = ?", (current_user,)).fetchone()
    new_z = (row['max_z'] if row and row['max_z'] is not None else 99) + 1
    
    cursor.execute('''
        INSERT INTO whiteboard_memos (owner, type, content, color, pos_x, pos_y, z_index) 
        VALUES (?, 'postit', '', ?, 100, 100, ?)
    ''', (current_user, color, new_z))
    
    memo_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "id": memo_id, "color": color})


@memo_bp.route('/upload_file', methods=['POST'])
def memo_upload_file():
    current_user = session.get('user_name')
    file = request.files.get('file')
    
    if not file or not file.filename:
        return jsonify({"status": "error", "message": "첨부된 파일이 없습니다."}), 400
        
    # [한글 보존] secure_filename 대신 경로 구분자만 제거
    original_filename = file.filename.replace('/', '').replace('\\', '')
    
    # [파일명 고유화] UUID 사용
    unique_id = uuid.uuid4().hex
    saved_filename = f"{unique_id}_{original_filename}.enc"
    
    ext = original_filename.split('.')[-1].lower()
    memo_type = 'image' if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'file'
    
    filepath = os.path.join(UPLOAD_FOLDER, saved_filename)
    
    # [암호화 저장]
    file_data = file.read()
    encrypted_data = cipher_suite.encrypt(file_data)
    
    with open(filepath, 'wb') as f:
        f.write(encrypted_data)
    
    conn = get_db()
    cursor = conn.cursor()
    
    # [최상단 배치]
    row = cursor.execute("SELECT MAX(z_index) as max_z FROM whiteboard_memos WHERE owner = ?", (current_user,)).fetchone()
    new_z = (row['max_z'] if row and row['max_z'] is not None else 99) + 1
    
    # content: 원래 파일명, filepath: 암호화된 파일명
    cursor.execute('''
        INSERT INTO whiteboard_memos (owner, type, content, filepath, pos_x, pos_y, z_index) 
        VALUES (?, ?, ?, ?, 150, 150, ?)
    ''', (current_user, memo_type, original_filename, saved_filename, new_z))
    
    memo_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "id": memo_id, "type": memo_type, "filename": original_filename})


@memo_bp.route('/file/<filename>')
def serve_secure_file(filename):
    current_user = session.get('user_name')
    if not current_user:
        return "Unauthorized", 401
        
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(filepath):
        return "File Not Found", 404
        
    # [파일명 복구] DB에서 원래 이름 찾기
    conn = get_db()
    memo = conn.execute("SELECT content FROM whiteboard_memos WHERE filepath = ?", (filename,)).fetchone()
    conn.close()

    if memo and memo['content']:
        original_filename = memo['content']
    else:
        original_filename = filename.split('_', 1)[-1].replace('.enc', '')
        
    # [복호화]
    try:
        with open(filepath, 'rb') as f:
            encrypted_data = f.read()
        decrypted_data = cipher_suite.decrypt(encrypted_data)
    except Exception:
        return "Decryption Failed", 500

    ext = original_filename.split('.')[-1].lower()
    as_attachment = ext not in ['png', 'jpg', 'jpeg', 'gif', 'webp']

    return send_file(
        io.BytesIO(decrypted_data),
        download_name=original_filename,
        as_attachment=as_attachment
    )


@memo_bp.route('/update', methods=['POST'])
def memo_update():
    data = request.get_json()
    memo_id = data.get('id')
    
    updates = []
    params = []
    
    # 업데이트 가능한 모든 컬럼 체크
    fields = ['pos_x', 'pos_y', 'z_index', 'content', 'width', 'height']
    for field in fields:
        if field in data:
            updates.append(f"{field} = ?")
            params.append(data[field])
        
    if not updates:
        return jsonify({"status": "success"})
        
    params.extend([memo_id, session.get('user_name')])
    
    query = f"UPDATE whiteboard_memos SET {', '.join(updates)} WHERE id = ? AND owner = ?"
    
    conn = get_db()
    conn.execute(query, tuple(params))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success"})


@memo_bp.route('/delete/<int:memo_id>', methods=['DELETE'])
def memo_delete(memo_id):
    owner = session.get('user_name')
    conn = get_db()
    
    memo = conn.execute("SELECT type, filepath FROM whiteboard_memos WHERE id = ? AND owner = ?", (memo_id, owner)).fetchone()
    
    if memo:
        conn.execute("DELETE FROM whiteboard_memos WHERE id = ? AND owner = ?", (memo_id, owner))
        conn.commit()
        
        if memo['type'] in ['file', 'image'] and memo['filepath']:
            file_path = os.path.join(UPLOAD_FOLDER, memo['filepath'])
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
                    
    conn.close()
    return jsonify({"status": "success"})