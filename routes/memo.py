from flask import Blueprint, render_template, jsonify, session, request
from werkzeug.utils import secure_filename
import os
from .database import get_db

memo_bp = Blueprint('memo', __name__)

UPLOAD_FOLDER = '/mnt/data/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# strict_slashes=False 설정: /memo 와 /memo/ 모두 정상적으로 동일한 화면을 띄워줌
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
    
    return render_template('memo.html', memos=memos, user_icons=user_icons)


@memo_bp.route('/add_postit', methods=['POST'])
def memo_add_postit():
    current_user = session.get('user_name')
    data = request.get_json()
    color = data.get('color', '#fff9b1')
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO whiteboard_memos (owner, type, content, color, pos_x, pos_y, z_index) 
        VALUES (?, 'postit', '', ?, 100, 100, 1)
    ''', (current_user, color))
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
        
    filename = secure_filename(file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    
    ext = filename.split('.')[-1].lower()
    memo_type = 'image' if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'file'
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO whiteboard_memos (owner, type, content, filepath, pos_x, pos_y, z_index) 
        VALUES (?, ?, ?, ?, 150, 150, 1)
    ''', (current_user, memo_type, filename, filename))
    memo_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success", "id": memo_id, "type": memo_type, "filename": filename})


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
    conn.execute("DELETE FROM whiteboard_memos WHERE id = ? AND owner = ?", (memo_id, owner))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})