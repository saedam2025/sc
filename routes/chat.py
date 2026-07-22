from flask import Blueprint, jsonify, session, request, render_template, current_app, send_file
from datetime import datetime, timedelta
import os
import threading
import unicodedata
import uuid
from .database import get_db, BASE_DIR

chat_bp = Blueprint('chat', __name__)
CHAT_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'chat_uploads')
LEGACY_UPLOAD_FOLDER = '/mnt/data/uploads'
CHAT_MAX_FILE_SIZE = 10 * 1024 * 1024
CHAT_RETENTION_DAYS = 30
CHAT_CLEANUP_INTERVAL = timedelta(hours=1)
UNREAD_SQL = "(is_read IN (0, '0', 'False', 'false') OR is_read IS NULL)"
_cleanup_lock = threading.Lock()
_last_cleanup_at = None


def _clean_original_filename(filename):
    """브라우저가 보낸 원본 파일명은 표시/다운로드용으로만 안전하게 보존한다."""
    name = unicodedata.normalize('NFC', str(filename or '').replace('\\', '/').split('/')[-1])
    name = ''.join(ch for ch in name if ch >= ' ' and ch != '\x7f').strip().strip('.')
    return name[:255] or '첨부파일'


def _get_file_size(file_storage):
    stream = file_storage.stream
    original_position = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(original_position)
    return size


def _save_chat_attachment(file_storage):
    original_name = _clean_original_filename(file_storage.filename)
    file_size = _get_file_size(file_storage)
    if file_size >= CHAT_MAX_FILE_SIZE:
        raise ValueError('첨부파일은 개당 10MB 미만만 업로드할 수 있습니다.')

    os.makedirs(CHAT_UPLOAD_FOLDER, exist_ok=True)
    # 원본명과 저장명을 분리해 한글명을 보존하고 동일 파일명의 덮어쓰기를 방지한다.
    while True:
        stored_path = os.path.join(CHAT_UPLOAD_FOLDER, uuid.uuid4().hex)
        try:
            descriptor = os.open(stored_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            break
        except FileExistsError:
            continue

    try:
        with os.fdopen(descriptor, 'wb') as destination:
            file_storage.stream.seek(0)
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
    except Exception:
        try:
            os.remove(stored_path)
        except OSError:
            pass
        raise

    return original_name, stored_path


def _is_allowed_chat_path(filepath):
    if not filepath:
        return False
    candidate = os.path.abspath(filepath)
    for root in (CHAT_UPLOAD_FOLDER, LEGACY_UPLOAD_FOLDER):
        try:
            if os.path.commonpath([candidate, os.path.abspath(root)]) == os.path.abspath(root):
                return True
        except ValueError:
            continue
    return False


def _get_attachment_metadata(filepath, sent_at):
    file_size = 0
    if filepath and _is_allowed_chat_path(filepath) and os.path.isfile(filepath):
        try:
            file_size = os.path.getsize(filepath)
        except OSError:
            file_size = 0

    expires_at = ''
    if sent_at:
        try:
            uploaded_at = datetime.fromisoformat(str(sent_at).replace('Z', '+00:00'))
            expires_at = (uploaded_at + timedelta(days=CHAT_RETENTION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
        except (TypeError, ValueError):
            pass
    return {"file_size": file_size, "expires_at": expires_at}


def _remove_physical_file(filepath):
    if not _is_allowed_chat_path(filepath):
        return False
    if not os.path.exists(filepath):
        return True
    if not os.path.isfile(filepath):
        return False
    try:
        os.remove(filepath)
        return True
    except OSError:
        current_app.logger.exception('메신저 첨부파일 삭제 실패: %s', filepath)
        return False


def _remove_file_if_unreferenced(conn, filepath):
    if not filepath:
        return
    remaining = conn.execute(
        "SELECT 1 FROM messages WHERE filepath=? LIMIT 1", (filepath,)
    ).fetchone()
    if not remaining:
        _remove_physical_file(filepath)


def _cleanup_expired_chat_attachments(conn):
    cutoff = (datetime.now() - timedelta(days=CHAT_RETENTION_DAYS)).strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute('''
        SELECT DISTINCT filepath
        FROM messages
        WHERE filepath IS NOT NULL AND filepath <> ''
          AND datetime(sent_at) < datetime(?)
    ''', (cutoff,)).fetchall()

    expired_paths = [row['filepath'] for row in rows if row['filepath']]
    if not expired_paths:
        return 0

    for filepath in expired_paths:
        has_active_reference = conn.execute('''
            SELECT 1 FROM messages
            WHERE filepath=? AND datetime(sent_at) >= datetime(?)
            LIMIT 1
        ''', (filepath, cutoff)).fetchone()
        file_released = bool(has_active_reference) or _remove_physical_file(filepath)
        if file_released:
            conn.execute('''
                UPDATE messages SET filename='', filepath=''
                WHERE filepath=? AND datetime(sent_at) < datetime(?)
            ''', (filepath, cutoff))
        else:
            # 사용자에게는 즉시 숨기되 filepath는 남겨 다음 주기에서 물리 삭제를 재시도한다.
            conn.execute('''
                UPDATE messages SET filename=''
                WHERE filepath=? AND datetime(sent_at) < datetime(?)
            ''', (filepath, cutoff))
    conn.commit()
    return len(expired_paths)


def _maybe_cleanup_expired_attachments(force=False):
    global _last_cleanup_at
    now = datetime.now()
    if not force and _last_cleanup_at and now - _last_cleanup_at < CHAT_CLEANUP_INTERVAL:
        return
    if not _cleanup_lock.acquire(blocking=False):
        return
    try:
        now = datetime.now()
        if not force and _last_cleanup_at and now - _last_cleanup_at < CHAT_CLEANUP_INTERVAL:
            return
        conn = get_db()
        try:
            _cleanup_expired_chat_attachments(conn)
        finally:
            conn.close()
        _last_cleanup_at = now
    finally:
        _cleanup_lock.release()


@chat_bp.before_app_request
def cleanup_expired_chat_attachments():
    # 앱이 요청을 처리하는 동안 시간당 한 번씩 30일 지난 첨부를 자동 정리한다.
    try:
        _maybe_cleanup_expired_attachments()
    except Exception:
        # 정리 작업의 일시적 실패가 일반 인트라넷 요청까지 막지 않도록 다음 주기에 재시도한다.
        current_app.logger.exception('메신저 만료 첨부파일 정리 실패')

def _ensure_chat_tables(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS pinned_chats (
        user_name TEXT,
        partner TEXT,
        pin_order INTEGER,
        PRIMARY KEY(user_name, partner)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS message_reactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_key TEXT NOT NULL,
        user_name TEXT NOT NULL,
        reaction TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(message_key, user_name)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS message_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_key TEXT NOT NULL,
        user_name TEXT NOT NULL,
        comment TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()

def _build_chat_rooms(conn, current_user):
    _ensure_chat_tables(conn)

    pinned_rows = conn.execute(
        "SELECT partner, pin_order FROM pinned_chats WHERE user_name=?",
        (current_user,)
    ).fetchall()
    pinned_by_partner = {row['partner']: int(row['pin_order']) for row in pinned_rows}

    rows = conn.execute('''
        SELECT id, sender, receiver, content, sent_at, room_id, is_read
        FROM messages
        WHERE sender=? OR receiver=?
        ORDER BY sent_at DESC, id DESC
    ''', (current_user, current_user)).fetchall()

    unread_rows = conn.execute(f'''
        SELECT CASE WHEN room_id IS NOT NULL THEN room_id ELSE sender END AS partner, COUNT(*) AS count
        FROM messages
        WHERE receiver=? AND {UNREAD_SQL}
        GROUP BY CASE WHEN room_id IS NOT NULL THEN room_id ELSE sender END
    ''', (current_user,)).fetchall()
    unread_by_partner = {row['partner']: int(row['count']) for row in unread_rows}

    rooms = {}
    for row in rows:
        room_id = row['room_id']
        partner = room_id if room_id else (row['receiver'] if row['sender'] == current_user else row['sender'])
        if not partner or partner == current_user or partner in rooms:
            continue

        pin_order = pinned_by_partner.get(partner)
        rooms[partner] = {
            'partner': partner,
            'is_group': bool(room_id),
            'last_message': row['content'] or '',
            'last_msg_time': row['sent_at'] or '',
            'last_id': int(row['id']),
            'unread_count': unread_by_partner.get(partner, 0),
            'is_pinned': pin_order is not None,
            'pin_order': pin_order if pin_order is not None else 0,
        }

    for partner, unread_count in unread_by_partner.items():
        if partner and partner not in rooms:
            pin_order = pinned_by_partner.get(partner)
            rooms[partner] = {
                'partner': partner,
                'is_group': ',' in partner,
                'last_message': '',
                'last_msg_time': '',
                'last_id': 0,
                'unread_count': unread_count,
                'is_pinned': pin_order is not None,
                'pin_order': pin_order if pin_order is not None else 0,
            }

    pinned_rooms = sorted(
        [room for room in rooms.values() if room['is_pinned']],
        key=lambda room: room['pin_order']
    )
    normal_rooms = sorted(
        [room for room in rooms.values() if not room['is_pinned']],
        key=lambda room: (room['last_msg_time'], room['last_id']),
        reverse=True
    )
    return pinned_rooms + normal_rooms

def _get_reaction_map(conn, message_ids, current_user):
    keys = [str(message_id) for message_id in message_ids if message_id is not None]
    if not keys:
        return {}

    placeholders = ','.join(['?'] * len(keys))
    rows = conn.execute(f'''
        SELECT message_key, reaction, COUNT(*) AS count,
               SUM(CASE WHEN user_name=? THEN 1 ELSE 0 END) AS mine,
               GROUP_CONCAT(user_name, ',') AS users
        FROM message_reactions
        WHERE message_key IN ({placeholders})
        GROUP BY message_key, reaction
        ORDER BY MIN(created_at) ASC
    ''', [current_user] + keys).fetchall()

    reaction_map = {}
    for row in rows:
        reaction_map.setdefault(row['message_key'], []).append({
            'reaction': row['reaction'],
            'count': int(row['count']),
            'mine': bool(row['mine']),
            'users': [name for name in (row['users'] or '').split(',') if name],
        })
    return reaction_map

def _get_comment_map(conn, message_ids):
    keys = [str(message_id) for message_id in message_ids if message_id is not None]
    if not keys:
        return {}

    placeholders = ','.join(['?'] * len(keys))
    rows = conn.execute(f'''
        SELECT message_key, user_name, comment, created_at
        FROM message_comments
        WHERE message_key IN ({placeholders})
        ORDER BY created_at ASC, id ASC
    ''', keys).fetchall()

    comment_map = {}
    for row in rows:
        comment_map.setdefault(row['message_key'], []).append({
            'user_name': row['user_name'],
            'comment': row['comment'],
            'created_at': row['created_at'],
        })
    return comment_map

def _can_access_message(msg, current_user):
    if not msg or not current_user:
        return False
    if msg['room_id']:
        members = [member.strip() for member in msg['room_id'].split(',')]
        return current_user == msg['sender'] or current_user in members
    return current_user in (msg['sender'], msg['receiver'])

@chat_bp.app_context_processor
def inject_chat_data():
    current_user = session.get('user_name')
    if not current_user:
        return {}

    conn = get_db()
    
    # 🚀 [안전망] 예전 DB 파일을 덮어쓰며 is_read 컬럼이 누락되었을 경우를 대비해 무조건 주입(에러 무시)
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN is_read INTEGER DEFAULT 0")
        conn.commit()
    except:
        pass

    conn.execute('''CREATE TABLE IF NOT EXISTS pinned_chats (
        user_name TEXT,
        partner TEXT,
        pin_order INTEGER,
        PRIMARY KEY(user_name, partner)
    )''')
    
    received_messages = conn.execute("SELECT * FROM messages WHERE receiver=? ORDER BY sent_at DESC LIMIT 50", (current_user,)).fetchall()
    sent_messages = conn.execute("SELECT * FROM messages WHERE sender=? ORDER BY sent_at DESC LIMIT 50", (current_user,)).fetchall()

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

    db_users = conn.execute("SELECT name, profile_icon FROM users WHERE status='승인' ORDER BY level ASC, id ASC").fetchall()
    user_list = []
    user_icons = {}
    for u in db_users:
        if u['name'] not in user_list: user_list.append(u['name'])
        user_icons[u['name']] = u['profile_icon'] if u['profile_icon'] else '👤'
        
    pinned_query = conn.execute("SELECT partner, pin_order FROM pinned_chats WHERE user_name=? ORDER BY pin_order ASC", (current_user,)).fetchall()
    pinned_chats = {p['partner']: p['pin_order'] for p in pinned_query}
        
    conn.close()

    return dict(
        current_user=current_user,
        widget_recv_msgs=received_messages,
        widget_sent_msgs=sent_messages,
        widget_chat_partners=chat_partners,
        chat_user_list=user_list,
        chat_user_icons=user_icons,
        widget_pinned_chats=pinned_chats
    )

@chat_bp.route('/api/unread_messages')
def api_unread_messages():
    current_user = session.get('user_name')
    if not current_user: return jsonify({"total_unread": 0, "details": {}, "rooms": []})

    conn = get_db()
    rooms = _build_chat_rooms(conn, current_user)
    conn.close()

    details = {room['partner']: room['unread_count'] for room in rooms if room['unread_count'] > 0}
    total_count = sum(details.values())
    return jsonify({"total_unread": total_count, "details": details, "rooms": rooms})

@chat_bp.route('/send_message', methods=['POST'])
def send_message():
    sender = session.get('user_name', '익명')
    receivers_str = request.form.get('receiver', '')
    content = request.form.get('content', '')
    is_group_chat = request.form.get('is_group_chat') == 'true'
    room_id_input = request.form.get('room_id')
    
    if room_id_input:
        participants = room_id_input.split(',')
        receivers = [p.strip() for p in participants if p.strip() != sender]
        room_id = room_id_input
    else:
        receivers = [r.strip() for r in receivers_str.split(',') if r.strip()]
        if is_group_chat and len(receivers) > 1:
            participants = sorted(receivers + [sender])
            room_id = ",".join(participants)
        else:
            room_id = None
    
    if not receivers:
        return jsonify({"status": "error", "message": "받는 사람을 선택해주세요."}), 400

    file = request.files.get('file')
    filename, filepath = '', ''
    if file and file.filename:
        try:
            filename, filepath = _save_chat_attachment(file)
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 413
        except OSError:
            current_app.logger.exception('메신저 첨부파일 저장 실패')
            return jsonify({"status": "error", "message": "첨부파일을 저장하지 못했습니다."}), 500

    conn = get_db()
    try:
        for rec in receivers:
            if room_id:
                conn.execute("INSERT INTO messages (sender, receiver, content, filename, filepath, room_id, is_read) VALUES (?, ?, ?, ?, ?, ?, 0)", 
                             (sender, rec, content, filename, filepath, room_id))
            else:
                conn.execute("INSERT INTO messages (sender, receiver, content, filename, filepath, is_read) VALUES (?, ?, ?, ?, ?, 0)", 
                             (sender, rec, content, filename, filepath))
        conn.commit()
    except Exception:
        conn.rollback()
        _remove_physical_file(filepath)
        current_app.logger.exception('메신저 메시지 저장 실패')
        return jsonify({"status": "error", "message": "메시지를 저장하지 못했습니다."}), 500
    finally:
        conn.close()
    return jsonify({"status": "success", "room_id": room_id, "filename": filename})


@chat_bp.route('/chat/attachment/<int:message_id>')
def chat_attachment(message_id):
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    # 다운로드 직전에는 만료 여부를 즉시 반영해 30일 지난 파일이 제공되지 않게 한다.
    _maybe_cleanup_expired_attachments(force=True)
    conn = get_db()
    try:
        msg = conn.execute('''
            SELECT id, sender, receiver, room_id, filename, filepath, sent_at
            FROM messages WHERE id=?
        ''', (message_id,)).fetchone()
        if not _can_access_message(msg, current_user):
            return jsonify({"status": "error", "message": "첨부파일 접근 권한이 없습니다."}), 403
        cutoff = datetime.now() - timedelta(days=CHAT_RETENTION_DAYS)
        try:
            sent_at = datetime.fromisoformat(str(msg['sent_at']).replace('Z', '+00:00')).replace(tzinfo=None)
        except (TypeError, ValueError):
            sent_at = None
        if sent_at and sent_at < cutoff:
            return jsonify({"status": "error", "message": "보관기간이 만료된 첨부파일입니다."}), 410
        if not msg['filepath'] or not msg['filename']:
            return jsonify({"status": "error", "message": "보관기간이 만료되었거나 삭제된 첨부파일입니다."}), 410
        filepath = msg['filepath']
        filename = _clean_original_filename(msg['filename'])
    finally:
        conn.close()

    if not _is_allowed_chat_path(filepath) or not os.path.isfile(filepath):
        return jsonify({"status": "error", "message": "첨부파일을 찾을 수 없습니다."}), 404
    return send_file(
        filepath,
        as_attachment=request.args.get('download') == '1',
        download_name=filename,
        conditional=True,
    )

@chat_bp.route('/get_chat_history/<other_user>')
def get_chat_history(other_user):
    current_user = session.get('user_name')
    conn = get_db()
    _ensure_chat_tables(conn)

    if ',' in other_user:
        room_id = other_user
        conn.execute("UPDATE messages SET is_read=1 WHERE receiver=? AND room_id=?", (current_user, room_id))
        conn.commit()

        chat = conn.execute(f'''
            SELECT MIN(id) as id, sender, room_id as receiver, content, sent_at, filename, filepath,
                   SUM(CASE WHEN {UNREAD_SQL} THEN 1 ELSE 0 END) as unread_count
            FROM messages
            WHERE room_id=?
            GROUP BY sender, content, sent_at, filename, filepath
            ORDER BY sent_at ASC, id ASC
        ''', (room_id,)).fetchall()

        message_ids = [c['id'] for c in chat]
        reaction_map = _get_reaction_map(conn, message_ids, current_user)
        comment_map = _get_comment_map(conn, message_ids)
        result = []
        for c in chat:
            key = str(c['id'])
            attachment = _get_attachment_metadata(c['filepath'], c['sent_at'])
            result.append({
                "id": c['id'],
                "sender": c['sender'],
                "receiver": c['receiver'],
                "content": c['content'],
                "sent_at": c['sent_at'],
                "unread_count": int(c['unread_count']) if c['unread_count'] else 0,
                "filename": c['filename'] if c['filename'] else '',
                "file_size": attachment['file_size'],
                "expires_at": attachment['expires_at'],
                "is_group": True,
                "reactions": reaction_map.get(key, []),
                "comments": comment_map.get(key, []),
            })
    else:
        conn.execute("UPDATE messages SET is_read=1 WHERE receiver=? AND sender=? AND room_id IS NULL", (current_user, other_user))
        conn.commit()

        chat = conn.execute(f'''
            SELECT id, sender, receiver, content, sent_at, filename, filepath,
                   CASE WHEN {UNREAD_SQL} THEN 1 ELSE 0 END as unread_count
            FROM messages
            WHERE ((sender=? AND receiver=?) OR (sender=? AND receiver=?)) AND room_id IS NULL
            ORDER BY sent_at ASC, id ASC
        ''', (current_user, other_user, other_user, current_user)).fetchall()

        message_ids = [c['id'] for c in chat]
        reaction_map = _get_reaction_map(conn, message_ids, current_user)
        comment_map = _get_comment_map(conn, message_ids)
        result = []
        for c in chat:
            key = str(c['id'])
            attachment = _get_attachment_metadata(c['filepath'], c['sent_at'])
            result.append({
                "id": c['id'],
                "sender": c['sender'],
                "receiver": c['receiver'],
                "content": c['content'],
                "sent_at": c['sent_at'],
                "unread_count": int(c['unread_count']) if c['unread_count'] else 0,
                "filename": c['filename'] if c['filename'] else '',
                "file_size": attachment['file_size'],
                "expires_at": attachment['expires_at'],
                "is_group": False,
                "reactions": reaction_map.get(key, []),
                "comments": comment_map.get(key, []),
            })

    conn.close()
    return jsonify(result)

@chat_bp.route('/api/message_reaction', methods=['POST'])
def message_reaction():
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    message_id = data.get('message_id')
    reaction = str(data.get('reaction') or '').strip()

    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if not message_id or reaction not in ['heart', 'like', 'laugh', 'love', 'clap']:
        return jsonify({"status": "error", "message": "잘못된 요청입니다."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    msg = conn.execute("SELECT id, sender, receiver, room_id FROM messages WHERE id=?", (message_id,)).fetchone()
    if not _can_access_message(msg, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "권한이 없습니다."}), 403

    message_key = str(message_id)
    existing = conn.execute(
        "SELECT reaction FROM message_reactions WHERE message_key=? AND user_name=?",
        (message_key, current_user)
    ).fetchone()

    if existing and existing['reaction'] == reaction:
        conn.execute(
            "DELETE FROM message_reactions WHERE message_key=? AND user_name=?",
            (message_key, current_user)
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO message_reactions (message_key, user_name, reaction) VALUES (?, ?, ?)",
            (message_key, current_user, reaction)
        )

    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@chat_bp.route('/api/message_comment', methods=['POST'])
def message_comment():
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    message_id = data.get('message_id')
    comment = str(data.get('comment') or '').strip()

    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if not message_id or not comment:
        return jsonify({"status": "error", "message": "댓글을 입력해주세요."}), 400
    if len(comment) > 500:
        return jsonify({"status": "error", "message": "댓글은 500자 이하로 입력해주세요."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    msg = conn.execute("SELECT id, sender, receiver, room_id FROM messages WHERE id=?", (message_id,)).fetchone()
    if not _can_access_message(msg, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "권한이 없습니다."}), 403

    conn.execute(
        "INSERT INTO message_comments (message_key, user_name, comment) VALUES (?, ?, ?)",
        (str(message_id), current_user, comment)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@chat_bp.route('/delete_message/<int:msg_id>', methods=['DELETE'])
def delete_message(msg_id):
    current_user = session.get('user_name')
    conn = get_db()
    _ensure_chat_tables(conn)
    msg = conn.execute("SELECT room_id, content, sent_at, filepath FROM messages WHERE id=? AND sender=?", (msg_id, current_user)).fetchone()
    if msg:
        filepaths = []
        if msg['room_id']:
            file_rows = conn.execute(
                "SELECT DISTINCT filepath FROM messages WHERE room_id=? AND sender=? AND content=? AND sent_at=? AND COALESCE(filepath, '')=COALESCE(?, '')",
                (msg['room_id'], current_user, msg['content'], msg['sent_at'], msg['filepath'])
            ).fetchall()
            filepaths = [row['filepath'] for row in file_rows if row['filepath']]
            conn.execute(
                "DELETE FROM messages WHERE room_id=? AND sender=? AND content=? AND sent_at=? AND COALESCE(filepath, '')=COALESCE(?, '')",
                (msg['room_id'], current_user, msg['content'], msg['sent_at'], msg['filepath'])
            )
        else:
            if msg['filepath']:
                filepaths = [msg['filepath']]
            conn.execute("DELETE FROM messages WHERE id=? AND sender=?", (msg_id, current_user))
        conn.execute("DELETE FROM message_reactions WHERE message_key=?", (str(msg_id),))
        conn.execute("DELETE FROM message_comments WHERE message_key=?", (str(msg_id),))
        conn.commit()
        for filepath in filepaths:
            _remove_file_if_unreferenced(conn, filepath)
    conn.close()
    return jsonify({"status": "success"})

@chat_bp.route('/api/leave_chat', methods=['POST'])
def leave_chat():
    current_user = session.get('user_name')
    partner = request.json.get('partner')
    conn = get_db()
    
    if ',' in partner:
        files = conn.execute("SELECT filepath FROM messages WHERE room_id=? AND sender=?", (partner, current_user)).fetchall()
        conn.execute("DELETE FROM messages WHERE room_id=? AND sender=?", (partner, current_user))
    else:
        files = conn.execute("SELECT filepath FROM messages WHERE ((sender=? AND receiver=?) OR (sender=? AND receiver=?)) AND room_id IS NULL", (current_user, partner, partner, current_user)).fetchall()
        conn.execute("DELETE FROM messages WHERE ((sender=? AND receiver=?) OR (sender=? AND receiver=?)) AND room_id IS NULL", (current_user, partner, partner, current_user))
        
    conn.commit()
    for filepath in {f['filepath'] for f in files if f['filepath']}:
        _remove_file_if_unreferenced(conn, filepath)
    conn.close()
                
    return jsonify({"status": "success"})

@chat_bp.route('/api/toggle_pin', methods=['POST'])
def toggle_pin():
    current_user = session.get('user_name')
    partner = request.json.get('partner')
    conn = get_db()
    _ensure_chat_tables(conn)
    
    existing = conn.execute("SELECT * FROM pinned_chats WHERE user_name=? AND partner=?", (current_user, partner)).fetchone()
    if existing:
        conn.execute("DELETE FROM pinned_chats WHERE user_name=? AND partner=?", (current_user, partner))
    else:
        max_row = conn.execute("SELECT MAX(pin_order) as max_order FROM pinned_chats WHERE user_name=?", (current_user,)).fetchone()
        next_order = 1 if max_row['max_order'] is None else max_row['max_order'] + 1
        conn.execute("INSERT INTO pinned_chats (user_name, partner, pin_order) VALUES (?, ?, ?)", (current_user, partner, next_order))
        
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@chat_bp.route('/api/move_pin', methods=['POST'])
def move_pin():
    current_user = session.get('user_name')
    partner = request.json.get('partner')
    direction = request.json.get('direction')
    
    conn = get_db()
    _ensure_chat_tables(conn)
    current_pin = conn.execute("SELECT pin_order FROM pinned_chats WHERE user_name=? AND partner=?", (current_user, partner)).fetchone()
    
    if current_pin:
        current_order = current_pin['pin_order']
        if direction == 'up':
            swap_pin = conn.execute("SELECT partner, pin_order FROM pinned_chats WHERE user_name=? AND pin_order < ? ORDER BY pin_order DESC LIMIT 1", (current_user, current_order)).fetchone()
        else:
            swap_pin = conn.execute("SELECT partner, pin_order FROM pinned_chats WHERE user_name=? AND pin_order > ? ORDER BY pin_order ASC LIMIT 1", (current_user, current_order)).fetchone()
            
        if swap_pin:
            conn.execute("UPDATE pinned_chats SET pin_order=? WHERE user_name=? AND partner=?", (swap_pin['pin_order'], current_user, partner))
            conn.execute("UPDATE pinned_chats SET pin_order=? WHERE user_name=? AND partner=?", (current_order, current_user, swap_pin['partner']))
            conn.commit()
            
    conn.close()
    return jsonify({"status": "success"})
