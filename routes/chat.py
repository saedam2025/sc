from flask import Blueprint, jsonify, session, request, render_template, current_app, send_file
from datetime import datetime, timedelta
from flask_socketio import join_room, leave_room
import os
import threading
import unicodedata
import uuid
from .database import get_db, BASE_DIR
from extensions import socketio

chat_bp = Blueprint('chat', __name__)
CHAT_UPLOAD_FOLDER = os.path.join(BASE_DIR, 'chat_uploads')
LEGACY_UPLOAD_FOLDER = '/mnt/data/uploads'
CHAT_MAX_FILE_SIZE = 10 * 1024 * 1024
CHAT_RETENTION_DAYS = 30
CHAT_CLEANUP_INTERVAL = timedelta(hours=1)
UNREAD_SQL = "(is_read IN (0, '0', 'False', 'false') OR is_read IS NULL)"
_cleanup_lock = threading.Lock()
_last_cleanup_at = None
_chat_schema_lock = threading.Lock()
_chat_schema_ready = set()


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

def _ensure_chat_tables_impl(conn):
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
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_room_profiles (
        room_key TEXT PRIMARY KEY,
        display_name TEXT,
        created_by TEXT NOT NULL,
        admin_user TEXT NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_room_members (
        room_key TEXT NOT NULL,
        user_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'member',
        joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        joined_after_id INTEGER NOT NULL DEFAULT 0,
        left_at DATETIME,
        PRIMARY KEY(room_key, user_name)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_user_room_settings (
        user_name TEXT NOT NULL,
        room_key TEXT NOT NULL,
        hidden_before_id INTEGER NOT NULL DEFAULT 0,
        notifications_muted INTEGER NOT NULL DEFAULT 0,
        left_at DATETIME,
        PRIMARY KEY(user_name, room_key)
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS message_hidden_users (
        message_uid TEXT NOT NULL,
        user_name TEXT NOT NULL,
        hidden_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(message_uid, user_name)
    )''')

    message_columns = {
        row['name'] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    message_migrations = {
        'message_uid': "ALTER TABLE messages ADD COLUMN message_uid TEXT",
        'reply_to_uid': "ALTER TABLE messages ADD COLUMN reply_to_uid TEXT",
        'edited_at': "ALTER TABLE messages ADD COLUMN edited_at DATETIME",
        'deleted_for_all': "ALTER TABLE messages ADD COLUMN deleted_for_all INTEGER DEFAULT 0",
        'deleted_at': "ALTER TABLE messages ADD COLUMN deleted_at DATETIME",
    }
    for column, statement in message_migrations.items():
        if column not in message_columns:
            conn.execute(statement)

    # 기존 그룹 메시지는 수신자별 중복 행을 하나의 논리 메시지로 묶는다.
    legacy_rows = conn.execute('''
        SELECT id, room_id, sender, content, sent_at, COALESCE(filepath, '') AS filepath
        FROM messages
        WHERE message_uid IS NULL OR message_uid=''
        ORDER BY id ASC
    ''').fetchall()
    legacy_group_uids = {}
    for row in legacy_rows:
        if row['room_id']:
            key = (
                row['room_id'], row['sender'], row['content'] or '',
                row['sent_at'] or '', row['filepath'] or ''
            )
            message_uid = legacy_group_uids.setdefault(key, f"legacy-group-{row['id']}")
        else:
            message_uid = f"legacy-{row['id']}"
        conn.execute(
            "UPDATE messages SET message_uid=? WHERE id=?",
            (message_uid, row['id'])
        )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_uid ON messages(message_uid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_room_id_id ON messages(room_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_direct ON messages(sender, receiver, room_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_room_members_user ON chat_room_members(user_name, left_at)")
    conn.commit()


def _ensure_chat_tables(conn):
    database_row = conn.execute("PRAGMA database_list").fetchone()
    try:
        database_file = database_row['file']
    except (TypeError, KeyError, IndexError):
        database_file = database_row[2] if database_row else ''
    database_key = database_file or f"memory:{id(conn)}"
    if database_key in _chat_schema_ready:
        return
    with _chat_schema_lock:
        if database_key in _chat_schema_ready:
            return
        _ensure_chat_tables_impl(conn)
        _chat_schema_ready.add(database_key)


def _approved_user_names(conn):
    return {
        row['name'] for row in conn.execute(
            "SELECT name FROM users WHERE status='승인' AND name IS NOT NULL"
        ).fetchall()
    }


def _max_room_message_id(conn, room_key, current_user=None):
    if ',' in str(room_key):
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS max_id FROM messages WHERE room_id=?",
            (room_key,)
        ).fetchone()
    elif current_user:
        row = conn.execute('''
            SELECT COALESCE(MAX(id), 0) AS max_id
            FROM messages
            WHERE room_id IS NULL
              AND ((sender=? AND receiver=?) OR (sender=? AND receiver=?))
        ''', (current_user, room_key, room_key, current_user)).fetchone()
    else:
        return 0
    return int(row['max_id'] or 0)


def _ensure_group_room(conn, room_key, created_by=None):
    room_key = str(room_key or '').strip()
    if not room_key or ',' not in room_key:
        return None

    profile = conn.execute(
        "SELECT * FROM chat_room_profiles WHERE room_key=?",
        (room_key,)
    ).fetchone()
    if profile:
        return profile

    legacy_members = [name.strip() for name in room_key.split(',') if name.strip()]
    first_message = conn.execute('''
        SELECT sender FROM messages
        WHERE room_id=?
        ORDER BY id ASC LIMIT 1
    ''', (room_key,)).fetchone()
    creator = (
        str(created_by or '').strip()
        or (first_message['sender'] if first_message else '')
        or (legacy_members[0] if legacy_members else '')
    )
    if not creator:
        return None

    conn.execute('''
        INSERT OR IGNORE INTO chat_room_profiles
            (room_key, display_name, created_by, admin_user)
        VALUES (?, NULL, ?, ?)
    ''', (room_key, creator, creator))
    for member in dict.fromkeys(legacy_members + [creator]):
        conn.execute('''
            INSERT OR IGNORE INTO chat_room_members
                (room_key, user_name, role, joined_after_id)
            VALUES (?, ?, ?, 0)
        ''', (room_key, member, 'admin' if member == creator else 'member'))
    return conn.execute(
        "SELECT * FROM chat_room_profiles WHERE room_key=?",
        (room_key,)
    ).fetchone()


def _active_group_members(conn, room_key):
    _ensure_group_room(conn, room_key)
    return conn.execute('''
        SELECT user_name, role, joined_at, joined_after_id
        FROM chat_room_members
        WHERE room_key=? AND left_at IS NULL
        ORDER BY CASE role WHEN 'admin' THEN 0 ELSE 1 END, joined_at ASC, user_name ASC
    ''', (room_key,)).fetchall()


def _room_setting(conn, current_user, room_key):
    row = conn.execute('''
        SELECT hidden_before_id, notifications_muted, left_at
        FROM chat_user_room_settings
        WHERE user_name=? AND room_key=?
    ''', (current_user, room_key)).fetchone()
    if row:
        return row
    return {
        'hidden_before_id': 0,
        'notifications_muted': 0,
        'left_at': None,
    }


def _group_member(conn, room_key, current_user):
    _ensure_group_room(conn, room_key)
    return conn.execute('''
        SELECT user_name, role, joined_after_id, left_at
        FROM chat_room_members
        WHERE room_key=? AND user_name=?
    ''', (room_key, current_user)).fetchone()


def _can_access_room(conn, room_key, current_user):
    if not current_user or not room_key:
        return False
    if ',' in str(room_key):
        member = _group_member(conn, room_key, current_user)
        return bool(member and not member['left_at'])
    if room_key == current_user:
        return True
    approved = _approved_user_names(conn)
    if room_key in approved:
        return True
    return bool(conn.execute('''
        SELECT 1 FROM messages
        WHERE room_id IS NULL
          AND ((sender=? AND receiver=?) OR (sender=? AND receiver=?))
        LIMIT 1
    ''', (current_user, room_key, room_key, current_user)).fetchone())


def _room_info(conn, room_key, current_user):
    setting = _room_setting(conn, current_user, room_key)
    if ',' not in str(room_key):
        return {
            'room_key': room_key,
            'is_group': False,
            'display_name': '나와의 채팅방' if room_key == current_user else room_key,
            'admin_user': None,
            'is_admin': False,
            'members': [current_user] if room_key == current_user else [current_user, room_key],
            'notifications_muted': bool(setting['notifications_muted']),
        }

    profile = _ensure_group_room(conn, room_key, current_user)
    members = _active_group_members(conn, room_key)
    member_names = [row['user_name'] for row in members]
    default_name = ', '.join(name for name in member_names if name != current_user)
    if len([name for name in member_names if name != current_user]) > 2:
        others = [name for name in member_names if name != current_user]
        default_name = f"{', '.join(others[:2])} 외 {len(others) - 2}명"
    return {
        'room_key': room_key,
        'is_group': True,
        'display_name': (profile['display_name'] if profile else None) or default_name or '그룹채팅',
        'admin_user': profile['admin_user'] if profile else None,
        'is_admin': bool(profile and profile['admin_user'] == current_user),
        'members': [
            {
                'name': row['user_name'],
                'role': row['role'],
            }
            for row in members
        ],
        'member_count': len(members),
        'notifications_muted': bool(setting['notifications_muted']),
    }

def _build_chat_rooms(conn, current_user):
    _ensure_chat_tables(conn)

    pinned_rows = conn.execute(
        "SELECT partner, pin_order FROM pinned_chats WHERE user_name=?",
        (current_user,)
    ).fetchall()
    pinned_by_partner = {row['partner']: int(row['pin_order']) for row in pinned_rows}

    settings = {
        row['room_key']: row
        for row in conn.execute('''
            SELECT room_key, hidden_before_id, notifications_muted, left_at
            FROM chat_user_room_settings
            WHERE user_name=?
        ''', (current_user,)).fetchall()
    }
    hidden_uids = {
        row['message_uid'] for row in conn.execute(
            "SELECT message_uid FROM message_hidden_users WHERE user_name=?",
            (current_user,)
        ).fetchall()
    }
    rows = conn.execute('''
        SELECT id, message_uid, sender, receiver, content, sent_at, room_id,
               is_read, filename, deleted_for_all
        FROM messages
        WHERE sender=? OR receiver=?
        ORDER BY id DESC
    ''', (current_user, current_user)).fetchall()

    rooms = {}
    unread_by_partner = {}
    seen_logical = set()
    group_member_cache = {}
    for row in rows:
        room_id = row['room_id']
        partner = room_id if room_id else (row['receiver'] if row['sender'] == current_user else row['sender'])
        if not partner:
            continue
        setting = settings.get(partner) or {
            'hidden_before_id': 0,
            'notifications_muted': 0,
            'left_at': None,
        }
        cutoff_id = int(setting['hidden_before_id'] or 0)

        if room_id:
            if partner not in group_member_cache:
                group_member_cache[partner] = _group_member(conn, partner, current_user)
            member = group_member_cache[partner]
            if not member or member['left_at']:
                continue
            cutoff_id = max(cutoff_id, int(member['joined_after_id'] or 0))

        if int(row['id']) <= cutoff_id or row['message_uid'] in hidden_uids:
            continue

        if (
            row['receiver'] == current_user
            and not row['deleted_for_all']
            and (
                row['is_read'] in (0, '0', 'False', 'false')
                or row['is_read'] is None
            )
        ):
            unread_by_partner[partner] = unread_by_partner.get(partner, 0) + 1

        logical_key = (partner, row['message_uid'] or f"id:{row['id']}")
        if logical_key in seen_logical:
            continue
        seen_logical.add(logical_key)
        if partner in rooms:
            continue

        pin_order = pinned_by_partner.get(partner)
        last_message = '삭제된 메시지입니다.' if row['deleted_for_all'] else (row['content'] or '')
        if not last_message and row['filename']:
            last_message = f"📎 {row['filename']}"
        rooms[partner] = {
            'partner': partner,
            'is_group': bool(room_id),
            'last_message': last_message,
            'last_msg_time': row['sent_at'] or '',
            'last_id': int(row['id']),
            'unread_count': 0,
            'is_pinned': pin_order is not None,
            'pin_order': pin_order if pin_order is not None else 0,
            'notifications_muted': bool(setting['notifications_muted']),
        }

    # 초대 직후 아직 메시지가 없는 그룹도 목록에 표시한다.
    active_groups = conn.execute('''
        SELECT room_key FROM chat_room_members
        WHERE user_name=? AND left_at IS NULL
    ''', (current_user,)).fetchall()
    for member_row in active_groups:
        partner = member_row['room_key']
        if partner in rooms:
            continue
        setting = settings.get(partner) or {
            'notifications_muted': 0,
        }
        pin_order = pinned_by_partner.get(partner)
        rooms[partner] = {
            'partner': partner,
            'is_group': True,
            'last_message': '',
            'last_msg_time': '',
            'last_id': 0,
            'unread_count': 0,
            'is_pinned': pin_order is not None,
            'pin_order': pin_order if pin_order is not None else 0,
            'notifications_muted': bool(setting['notifications_muted']),
        }

    for partner, room in rooms.items():
        room['unread_count'] = unread_by_partner.get(partner, 0)
        info = _room_info(conn, partner, current_user)
        room['display_name'] = info['display_name']
        room['member_count'] = info.get('member_count', 2 if partner != current_user else 1)

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

def _message_access_cutoff(conn, room_key, current_user):
    setting = _room_setting(conn, current_user, room_key)
    cutoff_id = int(setting['hidden_before_id'] or 0)
    if ',' in str(room_key):
        member = _group_member(conn, room_key, current_user)
        if not member or member['left_at']:
            return None
        cutoff_id = max(cutoff_id, int(member['joined_after_id'] or 0))
    return cutoff_id


def _can_access_message(conn, msg, current_user, include_hidden=False):
    if not msg or not current_user:
        return False
    if msg['room_id']:
        cutoff_id = _message_access_cutoff(conn, msg['room_id'], current_user)
        if cutoff_id is None or int(msg['id']) <= cutoff_id:
            return False
    else:
        if current_user not in (msg['sender'], msg['receiver']):
            return False
        partner = msg['receiver'] if msg['sender'] == current_user else msg['sender']
        cutoff_id = _message_access_cutoff(conn, partner, current_user) or 0
        if int(msg['id']) <= cutoff_id:
            return False

    if not include_hidden and msg['message_uid']:
        hidden = conn.execute('''
            SELECT 1 FROM message_hidden_users
            WHERE message_uid=? AND user_name=?
        ''', (msg['message_uid'], current_user)).fetchone()
        if hidden:
            return False
    return True


def _logical_message_cte(conn, room_key, current_user):
    cutoff_id = _message_access_cutoff(conn, room_key, current_user)
    if cutoff_id is None:
        return None, []

    if ',' in str(room_key):
        logical_sql = f'''
            SELECT MIN(m.id) AS id,
                   m.message_uid,
                   m.sender,
                   m.room_id AS receiver,
                   m.room_id,
                   MAX(m.content) AS content,
                   MAX(m.sent_at) AS sent_at,
                   MAX(m.filename) AS filename,
                   MAX(m.filepath) AS filepath,
                   SUM(CASE WHEN {UNREAD_SQL} THEN 1 ELSE 0 END) AS unread_count,
                   MAX(m.reply_to_uid) AS reply_to_uid,
                   MAX(m.edited_at) AS edited_at,
                   MAX(COALESCE(m.deleted_for_all, 0)) AS deleted_for_all,
                   MAX(m.deleted_at) AS deleted_at
            FROM messages m
            WHERE m.room_id=? AND m.id>?
              AND NOT EXISTS (
                  SELECT 1 FROM message_hidden_users h
                  WHERE h.message_uid=m.message_uid AND h.user_name=?
              )
            GROUP BY m.message_uid
        '''
        return logical_sql, [room_key, cutoff_id, current_user]

    logical_sql = f'''
        SELECT m.id,
               m.message_uid,
               m.sender,
               m.receiver,
               m.room_id,
               m.content,
               m.sent_at,
               m.filename,
               m.filepath,
               CASE WHEN {UNREAD_SQL} THEN 1 ELSE 0 END AS unread_count,
               m.reply_to_uid,
               m.edited_at,
               COALESCE(m.deleted_for_all, 0) AS deleted_for_all,
               m.deleted_at
        FROM messages m
        WHERE m.room_id IS NULL AND m.id>?
          AND ((m.sender=? AND m.receiver=?) OR (m.sender=? AND m.receiver=?))
          AND NOT EXISTS (
              SELECT 1 FROM message_hidden_users h
              WHERE h.message_uid=m.message_uid AND h.user_name=?
          )
    '''
    return logical_sql, [
        cutoff_id, current_user, room_key, room_key, current_user, current_user
    ]


def _fetch_logical_messages(
    conn,
    room_key,
    current_user,
    *,
    limit=50,
    before_id=None,
    after_id=None,
    around_id=None,
    search_query=None,
):
    logical_sql, params = _logical_message_cte(conn, room_key, current_user)
    if not logical_sql:
        return [], False

    sql = f"WITH logical AS ({logical_sql}) SELECT * FROM logical WHERE 1=1"
    outer_params = list(params)
    if search_query:
        sql += " AND (COALESCE(content, '') LIKE ? OR COALESCE(filename, '') LIKE ?)"
        pattern = f"%{search_query}%"
        outer_params.extend([pattern, pattern])

    fetch_limit = max(1, min(int(limit or 50), 100))
    if around_id:
        sql += " ORDER BY ABS(id - ?) ASC, id DESC LIMIT ?"
        outer_params.extend([int(around_id), fetch_limit])
        rows = conn.execute(sql, outer_params).fetchall()
        return sorted(rows, key=lambda row: int(row['id'])), False

    if after_id:
        sql += " AND id>? ORDER BY id ASC LIMIT ?"
        outer_params.extend([int(after_id), fetch_limit])
        return conn.execute(sql, outer_params).fetchall(), False

    if before_id:
        sql += " AND id<?"
        outer_params.append(int(before_id))
    sql += " ORDER BY id DESC LIMIT ?"
    outer_params.append(fetch_limit + 1)
    rows = conn.execute(sql, outer_params).fetchall()
    has_more = len(rows) > fetch_limit
    rows = rows[:fetch_limit]
    return list(reversed(rows)), has_more


def _serialize_messages(conn, rows, current_user):
    message_ids = [row['id'] for row in rows]
    reaction_map = _get_reaction_map(conn, message_ids, current_user)
    comment_map = _get_comment_map(conn, message_ids)
    reply_uids = list({
        row['reply_to_uid'] for row in rows if row['reply_to_uid']
    })
    reply_map = {}
    if reply_uids:
        placeholders = ','.join(['?'] * len(reply_uids))
        reply_rows = conn.execute(f'''
            SELECT message_uid, MIN(id) AS id, sender,
                   MAX(content) AS content, MAX(filename) AS filename,
                   MAX(COALESCE(deleted_for_all, 0)) AS deleted_for_all
            FROM messages
            WHERE message_uid IN ({placeholders})
            GROUP BY message_uid
        ''', reply_uids).fetchall()
        for reply in reply_rows:
            reply_map[reply['message_uid']] = {
                'id': reply['id'],
                'sender': reply['sender'],
                'content': '삭제된 메시지입니다.' if reply['deleted_for_all'] else (reply['content'] or ''),
                'filename': '' if reply['deleted_for_all'] else (reply['filename'] or ''),
                'is_deleted': bool(reply['deleted_for_all']),
            }

    result = []
    for row in rows:
        key = str(row['id'])
        is_deleted = bool(row['deleted_for_all'])
        attachment = (
            {'file_size': 0, 'expires_at': ''}
            if is_deleted
            else _get_attachment_metadata(row['filepath'], row['sent_at'])
        )
        result.append({
            'id': row['id'],
            'message_uid': row['message_uid'],
            'sender': row['sender'],
            'receiver': row['receiver'],
            'content': '' if is_deleted else (row['content'] or ''),
            'sent_at': row['sent_at'],
            'unread_count': int(row['unread_count'] or 0),
            'filename': '' if is_deleted else (row['filename'] or ''),
            'file_size': attachment['file_size'],
            'expires_at': attachment['expires_at'],
            'is_group': bool(row['room_id']),
            'is_deleted': is_deleted,
            'edited_at': row['edited_at'] or '',
            'reply': reply_map.get(row['reply_to_uid']),
            'reactions': reaction_map.get(key, []),
            'comments': comment_map.get(key, []),
        })
    return result


def _emit_chat_event(conn, room_key, actor, event_type, **payload):
    if ',' in str(room_key):
        targets = [row['user_name'] for row in _active_group_members(conn, room_key)]
    else:
        targets = list(dict.fromkeys([actor, room_key]))

    for target in targets:
        target_partner = room_key if ',' in str(room_key) else (
            room_key if target == actor else actor
        )
        socketio.emit(
            'chat_event',
            {
                'type': event_type,
                'partner': target_partner,
                'actor': actor,
                **payload,
            },
            to=f"user:{target}",
            namespace='/chat',
        )

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
    sender = session.get('user_name')
    if not sender:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    receivers_str = request.form.get('receiver', '')
    content = str(request.form.get('content', ''))
    if len(content) > 5000:
        return jsonify({"status": "error", "message": "메시지는 5,000자 이하로 입력해주세요."}), 400
    is_group_chat = request.form.get('is_group_chat') == 'true'
    room_id_input = str(request.form.get('room_id') or '').strip()
    reply_to_id = request.form.get('reply_to_id')

    conn = get_db()
    _ensure_chat_tables(conn)
    approved_users = _approved_user_names(conn)
    room_id = None
    if room_id_input:
        participants = list(dict.fromkeys(
            p.strip() for p in room_id_input.split(',') if p.strip()
        ))
        if sender not in participants and not conn.execute(
            "SELECT 1 FROM chat_room_profiles WHERE room_key=?",
            (room_id_input,)
        ).fetchone():
            conn.close()
            return jsonify({"status": "error", "message": "그룹방 참여자가 아닙니다."}), 403
        invalid = [name for name in participants if name not in approved_users and name != sender]
        if invalid:
            conn.close()
            return jsonify({"status": "error", "message": "승인되지 않은 사용자가 포함되어 있습니다."}), 400
        _ensure_group_room(conn, room_id_input, sender)
        if not _can_access_room(conn, room_id_input, sender):
            conn.close()
            return jsonify({"status": "error", "message": "그룹방 참여자가 아닙니다."}), 403
        room_id = room_id_input
        receivers = [
            row['user_name'] for row in _active_group_members(conn, room_id)
            if row['user_name'] != sender
        ]
    else:
        receivers = list(dict.fromkeys(
            r.strip() for r in receivers_str.split(',') if r.strip()
        ))
        invalid = [name for name in receivers if name not in approved_users and name != sender]
        if invalid:
            conn.close()
            return jsonify({"status": "error", "message": "승인되지 않은 수신자입니다."}), 400
        if is_group_chat and len(receivers) > 1:
            participants = sorted(set(receivers + [sender]))
            room_id = ",".join(participants)
            _ensure_group_room(conn, room_id, sender)
            receivers = [
                row['user_name'] for row in _active_group_members(conn, room_id)
                if row['user_name'] != sender
            ]

    if not receivers:
        conn.close()
        return jsonify({"status": "error", "message": "받는 사람을 선택해주세요."}), 400
    if not content.strip() and not (request.files.get('file') and request.files['file'].filename):
        conn.close()
        return jsonify({"status": "error", "message": "메시지나 첨부파일을 입력해주세요."}), 400

    reply_to_uid = None
    if reply_to_id:
        try:
            reply_id = int(reply_to_id)
        except (TypeError, ValueError):
            conn.close()
            return jsonify({"status": "error", "message": "답장 대상이 올바르지 않습니다."}), 400
        reply_msg = conn.execute('''
            SELECT id, message_uid, sender, receiver, room_id
            FROM messages WHERE id=?
        ''', (reply_id,)).fetchone()
        if not _can_access_message(conn, reply_msg, sender):
            conn.close()
            return jsonify({"status": "error", "message": "답장 대상 메시지에 접근할 수 없습니다."}), 403
        if room_id:
            same_room = reply_msg['room_id'] == room_id
        else:
            reply_partner = (
                reply_msg['receiver'] if reply_msg['sender'] == sender
                else reply_msg['sender']
            )
            same_room = not reply_msg['room_id'] and len(receivers) == 1 and reply_partner == receivers[0]
        if not same_room:
            conn.close()
            return jsonify({"status": "error", "message": "다른 대화방의 메시지에는 답장할 수 없습니다."}), 400
        reply_to_uid = reply_msg['message_uid']

    file = request.files.get('file')
    filename, filepath = '', ''
    if file and file.filename:
        try:
            filename, filepath = _save_chat_attachment(file)
        except ValueError as exc:
            conn.close()
            return jsonify({"status": "error", "message": str(exc)}), 413
        except OSError:
            conn.close()
            current_app.logger.exception('메신저 첨부파일 저장 실패')
            return jsonify({"status": "error", "message": "첨부파일을 저장하지 못했습니다."}), 500

    first_message_id = None
    emitted_rooms = []
    try:
        group_message_uid = uuid.uuid4().hex if room_id else None
        for receiver in receivers:
            message_uid = group_message_uid or uuid.uuid4().hex
            cursor = conn.execute('''
                INSERT INTO messages
                    (sender, receiver, content, filename, filepath, room_id,
                     is_read, message_uid, reply_to_uid)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            ''', (
                sender, receiver, content, filename, filepath, room_id,
                message_uid, reply_to_uid
            ))
            if first_message_id is None:
                first_message_id = int(cursor.lastrowid)

            if not room_id:
                for user_name, room_key in ((sender, receiver), (receiver, sender)):
                    conn.execute('''
                        INSERT OR IGNORE INTO chat_user_room_settings
                            (user_name, room_key)
                        VALUES (?, ?)
                    ''', (user_name, room_key))
                    conn.execute('''
                        UPDATE chat_user_room_settings
                        SET left_at=NULL
                        WHERE user_name=? AND room_key=?
                    ''', (user_name, room_key))
                emitted_rooms.append((receiver, int(cursor.lastrowid)))
        conn.commit()
    except Exception:
        conn.rollback()
        _remove_physical_file(filepath)
        conn.close()
        current_app.logger.exception('메신저 메시지 저장 실패')
        return jsonify({"status": "error", "message": "메시지를 저장하지 못했습니다."}), 500

    if room_id:
        _emit_chat_event(
            conn, room_id, sender, 'message',
            message_id=first_message_id,
            content=content[:120],
            filename=filename,
        )
    else:
        for partner, message_id in emitted_rooms:
            _emit_chat_event(
                conn, partner, sender, 'message',
                message_id=message_id,
                content=content[:120],
                filename=filename,
            )
    conn.close()
    return jsonify({
        "status": "success",
        "room_id": room_id,
        "filename": filename,
        "message_id": first_message_id,
    })


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
            SELECT id, message_uid, sender, receiver, room_id, filename, filepath,
                   sent_at, deleted_for_all
            FROM messages WHERE id=?
        ''', (message_id,)).fetchone()
        if not _can_access_message(conn, msg, current_user):
            return jsonify({"status": "error", "message": "첨부파일 접근 권한이 없습니다."}), 403
        if msg['deleted_for_all']:
            return jsonify({"status": "error", "message": "삭제된 첨부파일입니다."}), 410
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
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _can_access_room(conn, other_user, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "대화방 접근 권한이 없습니다."}), 403

    def _optional_int(name):
        raw = request.args.get(name)
        if raw in (None, ''):
            return None
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return None

    limit = _optional_int('limit') or 50
    before_id = _optional_int('before_id')
    after_id = _optional_int('after_id')
    around_id = _optional_int('around_id')
    cutoff_id = _message_access_cutoff(conn, other_user, current_user) or 0

    if around_id:
        around_msg = conn.execute('''
            SELECT id, message_uid, sender, receiver, room_id
            FROM messages WHERE id=?
        ''', (around_id,)).fetchone()
        if not _can_access_message(conn, around_msg, current_user):
            conn.close()
            return jsonify({"status": "error", "message": "메시지에 접근할 수 없습니다."}), 403
        around_partner = around_msg['room_id'] or (
            around_msg['receiver'] if around_msg['sender'] == current_user
            else around_msg['sender']
        )
        if around_partner != other_user:
            conn.close()
            return jsonify({"status": "error", "message": "다른 대화방의 메시지입니다."}), 400

    if ',' in other_user:
        conn.execute('''
            UPDATE messages SET is_read=1
            WHERE receiver=? AND room_id=? AND id>?
        ''', (current_user, other_user, cutoff_id))
    else:
        conn.execute('''
            UPDATE messages SET is_read=1
            WHERE receiver=? AND sender=? AND room_id IS NULL AND id>?
        ''', (current_user, other_user, cutoff_id))
    conn.commit()

    rows, has_more = _fetch_logical_messages(
        conn,
        other_user,
        current_user,
        limit=limit,
        before_id=before_id,
        after_id=after_id,
        around_id=around_id,
    )
    result = _serialize_messages(conn, rows, current_user)
    room_info = _room_info(conn, other_user, current_user)
    conn.close()
    return jsonify({
        "status": "success",
        "messages": result,
        "has_more": has_more,
        "oldest_id": result[0]['id'] if result else None,
        "last_id": result[-1]['id'] if result else (after_id or 0),
        "room": room_info,
    })


@chat_bp.route('/api/chat/search')
def search_chat_messages():
    current_user = session.get('user_name')
    room_key = str(request.args.get('partner') or '').strip()
    query = str(request.args.get('q') or '').strip()
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if not room_key or not query:
        return jsonify({"status": "success", "results": []})
    if len(query) > 100:
        return jsonify({"status": "error", "message": "검색어는 100자 이하로 입력해주세요."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _can_access_room(conn, room_key, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "대화방 접근 권한이 없습니다."}), 403
    rows, _ = _fetch_logical_messages(
        conn,
        room_key,
        current_user,
        limit=50,
        search_query=query,
    )
    messages = list(reversed(_serialize_messages(conn, rows, current_user)))
    results = [
        {
            'id': message['id'],
            'sender': message['sender'],
            'content': message['content'],
            'filename': message['filename'],
            'sent_at': message['sent_at'],
        }
        for message in messages
        if not message['is_deleted']
    ]
    conn.close()
    return jsonify({"status": "success", "results": results})


@chat_bp.route('/api/chat/room')
def get_chat_room_info():
    current_user = session.get('user_name')
    room_key = str(request.args.get('partner') or '').strip()
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    conn = get_db()
    _ensure_chat_tables(conn)
    if not _can_access_room(conn, room_key, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "대화방 접근 권한이 없습니다."}), 403
    info = _room_info(conn, room_key, current_user)
    conn.close()
    return jsonify({"status": "success", "room": info})

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
    msg = conn.execute(
        "SELECT id, message_uid, sender, receiver, room_id FROM messages WHERE id=?",
        (message_id,)
    ).fetchone()
    if not _can_access_message(conn, msg, current_user):
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
    partner = msg['room_id'] or (
        msg['receiver'] if msg['sender'] == current_user else msg['sender']
    )
    _emit_chat_event(conn, partner, current_user, 'message_changed', message_id=int(message_id))
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
    msg = conn.execute(
        "SELECT id, message_uid, sender, receiver, room_id FROM messages WHERE id=?",
        (message_id,)
    ).fetchone()
    if not _can_access_message(conn, msg, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "권한이 없습니다."}), 403

    conn.execute(
        "INSERT INTO message_comments (message_key, user_name, comment) VALUES (?, ?, ?)",
        (str(message_id), current_user, comment)
    )
    conn.commit()
    partner = msg['room_id'] or (
        msg['receiver'] if msg['sender'] == current_user else msg['sender']
    )
    _emit_chat_event(conn, partner, current_user, 'message_changed', message_id=int(message_id))
    conn.close()
    return jsonify({"status": "success"})

@chat_bp.route('/delete_message/<int:msg_id>', methods=['DELETE'])
def delete_message(msg_id):
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    mode = str(request.args.get('mode') or 'all').strip().lower()
    if mode not in {'me', 'all'}:
        return jsonify({"status": "error", "message": "삭제 방식이 올바르지 않습니다."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    msg = conn.execute('''
        SELECT id, message_uid, sender, receiver, room_id, filepath,
               deleted_for_all
        FROM messages WHERE id=?
    ''', (msg_id,)).fetchone()
    if not _can_access_message(conn, msg, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "메시지 접근 권한이 없습니다."}), 403

    partner = msg['room_id'] or (
        msg['receiver'] if msg['sender'] == current_user else msg['sender']
    )
    if mode == 'me':
        conn.execute('''
            INSERT OR IGNORE INTO message_hidden_users (message_uid, user_name)
            VALUES (?, ?)
        ''', (msg['message_uid'], current_user))
        conn.commit()
        _emit_chat_event(conn, partner, current_user, 'message_hidden', message_id=msg_id)
        conn.close()
        return jsonify({"status": "success", "mode": "me"})

    if msg['sender'] != current_user:
        conn.close()
        return jsonify({"status": "error", "message": "보낸 메시지만 모두에게 삭제할 수 있습니다."}), 403
    if msg['deleted_for_all']:
        conn.close()
        return jsonify({"status": "success", "mode": "all"})

    file_rows = conn.execute('''
        SELECT DISTINCT filepath FROM messages
        WHERE message_uid=? AND filepath IS NOT NULL AND filepath<>''
    ''', (msg['message_uid'],)).fetchall()
    logical = conn.execute(
        "SELECT MIN(id) AS id FROM messages WHERE message_uid=?",
        (msg['message_uid'],)
    ).fetchone()
    logical_id = int(logical['id']) if logical else msg_id
    conn.execute('''
        UPDATE messages
        SET content='', filename='', filepath='', deleted_for_all=1,
            deleted_at=CURRENT_TIMESTAMP
        WHERE message_uid=?
    ''', (msg['message_uid'],))
    conn.execute("DELETE FROM message_reactions WHERE message_key=?", (str(logical_id),))
    conn.execute("DELETE FROM message_comments WHERE message_key=?", (str(logical_id),))
    conn.commit()
    for file_row in file_rows:
        _remove_file_if_unreferenced(conn, file_row['filepath'])
    _emit_chat_event(conn, partner, current_user, 'message_changed', message_id=logical_id)
    conn.close()
    return jsonify({"status": "success", "mode": "all"})


@chat_bp.route('/api/messages/<int:msg_id>', methods=['PATCH'])
def edit_message(msg_id):
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    content = str(data.get('content') or '').strip()
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if len(content) > 5000:
        return jsonify({"status": "error", "message": "메시지는 5,000자 이하로 입력해주세요."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    msg = conn.execute('''
        SELECT id, message_uid, sender, receiver, room_id, filename,
               deleted_for_all
        FROM messages WHERE id=?
    ''', (msg_id,)).fetchone()
    if not _can_access_message(conn, msg, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "메시지 접근 권한이 없습니다."}), 403
    if msg['sender'] != current_user:
        conn.close()
        return jsonify({"status": "error", "message": "보낸 메시지만 수정할 수 있습니다."}), 403
    if msg['deleted_for_all']:
        conn.close()
        return jsonify({"status": "error", "message": "삭제된 메시지는 수정할 수 없습니다."}), 400
    if not content and not msg['filename']:
        conn.close()
        return jsonify({"status": "error", "message": "메시지 내용을 입력해주세요."}), 400

    conn.execute('''
        UPDATE messages
        SET content=?, edited_at=CURRENT_TIMESTAMP
        WHERE message_uid=?
    ''', (content, msg['message_uid']))
    conn.commit()
    partner = msg['room_id'] or (
        msg['receiver'] if msg['sender'] == current_user else msg['sender']
    )
    _emit_chat_event(conn, partner, current_user, 'message_changed', message_id=msg_id)
    conn.close()
    return jsonify({"status": "success"})

@chat_bp.route('/api/leave_chat', methods=['POST'])
def leave_chat():
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    partner = str(data.get('partner') or '').strip()
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if not partner:
        return jsonify({"status": "error", "message": "대화방 정보가 없습니다."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _can_access_room(conn, partner, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "대화방 접근 권한이 없습니다."}), 403

    cutoff_id = _max_room_message_id(conn, partner, current_user)
    conn.execute('''
        INSERT OR IGNORE INTO chat_user_room_settings
            (user_name, room_key, hidden_before_id)
        VALUES (?, ?, ?)
    ''', (current_user, partner, cutoff_id))
    conn.execute('''
        UPDATE chat_user_room_settings
        SET hidden_before_id=MAX(hidden_before_id, ?), left_at=CURRENT_TIMESTAMP
        WHERE user_name=? AND room_key=?
    ''', (cutoff_id, current_user, partner))

    if ',' in partner:
        conn.execute('''
            UPDATE chat_room_members
            SET left_at=CURRENT_TIMESTAMP
            WHERE room_key=? AND user_name=?
        ''', (partner, current_user))
        profile = conn.execute(
            "SELECT admin_user FROM chat_room_profiles WHERE room_key=?",
            (partner,)
        ).fetchone()
        if profile and profile['admin_user'] == current_user:
            successor = conn.execute('''
                SELECT user_name FROM chat_room_members
                WHERE room_key=? AND left_at IS NULL
                ORDER BY joined_at ASC, user_name ASC LIMIT 1
            ''', (partner,)).fetchone()
            if successor:
                conn.execute(
                    "UPDATE chat_room_profiles SET admin_user=?, updated_at=CURRENT_TIMESTAMP WHERE room_key=?",
                    (successor['user_name'], partner)
                )
                conn.execute(
                    "UPDATE chat_room_members SET role='member' WHERE room_key=?",
                    (partner,)
                )
                conn.execute(
                    "UPDATE chat_room_members SET role='admin' WHERE room_key=? AND user_name=?",
                    (partner, successor['user_name'])
                )

    conn.execute(
        "DELETE FROM pinned_chats WHERE user_name=? AND partner=?",
        (current_user, partner)
    )
    conn.commit()
    if ',' in partner:
        _emit_chat_event(conn, partner, current_user, 'room_changed')
    else:
        socketio.emit(
            'chat_event',
            {'type': 'room_hidden', 'partner': partner, 'actor': current_user},
            to=f"user:{current_user}",
            namespace='/chat',
        )
    conn.close()
    return jsonify({"status": "success", "history_deleted": False})


def _require_group_admin(conn, room_key, current_user):
    if ',' not in str(room_key) or not _can_access_room(conn, room_key, current_user):
        return False
    profile = _ensure_group_room(conn, room_key, current_user)
    return bool(profile and profile['admin_user'] == current_user)


@chat_bp.route('/api/chat/room/name', methods=['POST'])
def update_chat_room_name():
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    room_key = str(data.get('partner') or '').strip()
    display_name = str(data.get('display_name') or '').strip()
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if len(display_name) > 50:
        return jsonify({"status": "error", "message": "그룹방 이름은 50자 이하로 입력해주세요."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _require_group_admin(conn, room_key, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "그룹 관리자만 이름을 변경할 수 있습니다."}), 403
    conn.execute('''
        UPDATE chat_room_profiles
        SET display_name=?, updated_at=CURRENT_TIMESTAMP
        WHERE room_key=?
    ''', (display_name or None, room_key))
    conn.commit()
    _emit_chat_event(conn, room_key, current_user, 'room_changed')
    info = _room_info(conn, room_key, current_user)
    conn.close()
    return jsonify({"status": "success", "room": info})


@chat_bp.route('/api/chat/room/mute', methods=['POST'])
def update_chat_room_mute():
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    room_key = str(data.get('partner') or '').strip()
    muted = bool(data.get('muted'))
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _can_access_room(conn, room_key, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "대화방 접근 권한이 없습니다."}), 403
    conn.execute('''
        INSERT OR IGNORE INTO chat_user_room_settings (user_name, room_key)
        VALUES (?, ?)
    ''', (current_user, room_key))
    conn.execute('''
        UPDATE chat_user_room_settings
        SET notifications_muted=?
        WHERE user_name=? AND room_key=?
    ''', (1 if muted else 0, current_user, room_key))
    conn.commit()
    socketio.emit(
        'chat_event',
        {'type': 'room_changed', 'partner': room_key, 'actor': current_user},
        to=f"user:{current_user}",
        namespace='/chat',
    )
    conn.close()
    return jsonify({"status": "success", "muted": muted})


@chat_bp.route('/api/chat/room/members', methods=['POST'])
def add_chat_room_members():
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    room_key = str(data.get('partner') or '').strip()
    members = list(dict.fromkeys(
        str(name).strip() for name in (data.get('members') or []) if str(name).strip()
    ))
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if not members:
        return jsonify({"status": "error", "message": "초대할 멤버를 선택해주세요."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _require_group_admin(conn, room_key, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "그룹 관리자만 멤버를 초대할 수 있습니다."}), 403
    if any(name not in _approved_user_names(conn) for name in members):
        conn.close()
        return jsonify({"status": "error", "message": "승인되지 않은 사용자가 포함되어 있습니다."}), 400

    joined_after_id = _max_room_message_id(conn, room_key)
    for member in members:
        conn.execute('''
            INSERT OR IGNORE INTO chat_room_members
                (room_key, user_name, role, joined_after_id)
            VALUES (?, ?, 'member', ?)
        ''', (room_key, member, joined_after_id))
        conn.execute('''
            UPDATE chat_room_members
            SET left_at=NULL, role='member', joined_at=CURRENT_TIMESTAMP,
                joined_after_id=?
            WHERE room_key=? AND user_name=?
        ''', (joined_after_id, room_key, member))
        conn.execute('''
            INSERT OR IGNORE INTO chat_user_room_settings
                (user_name, room_key, hidden_before_id)
            VALUES (?, ?, ?)
        ''', (member, room_key, joined_after_id))
        conn.execute('''
            UPDATE chat_user_room_settings
            SET left_at=NULL, hidden_before_id=MAX(hidden_before_id, ?)
            WHERE user_name=? AND room_key=?
        ''', (joined_after_id, member, room_key))
    conn.commit()
    _emit_chat_event(conn, room_key, current_user, 'room_changed')
    info = _room_info(conn, room_key, current_user)
    conn.close()
    return jsonify({"status": "success", "room": info})


@chat_bp.route('/api/chat/room/remove-member', methods=['POST'])
def remove_chat_room_member():
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    room_key = str(data.get('partner') or '').strip()
    member_name = str(data.get('member') or '').strip()
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if member_name == current_user:
        return jsonify({"status": "error", "message": "본인은 채팅방 나가기를 이용해주세요."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _require_group_admin(conn, room_key, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "그룹 관리자만 멤버를 내보낼 수 있습니다."}), 403
    target = conn.execute('''
        SELECT user_name FROM chat_room_members
        WHERE room_key=? AND user_name=? AND left_at IS NULL
    ''', (room_key, member_name)).fetchone()
    if not target:
        conn.close()
        return jsonify({"status": "error", "message": "현재 참여 중인 멤버가 아닙니다."}), 404

    cutoff_id = _max_room_message_id(conn, room_key)
    conn.execute('''
        UPDATE chat_room_members SET left_at=CURRENT_TIMESTAMP
        WHERE room_key=? AND user_name=?
    ''', (room_key, member_name))
    conn.execute('''
        INSERT OR IGNORE INTO chat_user_room_settings
            (user_name, room_key, hidden_before_id)
        VALUES (?, ?, ?)
    ''', (member_name, room_key, cutoff_id))
    conn.execute('''
        UPDATE chat_user_room_settings
        SET left_at=CURRENT_TIMESTAMP, hidden_before_id=MAX(hidden_before_id, ?)
        WHERE user_name=? AND room_key=?
    ''', (cutoff_id, member_name, room_key))
    conn.execute(
        "DELETE FROM pinned_chats WHERE user_name=? AND partner=?",
        (member_name, room_key)
    )
    conn.commit()
    _emit_chat_event(conn, room_key, current_user, 'room_changed')
    socketio.emit(
        'chat_event',
        {'type': 'room_removed', 'partner': room_key, 'actor': current_user},
        to=f"user:{member_name}",
        namespace='/chat',
    )
    info = _room_info(conn, room_key, current_user)
    conn.close()
    return jsonify({"status": "success", "room": info})


@chat_bp.route('/api/chat/room/admin', methods=['POST'])
def update_chat_room_admin():
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    room_key = str(data.get('partner') or '').strip()
    admin_user = str(data.get('admin_user') or '').strip()
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _require_group_admin(conn, room_key, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "현재 그룹 관리자만 관리자를 변경할 수 있습니다."}), 403
    target = conn.execute('''
        SELECT user_name FROM chat_room_members
        WHERE room_key=? AND user_name=? AND left_at IS NULL
    ''', (room_key, admin_user)).fetchone()
    if not target:
        conn.close()
        return jsonify({"status": "error", "message": "관리자로 지정할 멤버가 없습니다."}), 404

    conn.execute(
        "UPDATE chat_room_profiles SET admin_user=?, updated_at=CURRENT_TIMESTAMP WHERE room_key=?",
        (admin_user, room_key)
    )
    conn.execute("UPDATE chat_room_members SET role='member' WHERE room_key=?", (room_key,))
    conn.execute(
        "UPDATE chat_room_members SET role='admin' WHERE room_key=? AND user_name=?",
        (room_key, admin_user)
    )
    conn.commit()
    _emit_chat_event(conn, room_key, current_user, 'room_changed')
    info = _room_info(conn, room_key, current_user)
    conn.close()
    return jsonify({"status": "success", "room": info})

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


def _socket_conversation_room(room_key, current_user):
    if ',' in str(room_key):
        return f"chat-room:{room_key}"
    participants = sorted([str(current_user), str(room_key)])
    return f"chat-direct:{participants[0]}|{participants[1]}"


@socketio.on('connect', namespace='/chat')
def chat_socket_connect():
    current_user = session.get('user_name')
    if not current_user:
        return False
    join_room(f"user:{current_user}")
    return True


@socketio.on('join_chat', namespace='/chat')
def chat_socket_join(data):
    current_user = session.get('user_name')
    room_key = str((data or {}).get('partner') or '').strip()
    if not current_user or not room_key:
        return {'status': 'error', 'message': '로그인이 필요합니다.'}
    conn = get_db()
    _ensure_chat_tables(conn)
    allowed = _can_access_room(conn, room_key, current_user)
    conn.close()
    if not allowed:
        return {'status': 'error', 'message': '대화방 접근 권한이 없습니다.'}
    join_room(_socket_conversation_room(room_key, current_user))
    return {'status': 'success'}


@socketio.on('leave_chat_room', namespace='/chat')
def chat_socket_leave(data):
    current_user = session.get('user_name')
    room_key = str((data or {}).get('partner') or '').strip()
    if current_user and room_key:
        leave_room(_socket_conversation_room(room_key, current_user))


@socketio.on('typing', namespace='/chat')
def chat_socket_typing(data):
    current_user = session.get('user_name')
    room_key = str((data or {}).get('partner') or '').strip()
    is_typing = bool((data or {}).get('is_typing'))
    if not current_user or not room_key:
        return
    conn = get_db()
    _ensure_chat_tables(conn)
    allowed = _can_access_room(conn, room_key, current_user)
    conn.close()
    if not allowed:
        return
    socketio.emit(
        'chat_typing',
        {'user': current_user, 'is_typing': is_typing},
        to=_socket_conversation_room(room_key, current_user),
        include_self=False,
        namespace='/chat',
    )
