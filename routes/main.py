from flask import Blueprint, render_template, jsonify, session, request, current_app, send_from_directory
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import holidays
import os
from .database import get_db

main_bp = Blueprint('main', __name__)

UPLOAD_FOLDER = '/mnt/data/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@main_bp.route('/')
def index():
    cats = ['회의', '면접', '미팅', '외근', '기타', '근태/휴가']
    cat_colors = {
        '회의': '#9b59b6', '면접': '#f1c40f', '미팅': '#1abc9c',
        '외근': '#e67e22', '기타': '#7b8a9e', '근태/휴가': '#e74c3c'
    }

    current_user = session.get('user_name', '배서현') 
    events = []
    
    conn = get_db()

    # [DB 컬럼 자동 확인 및 추가]
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

    # 7. 전체 회원 명단 및 프로필 아이콘 로드
    db_users = conn.execute("SELECT name, profile_icon FROM users WHERE status='승인'").fetchall()
    user_list = sorted(list(set([u['name'] for u in db_users])))
    
    user_icons = {}
    for u in db_users:
        user_icons[u['name']] = u['profile_icon'] if 'profile_icon' in u.keys() and u['profile_icon'] else '👤'
        
    if current_user not in user_icons:
        user_icons[current_user] = '👤'
    if current_user not in user_list: 
        user_list.append(current_user)

    conn.close()

    return render_template('main.html', 
                           events=events, today_grouped=today_grouped, weekly_grouped=weekly_grouped,
                           cats=cats, today_str=today.strftime('%Y년 %m월 %d일'), holidays_dict=holidays_dict,
                           current_user=current_user, board_posts=board_posts, 
                           chat_partners=chat_partners,
                           received_messages=received_messages, sent_messages=sent_messages,
                           user_list=user_list, user_icons=user_icons)

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
    # 보안: 본인이 전송한 글(sender)만 삭제할 수 있도록 제한
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