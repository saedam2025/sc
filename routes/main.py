from flask import Blueprint, render_template, jsonify, session, request, current_app, send_from_directory
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import holidays
import os
import json
import urllib.parse
from .database import get_db
from .board import init_board_db  # 💡 새로 추가: board.py에서 게시판 DB 초기화 함수 임포트

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
    # 💡 새로 추가: 메인화면 접속 시 게시판 관련 테이블(4개)이 없으면 자동 생성
    try:
        init_board_db()
    except Exception as e:
        print(f"게시판 DB 초기화 오류: {e}")

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
        
    # 그룹 채팅 지원을 위한 room_id 컬럼 추가
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN room_id TEXT")
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
    try:
        # 통합 테이블인 board_posts에서 board_en이 'noti'인 글만 10개 가져오기
        board_posts = conn.execute("SELECT * FROM board_posts WHERE board_en='noti' ORDER BY id DESC LIMIT 10").fetchall()
    except Exception as e:
        print(f"🚨 게시판 로드 에러: {e}")
        board_posts = []

    # 5. 메시지 로드 (받은 쪽지, 보낸 쪽지 분리)
    received_messages = conn.execute("SELECT * FROM messages WHERE receiver=? ORDER BY sent_at DESC LIMIT 50", (current_user,)).fetchall()
    sent_messages = conn.execute("SELECT * FROM messages WHERE sender=? ORDER BY sent_at DESC LIMIT 50", (current_user,)).fetchall()

    # 6. 대화 상대 로드 (최신 메시지 순 정렬 및 안 읽은 메시지 개수 조회 - 그룹채팅 방(room_id) 포함)
    partners_query = conn.execute('''
        SELECT 
            CASE WHEN room_id IS NOT NULL THEN room_id
                 WHEN sender = ? THEN receiver ELSE sender END AS partner,
            MAX(sent_at) AS last_msg_time,
            SUM(CASE WHEN receiver = ? AND (is_read IN (0, '0', 'False', 'false') OR is_read IS NULL) THEN 1 ELSE 0 END) AS unread_count
        FROM messages 
        WHERE sender = ? OR receiver = ?
        GROUP BY CASE WHEN room_id IS NOT NULL THEN room_id
                      WHEN sender = ? THEN receiver ELSE sender END
        ORDER BY last_msg_time DESC
    ''', (current_user, current_user, current_user, current_user, current_user)).fetchall()
    
    chat_partners = []
    for p in partners_query:
        if p['partner'] != current_user: 
            chat_partners.append({
                'name': p['partner'],
                'unread': p['unread_count']
            })

    # 7. 전체 회원 명단 및 프로필 아이콘 로드 (직급별, 가입순 정렬 적용)
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

    # 10. 사내갤러리(gall2) 최신 사진 미리보기 로드
    gallery_preview_items = []
    gallery_total_count = 0
    try:
        gallery_total_count = conn.execute("SELECT COUNT(*) FROM gall2_posts").fetchone()[0]
        gallery_rows = conn.execute('''
            SELECT p.id, p.title, p.author, p.created_at, t.name AS tab_name,
                   (
                       SELECT COUNT(*)
                       FROM gall2 AS post_gallery
                       WHERE post_gallery.post_id = p.id
                   ) AS photo_count,
                   (
                       SELECT thumb_name
                       FROM gall2 AS cover_gallery
                       WHERE cover_gallery.post_id = p.id
                       ORDER BY cover_gallery.id ASC
                       LIMIT 1
                   ) AS thumb_name
            FROM gall2_posts p
            LEFT JOIN gall2_tabs t ON p.tab_id = t.id
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT 5
        ''').fetchall()
        gallery_preview_items = [dict(row) for row in gallery_rows]
    except Exception as e:
        print(f"사내갤러리 미리보기 로드 에러: {e}")

    conn.close()

    return render_template('main.html', 
                           weblinks=weblinks, current_user_level=current_user_level,
                           events=events, today_grouped=today_grouped, weekly_grouped=weekly_grouped,
                           cats=cats, today_str=today.strftime('%d'), holidays_dict=holidays_dict,
                            current_user=current_user, board_posts=board_posts, 
                            chat_partners=chat_partners,
                            received_messages=received_messages, sent_messages=sent_messages,
                           user_list=user_list, user_icons=user_icons, my_memo=my_memo,
                           gallery_preview_items=gallery_preview_items,
                           gallery_total_count=gallery_total_count)

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

UNREAD_CONDITION = "(is_read IN (0, '0', 'False', 'false') OR is_read IS NULL)"

def _build_chat_rooms_for_api(conn, current_user):
    rows = conn.execute('''
        SELECT id, sender, receiver, content, sent_at, room_id, is_read
        FROM messages
        WHERE sender=? OR receiver=?
        ORDER BY sent_at DESC, id DESC
    ''', (current_user, current_user)).fetchall()

    unread_rows = conn.execute(f'''
        SELECT CASE WHEN room_id IS NOT NULL THEN room_id ELSE sender END AS partner, COUNT(*) AS count
        FROM messages
        WHERE receiver=? AND {UNREAD_CONDITION}
        GROUP BY CASE WHEN room_id IS NOT NULL THEN room_id ELSE sender END
    ''', (current_user,)).fetchall()
    unread_by_partner = {row['partner']: int(row['count']) for row in unread_rows}

    rooms = {}
    for row in rows:
        room_id = row['room_id']
        partner = room_id if room_id else (row['receiver'] if row['sender'] == current_user else row['sender'])
        if not partner or partner == current_user or partner in rooms:
            continue

        rooms[partner] = {
            'partner': partner,
            'is_group': bool(room_id),
            'last_message': row['content'] or '',
            'last_msg_time': row['sent_at'] or '',
            'last_id': int(row['id']),
            'unread_count': unread_by_partner.get(partner, 0),
        }

    for partner, unread_count in unread_by_partner.items():
        if partner and partner not in rooms:
            rooms[partner] = {
                'partner': partner,
                'is_group': ',' in partner,
                'last_message': '',
                'last_msg_time': '',
                'last_id': 0,
                'unread_count': unread_count,
            }

    return sorted(rooms.values(), key=lambda room: (room['last_msg_time'], room['last_id']), reverse=True)


# --- 메시지/쪽지 관련 API ---

@main_bp.route('/api/unread_messages')
def api_unread_messages():
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({"total_unread": 0, "details": {}, "rooms": []})

    conn = get_db()
    rooms = _build_chat_rooms_for_api(conn, current_user)
    conn.close()

    details = {room['partner']: room['unread_count'] for room in rooms if room['unread_count'] > 0}
    total_count = sum(details.values())
    return jsonify({"total_unread": total_count, "details": details, "rooms": rooms})

@main_bp.route('/send_message', methods=['POST'])
def send_message():
    sender = session.get('user_name', '익명')
    receivers_str = request.form.get('receiver', '')
    content = request.form.get('content', '')
    
    # 그룹 채팅 플래그 및 기존 방 ID 체크
    is_group_chat = request.form.get('is_group_chat') == 'true'
    room_id_input = request.form.get('room_id')
    
    if room_id_input:
        # 기존에 생성된 그룹 채팅방 내부에서 발송하는 경우
        participants = room_id_input.split(',')
        receivers = [p.strip() for p in participants if p.strip() != sender]
        room_id = room_id_input
    else:
        # 신규 발송: 수신자가 쉼표로 여러 명 들어온 경우
        receivers = [r.strip() for r in receivers_str.split(',') if r.strip()]
        if is_group_chat and len(receivers) > 1:
            # 새로운 그룹 채팅방 생성 (이름을 정렬하여 고유 ID처럼 활용)
            participants = sorted(receivers + [sender])
            room_id = ",".join(participants)
        else:
            # 단체 쪽지 일괄 전송 혹은 1:1 대화
            room_id = None
    
    file = request.files.get('file')
    filename, filepath = '', ''
    
    if file and file.filename:
        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

    conn = get_db()
    
    if receivers:
        for rec in receivers:
            if room_id:
                conn.execute("INSERT INTO messages (sender, receiver, content, filename, filepath, room_id, is_read) VALUES (?, ?, ?, ?, ?, ?, 0)", 
                             (sender, rec, content, filename, filepath, room_id))
            else:
                conn.execute("INSERT INTO messages (sender, receiver, content, filename, filepath, is_read) VALUES (?, ?, ?, ?, ?, 0)", 
                             (sender, rec, content, filename, filepath))
                             
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/get_chat_history/<other_user>')
def get_chat_history(other_user):
    current_user = session.get('user_name')
    conn = get_db()
    
    if ',' in other_user:
        # 그룹 채팅방(room_id)인 경우
        room_id = other_user
        # 내가 수신자인 메시지들을 읽음 처리
        conn.execute("UPDATE messages SET is_read=1 WHERE receiver=? AND room_id=?", (current_user, room_id))
        conn.commit()
        
        # 중복 방지를 위해 메세지 내용, 발신자, 시간 단위로 그룹화
        chat = conn.execute('''
            SELECT MIN(id) as id, sender, room_id as receiver, content, sent_at, MAX(filename) as filename, MAX(filepath) as filepath,
                   SUM(CASE WHEN (is_read IN (0, '0', 'False', 'false') OR is_read IS NULL) THEN 1 ELSE 0 END) as unread_count
            FROM messages 
            WHERE room_id=? 
            GROUP BY sender, content, sent_at
            ORDER BY sent_at ASC
        ''', (room_id,)).fetchall()
        
        result = []
        for c in chat:
            fname = c['filename'] if 'filename' in c.keys() else ''
            result.append({
                "id": c['id'], "sender": c['sender'], "receiver": room_id, 
                "content": c['content'], "sent_at": c['sent_at'], "is_read": 1,
                "unread_count": int(c['unread_count']) if c['unread_count'] else 0,
                "filename": fname, "is_group": True
            })
    else:
        # 1:1 채팅인 경우
        conn.execute("UPDATE messages SET is_read=1 WHERE receiver=? AND sender=? AND room_id IS NULL", (current_user, other_user))
        conn.commit()
        
        chat = conn.execute('''
            SELECT id, sender, receiver, content, sent_at, filename, filepath, is_read,
                   CASE WHEN (is_read IN (0, '0', 'False', 'false') OR is_read IS NULL) THEN 1 ELSE 0 END as unread_count
            FROM messages 
            WHERE ((sender=? AND receiver=?) OR (sender=? AND receiver=?)) AND room_id IS NULL
            ORDER BY sent_at ASC
        ''', (current_user, other_user, other_user, current_user)).fetchall()
        
        result = []
        for c in chat:
            fname = c['filename'] if 'filename' in c.keys() else ''
            result.append({
                "id": c['id'], "sender": c['sender'], "receiver": c['receiver'], 
                "content": c['content'], "sent_at": c['sent_at'], "is_read": c['is_read'],
                "unread_count": int(c['unread_count']) if c['unread_count'] else 0,
                "filename": fname, "is_group": False
            })
        
    conn.close()
    return jsonify(result)

@main_bp.route('/delete_message/<int:msg_id>', methods=['DELETE'])
def delete_message(msg_id):
    current_user = session.get('user_name')
    conn = get_db()
    
    # 선택한 메시지가 그룹 채팅인지 1:1인지 확인 후 일괄 삭제
    msg = conn.execute("SELECT room_id, content, sent_at FROM messages WHERE id=? AND sender=?", (msg_id, current_user)).fetchone()
    if msg:
        if msg['room_id']:
            # 그룹 채팅일 경우 수신자별로 생성된 모든 동일 메시지를 지움
            conn.execute("DELETE FROM messages WHERE room_id=? AND sender=? AND content=? AND sent_at=?", 
                         (msg['room_id'], current_user, msg['content'], msg['sent_at']))
        else:
            conn.execute("DELETE FROM messages WHERE id=? AND sender=?", (msg_id, current_user))
        conn.commit()
        
    conn.close()
    return jsonify({"status": "success"})

@main_bp.route('/check_messages')
def check_messages():
    current_user = session.get('user_name')
    if not current_user: return jsonify({"unread": 0})
    
    conn = get_db()
    unread_count = conn.execute("SELECT COUNT(*) as count FROM messages WHERE receiver=? AND (is_read IN (0, '0', 'False', 'false') OR is_read IS NULL)", (current_user,)).fetchone()['count']
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
    current_user = session.get('user_name')

    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    
    conn = get_db()
    user_row = conn.execute("SELECT level FROM users WHERE name=?", (current_user,)).fetchone()
    try:
        user_level = int(user_row['level']) if user_row else 99
    except (TypeError, ValueError):
        user_level = 99

    if not 1 <= user_level <= 5:
        conn.close()
        return jsonify({"status": "error", "message": "링크 등록 권한이 없습니다."}), 403
    
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

    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    conn = get_db()

    user_row = conn.execute("SELECT level FROM users WHERE name=?", (current_user,)).fetchone()
    try:
        user_level = int(user_row['level']) if user_row else 99
    except (TypeError, ValueError):
        user_level = 99

    if not 1 <= user_level <= 5:
        conn.close()
        return jsonify({"status": "error", "message": "링크 삭제 권한이 없습니다."}), 403

    link = conn.execute("SELECT * FROM weblinks WHERE id=?", (link_id,)).fetchone()
    if not link:
        conn.close()
        return jsonify({"status": "error", "message": "존재하지 않는 링크입니다."}), 404

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


    # 메신저 메뉴 모듈화=====================================================
@main_bp.app_context_processor
def inject_chat_data():
    """모든 템플릿에서 쪽지/대화 관련 데이터를 사용할 수 있도록 전역 주입"""
    current_user = session.get('user_name')
    if not current_user:
        return {}

    conn = get_db()
    
    # 1. 받은 쪽지, 보낸 쪽지
    received_messages = conn.execute("SELECT * FROM messages WHERE receiver=? ORDER BY sent_at DESC LIMIT 50", (current_user,)).fetchall()
    sent_messages = conn.execute("SELECT * FROM messages WHERE sender=? ORDER BY sent_at DESC LIMIT 50", (current_user,)).fetchall()

    # 2. 대화 상대 (최신 메시지 순 정렬 및 안 읽은 메시지 개수 - 그룹채팅 방(room_id) 포함)
    partners_query = conn.execute('''
        SELECT 
            CASE WHEN room_id IS NOT NULL THEN room_id
                 WHEN sender = ? THEN receiver ELSE sender END AS partner,
            MAX(sent_at) AS last_msg_time,
            SUM(CASE WHEN receiver = ? AND (is_read IN (0, '0', 'False', 'false') OR is_read IS NULL) THEN 1 ELSE 0 END) AS unread_count
        FROM messages 
        WHERE sender = ? OR receiver = ?
        GROUP BY CASE WHEN room_id IS NOT NULL THEN room_id
                      WHEN sender = ? THEN receiver ELSE sender END
        ORDER BY last_msg_time DESC
    ''', (current_user, current_user, current_user, current_user, current_user)).fetchall()
    
    chat_partners = [{'name': p['partner'], 'unread': p['unread_count']} for p in partners_query if p['partner'] != current_user]

    # 3. 전체 회원 명단 및 프로필 아이콘 (조직도 및 수신자 선택용)
    db_users = conn.execute("SELECT name, profile_icon FROM users WHERE status='승인' ORDER BY level ASC, id ASC").fetchall()
    user_list = []
    user_icons = {}
    for u in db_users:
        name = u['name']
        if name not in user_list:
            user_list.append(name)
        user_icons[name] = u['profile_icon'] if 'profile_icon' in u.keys() and u['profile_icon'] else '👤'
        
    conn.close()

    return dict(
        widget_recv_msgs=received_messages,
        widget_sent_msgs=sent_messages,
        widget_chat_partners=chat_partners,
        chat_user_list=user_list,
        chat_user_icons=user_icons
    )
