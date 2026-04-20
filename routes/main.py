from flask import Blueprint, render_template, jsonify, session, request, current_app, send_from_directory
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import holidays
import os
import json
import urllib.parse
from .database import get_db

main_bp = Blueprint('main', __name__)

UPLOAD_FOLDER = '/mnt/data/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# === 실시간 접속자 상태 관리를 위한 전역 변수 ===
active_users = {}
ACTIVE_TIMEOUT = 5  # 5분(300초) 이내에 활동이 없으면 접속 종료로 간주

@main_bp.before_request
def update_last_active():
    """요청이 들어올 때마다 현재 사용자의 마지막 활동 시간을 갱신합니다."""
    user_name = session.get('user_name')
    if user_name:
        active_users[user_name] = datetime.now()

@main_bp.route('/get_active_users', methods=['GET'])
def get_active_users():
    """현재 접속 중인 직원 목록을 반환하는 API"""
    now = datetime.now()
    active_user_list = []
    
    # 딕셔너리를 순회하며 5분 이내 활동한 사람만 추출
    for user, last_active in list(active_users.items()):
        if now - last_active <= timedelta(minutes=ACTIVE_TIMEOUT):
            active_user_list.append(user)
        else:
            del active_users[user]
            
    active_user_list.sort()
    return jsonify({"active_users": active_user_list})


@main_bp.route('/')
def index():
    cats = ['회의', '면접', '미팅', '외근', '기타', '근태/휴가']
    cat_colors = {
        '회의': '#9b59b6', '면접': '#f1c40f', '미팅': '#1abc9c',
        '외근': '#e67e22', '기타': '#7b8a9e', '근태/휴가': '#e74c3c'
    }

    current_user = session.get('user_name', '배호영') 
    events = []
    
    conn = get_db()

    # [DB 컬럼 및 테이블 자동 확인 및 추가]
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN filename TEXT")
        conn.execute("ALTER TABLE messages ADD COLUMN filepath TEXT")
        conn.commit()
    except:
        pass 
        
    try:
        conn.execute("ALTER TABLE users ADD COLUMN profile_icon TEXT DEFAULT '👤'")
        conn.commit()
    except:
        pass
        
    try:
        conn.execute("ALTER TABLE users ADD COLUMN level INTEGER DEFAULT 0")
        conn.commit()
    except:
        pass

    # 내 메모장 DB 테이블 자동 생성 (없을 경우)
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS memos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT UNIQUE,
                content TEXT,
                filename TEXT,
                filepath TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    except:
        pass

    # [WebLink DB 테이블 자동 생성 및 업데이트]
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS weblinks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                url TEXT,
                favicon_url TEXT,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_weblink_order (
                user_name TEXT PRIMARY KEY,
                order_json TEXT
            )
        ''')
        conn.commit()
    except:
        pass

    # WebLink에 파일 업로드를 위한 컬럼 추가 (이미 있으면 pass)
    try:
        conn.execute("ALTER TABLE weblinks ADD COLUMN type TEXT DEFAULT 'url'")
        conn.execute("ALTER TABLE weblinks ADD COLUMN filename TEXT")
        conn.execute("ALTER TABLE weblinks ADD COLUMN filepath TEXT")
        conn.commit()
    except:
        pass

    # 현재 사용자의 레벨 조회
    user_row = conn.execute("SELECT level FROM users WHERE name=?", (current_user,)).fetchone()
    current_user_level = user_row['level'] if user_row and 'level' in user_row.keys() else 0

    # 1. 일정(Tasks) 로드
    tasks = conn.execute('SELECT * FROM tasks').fetchall()
    for row in tasks:
        owner = row['owner']
        date_str = row['date']
        note = row['note']
        
        cat_map = {
            '회의': ('cat_meeting_title', 'cat_meeting_time'),
            '면접': ('cat_interview_title', 'cat_interview_time'),
            '미팅': ('cat_miting_title', 'cat_miting_time'),
            '외근': ('cat_out_title', 'cat_out_time'),
            '기타': ('cat_etc_title', 'cat_etc_time')
        }
        
        for cat, (t_col, h_col) in cat_map.items():
            if row[t_col]:
                events.append({
                    "id": f"task_{row['id']}_{cat}",
                    "title": row[t_col],
                    "start": date_str,
                    "color": cat_colors[cat],
                    "extendedProps": {
                        "task_id": row['id'], 
                        "owner": owner, "category": cat,
                        "task_title": row[t_col], "task_time": row[h_col] or '',
                        "note": note or '',
                        "cat_회의_제목": row['cat_meeting_title'] or '', "cat_회의_시간": row['cat_meeting_time'] or '',
                        "cat_면접_제목": row['cat_interview_title'] or '', "cat_면접_시간": row['cat_interview_time'] or '',
                        "cat_미팅_제목": row['cat_miting_title'] or '', "cat_미팅_시간": row['cat_miting_time'] or '',
                        "cat_외근_제목": row['cat_out_title'] or '', "cat_외근_시간": row['cat_out_time'] or '',
                        "cat_기타_제목": row['cat_etc_title'] or '', "cat_기타_시간": row['cat_etc_time'] or ''
                    }
                })

    # 2. 근태(Attendance) 로드
    attendances = conn.execute("SELECT * FROM attendance WHERE status='승인'").fetchall()
    for row in attendances:
        events.append({
            "title": str(row['type']),
            "start": str(row['start_date']),
            "end": str(row['end_date']),
            "color": cat_colors['근태/휴가'],
            "allDay": True,
            "extendedProps": {
                "owner": str(row['owner']), "category": "근태/휴가",
                "task_title": str(row['type']), "task_time": "", "note": ""
            }
        })

    # 3. 날짜 계산 및 그룹핑 (우측 판넬)
    today = datetime.now()
    today_date = today.date()
    tomorrow_date = today_date + timedelta(days=1)
    next_week_date = today_date + timedelta(days=7)

    today_grouped = {c: [] for c in cats}
    weekly_grouped = {c: [] for c in cats}

    for e in events:
        try:
            start_date = datetime.strptime(e['start'][:10], '%Y-%m-%d').date()
            end_date = start_date
            if 'end' in e and e['end']:
                end_date_orig = datetime.strptime(e['end'][:10], '%Y-%m-%d').date()
                end_date = end_date_orig - timedelta(days=1) if e.get('allDay') else end_date_orig

            cat = e.get('extendedProps', {}).get('category', '기타')
            owner = e.get('extendedProps', {}).get('owner', '')
            task_title = e.get('extendedProps', {}).get('task_title', '')
            task_time = e.get('extendedProps', {}).get('task_time', '')
            
            disp = f"[{owner}] {task_title}" + (f" ({task_time})" if task_time else "")
            event_copy = e.copy()
            event_copy['display_title_detailed'] = disp

            if start_date <= today_date <= end_date:
                if cat in today_grouped: today_grouped[cat].append(event_copy)
            
            if start_date <= next_week_date and end_date >= tomorrow_date:
                if cat in weekly_grouped: weekly_grouped[cat].append(event_copy)
        except ValueError: continue

    for cat in cats:
        today_grouped[cat].sort(key=lambda x: x['start'])
        weekly_grouped[cat].sort(key=lambda x: x['start'])

    kr_holidays = holidays.KR(years=[today_date.year, today_date.year + 1])
    holidays_dict = {str(date): str(name) for date, name in kr_holidays.items()}

    # 4. 게시판 로드
    board_posts = conn.execute("SELECT * FROM board ORDER BY created_at DESC LIMIT 10").fetchall()
    
    # 5. 메시지 로드 (받은 쪽지, 보낸 쪽지 분리)
    received_messages = conn.execute("SELECT * FROM messages WHERE receiver=? ORDER BY sent_at DESC LIMIT 50", (current_user,)).fetchall()
    sent_messages = conn.execute("SELECT * FROM messages WHERE sender=? ORDER BY sent_at DESC LIMIT 50", (current_user,)).fetchall()

    # 6. 대화 상대 로드 (최신 메시지 순 정렬 및 안 읽은 메시지 개수 조회)
    partners_query = conn.execute('''
        SELECT 
            CASE WHEN sender = ? THEN receiver ELSE sender END AS partner,
            MAX(sent_at) AS last_msg_time,
            SUM(CASE WHEN receiver = ? AND is_read = 0 THEN 1 ELSE 0 END) AS unread_count
        FROM messages 
        WHERE sender = ? OR receiver = ?
        GROUP BY CASE WHEN sender = ? THEN receiver ELSE sender END
        ORDER BY last_msg_time DESC
    ''', (current_user, current_user, current_user, current_user, current_user)).fetchall()
    
    chat_partners = []
    for p in partners_query:
        if p['partner'] != current_user: 
            chat_partners.append({
                'name': p['partner'],
                'unread': p['unread_count']
            })

    # 7. 전체 회원 명단 및 프로필 아이콘 로드 (🚀 수정된 부분: 직급별, 가입순 정렬 적용)
    db_users = conn.execute("SELECT name, profile_icon FROM users WHERE status='승인' ORDER BY level ASC, id ASC").fetchall()
    
    user_list = []
    user_icons = {}
    for u in db_users:
        name = u['name']
        if name not in user_list: # 순서를 유지하면서 중복 제거
            user_list.append(name)
        user_icons[name] = u['profile_icon'] if 'profile_icon' in u.keys() and u['profile_icon'] else '👤'
        
    if current_user not in user_icons:
        user_icons[current_user] = '👤'
    if current_user not in user_list: 
        user_list.append(current_user)

    # 8. 개인 메모 로드 (현재 사용자용)
    my_memo = conn.execute("SELECT * FROM memos WHERE owner = ?", (current_user,)).fetchone()

    # 9. WebLink 및 사용자별 정렬 로드
    weblinks_db = conn.execute("SELECT * FROM weblinks").fetchall()
    weblinks = [dict(row) for row in weblinks_db]
    
    order_row = conn.execute("SELECT order_json FROM user_weblink_order WHERE user_name=?", (current_user,)).fetchone()
    if order_row and order_row['order_json']:
        try:
            order_list = json.loads(order_row['order_json'])
            order_dict = {int(id_val): index for index, id_val in enumerate(order_list)}
            weblinks.sort(key=lambda x: order_dict.get(x['id'], 999999))
        except:
            pass

    conn.close()

    return render_template('main.html', 
                           weblinks=weblinks, current_user_level=current_user_level,
                           events=events, today_grouped=today_grouped, weekly_grouped=weekly_grouped,
                           cats=cats, today_str=today.strftime('%d'), holidays_dict=holidays_dict,
                           current_user=current_user, board_posts=board_posts, 
                           chat_partners=chat_partners,
                           received_messages=received_messages, sent_messages=sent_messages,
                           user_list=user_list, user_icons=user_icons, my_memo=my_memo)

@main_bp.route('/save_task', methods=['POST'])
def save_task():
    try:
        data = request.get_json()
        conn = get_db()
        conn.execute('''
            INSERT INTO tasks (year, date, owner, 
            cat_meeting_title, cat_meeting_time, cat_interview_title, cat_interview_time,
            cat_miting_title, cat_miting_time, cat_out_title, cat_out_time,
            cat_etc_title, cat_etc_time, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data.get('date', '')[:4], data.get('date'), data.get('owner'),
            data.get('회의_제목'), data.get('회의_시간'), data.get('면접_제목'), data.get('면접_시간'),
            data.get('미팅_제목'), data.get('미팅_시간'), data.get('외근_제목'), data.get('외근_시간'),
            data.get('기타_제목'), data.get('기타_시간'), data.get('note')
        ))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@main_bp.route('/update_task/<int:task_id>', methods=['POST'])
def update_task(task_id):
    try:
        data = request.get_json()
        owner = session.get('user_name')
        conn = get_db()
        conn.execute('''
            UPDATE tasks SET 
            date=?, year=?,
            cat_meeting_title=?, cat_meeting_time=?, cat_interview_title=?, cat_interview_time=?,
            cat_miting_title=?, cat_miting_time=?, cat_out_title=?, cat_out_time=?,
            cat_etc_title=?, cat_etc_time=?, note=?
            WHERE id=? AND owner=?
        ''', (
            data.get('date'), data.get('date', '')[:4],
            data.get('회의_제목'), data.get('회의_시간'), data.get('면접_제목'), data.get('면접_시간'),
            data.get('미팅_제목'), data.get('미팅_시간'), data.get('외근_제목'), data.get('외근_시간'),
            data.get('기타_제목'), data.get('기타_시간'), data.get('note'),
            task_id, owner
        ))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@main_bp.route('/delete_task/<int:task_id>', methods=['DELETE'])
def delete_task(task_id):
    try:
        owner = session.get('user_name')
        conn = get_db()
        conn.execute("DELETE FROM tasks WHERE id=? AND owner=?", (task_id, owner))
        conn.commit()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@main_bp.route('/save_board', methods=['POST'])
def save_board():
    title = request.form.get('title')
    content = request.form.get('content')
    author = session.get('user_name', '익명')
    
    file = request.files.get('file')
    filename, filepath = '', ''
    
    if file and file.filename:
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

    conn = get_db()
    conn.execute("INSERT INTO board (title, content, author, filename, filepath) VALUES (?, ?, ?, ?, ?)", 
                 (title, content, author, filename, filepath))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/update_board/<int:post_id>', methods=['POST'])
def update_board(post_id):
    title = request.form.get('title')
    content = request.form.get('content')
    author = session.get('user_name')
    
    file = request.files.get('file')
    conn = get_db()
    
    if file and file.filename:
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        conn.execute("UPDATE board SET title=?, content=?, filename=?, filepath=? WHERE id=? AND author=?", 
                     (title, content, filename, filepath, post_id, author))
    else:
        conn.execute("UPDATE board SET title=?, content=? WHERE id=? AND author=?", 
                     (title, content, post_id, author))
    
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/delete_board/<int:post_id>', methods=['DELETE'])
def delete_board(post_id):
    author = session.get('user_name')
    conn = get_db()
    conn.execute("DELETE FROM board WHERE id=? AND author=?", (post_id, author))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/uploads/<name>')
def download_file(name):
    return send_from_directory(UPLOAD_FOLDER, name)

# --- 메시지/쪽지 관련 API ---

@main_bp.route('/api/unread_messages')
def api_unread_messages():
    """현재 사용자의 안 읽은 메시지 총 개수 및 보낸 사람별 개수를 반환하는 API (자동 갱신용)"""
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({"total_unread": 0, "details": {}})
    
    conn = get_db()
    # 1. 전체 미확인 쪽지 개수
    total_count = conn.execute("SELECT COUNT(*) as count FROM messages WHERE receiver=? AND is_read=0", (current_user,)).fetchone()['count']
    
    # 2. 보낸 사람(발신자)별 미확인 쪽지 개수
    details_query = conn.execute("SELECT sender, COUNT(*) as count FROM messages WHERE receiver=? AND is_read=0 GROUP BY sender", (current_user,)).fetchall()
    details = {row['sender']: row['count'] for row in details_query}
    
    conn.close()
    
    return jsonify({"total_unread": total_count, "details": details})

@main_bp.route('/send_message', methods=['POST'])
def send_message():
    sender = session.get('user_name', '익명')
    receiver = request.form.get('receiver')
    content = request.form.get('content', '')
    
    file = request.files.get('file')
    filename, filepath = '', ''
    
    if file and file.filename:
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

    conn = get_db()
    conn.execute("INSERT INTO messages (sender, receiver, content, filename, filepath) VALUES (?, ?, ?, ?, ?)", 
                 (sender, receiver, content, filename, filepath))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/get_chat_history/<other_user>')
def get_chat_history(other_user):
    current_user = session.get('user_name')
    conn = get_db()
    
    # 상대방이 보낸 메시지를 확인하면 '읽음' 처리
    conn.execute("UPDATE messages SET is_read=1 WHERE receiver=? AND sender=?", (current_user, other_user))
    conn.commit()
    
    chat = conn.execute('''
        SELECT * FROM messages 
        WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?) 
        ORDER BY sent_at ASC
    ''', (current_user, other_user, other_user, current_user)).fetchall()
    
    conn.close()
    
    result = []
    for c in chat:
        fname = c['filename'] if 'filename' in c.keys() else ''
        result.append({
            "id": c['id'], "sender": c['sender'], "receiver": c['receiver'], 
            "content": c['content'], "sent_at": c['sent_at'], "is_read": c['is_read'],
            "filename": fname
        })
        
    return jsonify(result)

@main_bp.route('/delete_message/<int:msg_id>', methods=['DELETE'])
def delete_message(msg_id):
    current_user = session.get('user_name')
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE id=? AND sender=?", (msg_id, current_user))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/check_messages')
def check_messages():
    current_user = session.get('user_name')
    if not current_user: return jsonify({"unread": 0})
    
    conn = get_db()
    unread_count = conn.execute("SELECT COUNT(*) as count FROM messages WHERE receiver=? AND is_read=0", (current_user,)).fetchone()['count']
    conn.close()
    return jsonify({"unread": unread_count})

# --- 내 메모장 DB 저장 관련 API ---

@main_bp.route('/save_my_memo', methods=['POST'])
def save_my_memo():
    content = request.form.get('content', '')
    owner = session.get('user_name')
    
    if not owner:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    file = request.files.get('file')
    filename, filepath = '', ''
    
    if file and file.filename:
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

    conn = get_db()
    
    existing_memo = conn.execute("SELECT * FROM memos WHERE owner = ?", (owner,)).fetchone()
    
    if existing_memo:
        if not filename:
            filename = existing_memo['filename'] if existing_memo['filename'] else ''
            filepath = existing_memo['filepath'] if existing_memo['filepath'] else ''
            
        conn.execute('''
            UPDATE memos 
            SET content = ?, filename = ?, filepath = ?, updated_at = CURRENT_TIMESTAMP
            WHERE owner = ?
        ''', (content, filename, filepath, owner))
    else:
        conn.execute('''
            INSERT INTO memos (owner, content, filename, filepath)
            VALUES (?, ?, ?, ?)
        ''', (owner, content, filename, filepath))
        
    conn.commit()
    conn.close()

    return jsonify({"status": "success"})


# --- 업무사이트링크(WebLink) 관련 API ---

@main_bp.route('/save_weblink', methods=['POST'])
def save_weblink():
    title = request.form.get('title')
    link_type = request.form.get('type', 'url')  # 'url' 또는 'file'
    current_user = session.get('user_name', '익명')
    
    conn = get_db()
    
    if link_type == 'file':
        file = request.files.get('file')
        if file and file.filename:
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            
            url = f'/uploads/{filename}'
            # 파일을 위한 특수 플래그로 favicon_url 설정
            conn.execute("INSERT INTO weblinks (title, type, url, favicon_url, created_by, filename, filepath) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                         (title, 'file', url, 'FILE', current_user, filename, filepath))
    else:
        url = request.form.get('url')
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'http://' + url
        
        parsed_uri = urllib.parse.urlparse(url)
        domain = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
        favicon_url = f"https://www.google.com/s2/favicons?domain={domain}&sz=64"
        
        conn.execute("INSERT INTO weblinks (title, type, url, favicon_url, created_by) VALUES (?, ?, ?, ?, ?)", 
                     (title, 'url', url, favicon_url, current_user))
        
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/update_weblink_order', methods=['POST'])
def update_weblink_order():
    data = request.get_json()
    order_list = data.get('order', [])
    current_user = session.get('user_name')
    
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
        
    order_json = json.dumps(order_list)
    
    conn = get_db()
    conn.execute('''
        INSERT INTO user_weblink_order (user_name, order_json)
        VALUES (?, ?)
        ON CONFLICT(user_name) DO UPDATE SET order_json=excluded.order_json
    ''', (current_user, order_json))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/delete_weblink/<int:link_id>', methods=['DELETE'])
def delete_weblink(link_id):
    current_user = session.get('user_name')
    conn = get_db()

    # 권한 체크: 생성자이거나 레벨 1 이상
    user_row = conn.execute("SELECT level FROM users WHERE name=?", (current_user,)).fetchone()
    user_level = user_row['level'] if user_row and 'level' in user_row.keys() else 0

    link = conn.execute("SELECT * FROM weblinks WHERE id=?", (link_id,)).fetchone()
    if not link:
        return jsonify({"status": "error", "message": "존재하지 않는 링크입니다."}), 404

    if link['created_by'] != current_user and user_level < 1:
        return jsonify({"status": "error", "message": "삭제 권한이 없습니다."}), 403

    # 등록된 파일이 있다면 서버에서 물리 파일도 삭제
    if 'type' in link.keys() and link['type'] == 'file' and link['filepath']:
        if os.path.exists(link['filepath']):
            try:
                os.remove(link['filepath'])
            except:
                pass

    conn.execute("DELETE FROM weblinks WHERE id=?", (link_id,))
    conn.commit()
    conn.close()
    
    return jsonify({"status": "success"})