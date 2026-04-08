from flask import Blueprint, render_template, jsonify, session, request, send_file
from werkzeug.utils import secure_filename
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    except Exception as e:
        pass

    # [핵심 수정] 기존 테이블에 크기 조절을 위한 width, height 컬럼 안전하게 추가
    try:
        conn.execute("ALTER TABLE whiteboard_memos ADD COLUMN width INTEGER")
        conn.commit()
    except Exception:
        pass
        
    try:
        conn.execute("ALTER TABLE whiteboard_memos ADD COLUMN height INTEGER")
        conn.commit()
    except Exception:
        pass

    # 현재 로그인된 사용자가 저장해둔 메모와 파일들만 로드
    memos = conn.execute("SELECT * FROM whiteboard_memos WHERE owner = ?", (current_user,)).fetchall()
    
    # [500 에러 해결 핵심] base.html 상단바 렌더링 시 필요한 user_icons를 무조건 전송
    try:
        db_users = conn.execute("SELECT name, profile_icon FROM users WHERE status='승인'").fetchall()
        user_icons = {}
        for u in db_users:
            user_icons[u['name']] = u['profile_icon'] if 'profile_icon' in u.keys() and u['profile_icon'] else '👤'
    except Exception:
        user_icons = {}
        
    if current_user not in user_icons:
        user_icons[current_user] = '👤'

    conn.close()
    
    # 템플릿으로 데이터를 넘길 때 dict 형태로 변환 (width, height 접근 위함)
    memos_list = [dict(row) for row in memos]
    return render_template('memo.html', memos=memos_list, user_icons=user_icons)


@memo_bp.route('/add_postit', methods=['POST'])
def memo_add_postit():
    current_user = session.get('user_name')
    data = request.get_json()
    color = data.get('color', '#fff9b1')
    
    conn = get_db()
    cursor = conn.cursor()
    
    # 현재 화이트보드에서 가장 높은 z-index 값 찾기
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
        
    # 한글 파일명이 지워지는 secure_filename 대신, 슬래시만 안전하게 제거하여 원본 이름 보존
    original_filename = file.filename.replace('/', '').replace('\\', '')
    
    # 파일명 겹침 방지 (UUID 사용)
    unique_id = uuid.uuid4().hex
    saved_filename = f"{unique_id}_{original_filename}.enc" # 암호화 명시
    
    ext = original_filename.split('.')[-1].lower()
    memo_type = 'image' if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'file'
    
    # memoup 폴더로 단일화 저장 경로
    filepath = os.path.join(UPLOAD_FOLDER, saved_filename)
    
    # 파일 내용 암호화 및 물리적 저장
    file_data = file.read()
    encrypted_data = cipher_suite.encrypt(file_data)
    
    with open(filepath, 'wb') as f:
        f.write(encrypted_data)
    
    conn = get_db()
    cursor = conn.cursor()
    
    # 첨부파일 추가 시에도 가장 높은 z-index 적용
    row = cursor.execute("SELECT MAX(z_index) as max_z FROM whiteboard_memos WHERE owner = ?", (current_user,)).fetchone()
    new_z = (row['max_z'] if row and row['max_z'] is not None else 99) + 1
    
    # content: 화면 표시 및 다운로드 복구용 한글 원본 파일명
    # filepath: 서버에 실제 저장된 암호화 고유 파일명
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
        return "로그인이 필요합니다.", 401
        
    # memoup 단일 경로에서 파일 찾기
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(filepath):
        return "파일을 찾을 수 없습니다.", 404
        
    # DB에서 완벽한 원본 파일명(content) 찾아오기
    conn = get_db()
    memo = conn.execute("SELECT content FROM whiteboard_memos WHERE filepath = ?", (filename,)).fetchone()
    conn.close()

    if memo and memo['content']:
        original_filename = memo['content']
    else:
        original_filename = filename.split('_', 1)[-1].replace('.enc', '')
        
    # 파일 복호화
    try:
        with open(filepath, 'rb') as f:
            encrypted_data = f.read()
        decrypted_data = cipher_suite.decrypt(encrypted_data)
    except Exception as e:
        return f"파일 복호화 실패 또는 손상된 파일입니다.", 500

    # 원본 파일명의 확장자로 다운로드 처리 여부 파악
    ext = original_filename.split('.')[-1].lower()
    as_attachment = ext not in ['png', 'jpg', 'jpeg', 'gif', 'webp']

    # 다운로드 시 원본 파일명으로 강제 지정하여 브라우저에 전송
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
    
    if 'pos_x' in data:
        updates.append("pos_x = ?")
        params.append(data['pos_x'])
    if 'pos_y' in data:
        updates.append("pos_y = ?")
        params.append(data['pos_y'])
    if 'z_index' in data:
        updates.append("z_index = ?")
        params.append(data['z_index'])
    if 'content' in data:
        updates.append("content = ?")
        params.append(data['content'])
        
    # [핵심 수정] 크기 조절 데이터 업데이트
    if 'width' in data:
        updates.append("width = ?")
        params.append(data['width'])
    if 'height' in data:
        updates.append("height = ?")
        params.append(data['height'])
        
    if not updates:
        return jsonify({"status": "success"})
        
    params.append(memo_id)
    params.append(session.get('user_name'))
    
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
    
    # 1. DB에서 삭제할 메모의 정보 조회
    memo = conn.execute("SELECT type, filepath FROM whiteboard_memos WHERE id = ? AND owner = ?", (memo_id, owner)).fetchone()
    
    if memo:
        # 2. DB 레코드 삭제
        conn.execute("DELETE FROM whiteboard_memos WHERE id = ? AND owner = ?", (memo_id, owner))
        conn.commit()
        
        # 3. 물리 파일 삭제
        if memo['type'] in ['file', 'image'] and memo['filepath']:
            file_path = os.path.join(UPLOAD_FOLDER, memo['filepath'])
            
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    pass
                    
    conn.close()
    return jsonify({"status": "success"})