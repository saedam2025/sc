from flask import Blueprint, jsonify, session, request, render_template, current_app
from datetime import datetime, timedelta, timezone
from flask_socketio import join_room, leave_room
import json
import os
import threading
import uuid
from urllib.parse import quote

try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception
from .database import get_db
from .organization import (
    MESSENGER_ORGANIZATION_GROUPS,
    classify_messenger_organization_group,
    normalize_messenger_department,
)
from .points import get_point_balances
from .socketio_ext import socketio
from .storage import CHAT_UPLOADS, UPLOADS_ROOT
from .secure_files import encrypted_response, encrypted_storage_name, encrypt_upload, original_filename, plaintext_size

chat_bp = Blueprint('chat', __name__)
CHAT_UPLOAD_FOLDER = str(CHAT_UPLOADS)
LEGACY_UPLOAD_FOLDER = str(UPLOADS_ROOT)
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
    return original_filename(filename, '첨부파일')


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
    stored_name = encrypted_storage_name(original_name)
    stored_path = os.path.join(CHAT_UPLOAD_FOLDER, stored_name)
    try:
        encrypt_upload(file_storage, stored_path)
    except Exception:
        try:
            os.remove(stored_path)
        except OSError:
            pass
        raise

    # DB에는 절대경로가 아니라 저장명만 남긴다. 절대경로를 넣으면 프로젝트 폴더가
    # 다른 드라이브나 서버로 옮겨졌을 때 예전 첨부를 통째로 못 찾게 된다.
    return original_name, stored_name


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


def _resolve_chat_path(filepath):
    """DB에 저장된 값에서 실제 첨부파일 경로를 찾아낸다.

    예전 메시지는 저장 당시의 절대경로(E:\\... 또는 /mnt/data/...)를 그대로
    담고 있어서, 프로젝트 폴더가 옮겨지면 파일이 멀쩡히 있어도 못 찾았다.
    경로가 맞지 않으면 저장명만 떼어 현재 업로드 폴더에서 다시 찾는다.
    """
    raw = str(filepath or '').strip()
    if not raw:
        return ''
    if ('/' in raw or '\\' in raw) and _is_allowed_chat_path(raw) and os.path.isfile(raw):
        return os.path.abspath(raw)
    stored_name = os.path.basename(raw.replace('\\', '/'))
    if not stored_name or stored_name in ('.', '..'):
        return ''
    for root in (CHAT_UPLOAD_FOLDER, LEGACY_UPLOAD_FOLDER):
        candidate = os.path.join(root, stored_name)
        if os.path.isfile(candidate) and _is_allowed_chat_path(candidate):
            return os.path.abspath(candidate)
    return ''


def _get_attachment_metadata(filepath, sent_at):
    file_size = 0
    resolved = _resolve_chat_path(filepath)
    if resolved:
        try:
            file_size = plaintext_size(resolved)
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
    resolved = _resolve_chat_path(filepath)
    if not resolved:
        # 이미 지워졌거나 이 서버에 없는 파일이면 정리된 것으로 본다.
        # (False로 두면 만료 정리가 매번 같은 행을 다시 붙잡아 filepath가 영구히 남는다.)
        return True
    try:
        os.remove(resolved)
        return True
    except OSError:
        current_app.logger.exception('메신저 첨부파일 삭제 실패: %s', resolved)
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
    # sent_at 은 SQLite CURRENT_TIMESTAMP(UTC)로 저장되므로 기준 시각도 UTC로 맞춘다.
    # local time으로 비교하면 한국 기준 9시간 일찍 삭제된다.
    cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=CHAT_RETENTION_DAYS)
    ).strftime('%Y-%m-%d %H:%M:%S')
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
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_name TEXT NOT NULL,
        endpoint TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL,
        auth TEXT NOT NULL,
        user_agent TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        last_success_at DATETIME,
        failure_count INTEGER NOT NULL DEFAULT 0
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_push_user ON chat_push_subscriptions(user_name)")

    conn.execute('''CREATE TABLE IF NOT EXISTS chat_task_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_key TEXT NOT NULL DEFAULT '',
        message_id INTEGER,
        message_uid TEXT,
        requester TEXT NOT NULL,
        assignee TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        responded_at DATETIME,
        response_text TEXT,
        response_message_id INTEGER
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_task_assignee ON chat_task_requests(assignee, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_task_requester ON chat_task_requests(requester, status)")
    conn.execute("DROP INDEX IF EXISTS idx_chat_task_message")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_task_msg_uid ON chat_task_requests(message_uid, assignee)")

    conn.execute('''CREATE TABLE IF NOT EXISTS chat_polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_key TEXT NOT NULL,
        creator TEXT NOT NULL,
        question TEXT NOT NULL,
        options TEXT NOT NULL,
        allow_multiple INTEGER NOT NULL DEFAULT 0,
        is_anonymous INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open',
        message_id INTEGER,
        message_uid TEXT,
        deadline DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        closed_at DATETIME
    )''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_polls_room ON chat_polls(room_key, id)")

    poll_columns = {
        row['name'] for row in conn.execute("PRAGMA table_info(chat_polls)").fetchall()
    }
    if 'message_uid' not in poll_columns:
        conn.execute("ALTER TABLE chat_polls ADD COLUMN message_uid TEXT")
    if 'deadline' not in poll_columns:
        conn.execute("ALTER TABLE chat_polls ADD COLUMN deadline DATETIME")
    # 대화창 카드는 논리 메시지 uid로 붙이므로, 예전 설문에도 uid를 채워 넣는다.
    conn.execute('''
        UPDATE chat_polls
        SET message_uid = (
            SELECT message_uid FROM messages WHERE messages.id = chat_polls.message_id
        )
        WHERE (message_uid IS NULL OR message_uid = '') AND message_id IS NOT NULL
    ''')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_polls_uid ON chat_polls(message_uid)")

    conn.execute('''CREATE TABLE IF NOT EXISTS chat_poll_votes (
        poll_id INTEGER NOT NULL,
        user_name TEXT NOT NULL,
        option_index INTEGER NOT NULL,
        voted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(poll_id, user_name, option_index)
    )''')

    conn.execute('''CREATE TABLE IF NOT EXISTS chat_user_status (
        user_name TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT '',
        status_message TEXT NOT NULL DEFAULT '',
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
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

    # 예전에는 첨부 경로를 절대경로로 저장해, 프로젝트 폴더가 다른 드라이브나
    # 서버로 옮겨지면 첨부가 통째로 깨졌다. 저장명만 남겨 위치와 무관하게 만든다.
    for row in conn.execute(
        "SELECT DISTINCT filepath FROM messages WHERE filepath IS NOT NULL AND filepath <> ''"
    ).fetchall():
        stored_path = str(row['filepath'])
        stored_name = os.path.basename(stored_path.replace('\\', '/'))
        if stored_name and stored_name != stored_path:
            conn.execute(
                "UPDATE messages SET filepath=? WHERE filepath=?",
                (stored_name, stored_path)
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
        (first_message['sender'] if first_message else '')
        or str(created_by or '').strip()
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

def _chat_push_config():
    return {
        'public_key': str(os.getenv('VAPID_PUBLIC_KEY') or '').strip(),
        'private_key': str(os.getenv('VAPID_PRIVATE_KEY') or '').strip(),
        'subject': str(os.getenv('VAPID_SUBJECT') or '').strip(),
    }


def _chat_push_ready():
    c = _chat_push_config()
    return bool(webpush and c['public_key'] and c['private_key'] and c['subject'])


def _push_body(content, filename):
    text = str(content or '').strip()
    if text:
        return text[:180]
    return f"📎 {filename}" if filename else '새 메시지가 도착했습니다.'


def _send_chat_push(conn, target_user, room_key, actor, content='', filename='', message_id=None):
    if not _chat_push_ready() or not target_user or target_user == actor:
        return 0
    if bool(_room_setting(conn, target_user, room_key)['notifications_muted']):
        return 0
    rows = conn.execute('''
        SELECT endpoint, p256dh, auth FROM chat_push_subscriptions
        WHERE user_name=? ORDER BY id ASC
    ''', (target_user,)).fetchall()
    if not rows:
        return 0
    is_group = ',' in str(room_key)
    if is_group:
        info = _room_info(conn, room_key, target_user)
        title = info.get('display_name') or '새담 사내메신저'
        body = f"{actor}: {_push_body(content, filename)}"
    else:
        title = actor or '새담 사내메신저'
        body = _push_body(content, filename)
    payload = {
        'title': title,
        'body': body,
        'icon': '/static/chat_notify_icon.png',
        'tag': f"saedam-chat-{quote(str(room_key), safe='')}",
        'partner': str(room_key),
        'url': f"/chat_popup/{quote(str(room_key), safe='')}",
        'message_id': message_id,
    }
    cfg = _chat_push_config()
    sent = 0
    changed = False
    for row in rows:
        try:
            webpush(
                subscription_info={
                    'endpoint': row['endpoint'],
                    'keys': {'p256dh': row['p256dh'], 'auth': row['auth']},
                },
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=cfg['private_key'],
                vapid_claims={'sub': cfg['subject']},
                ttl=3600,
                timeout=5,
            )
            conn.execute('''
                UPDATE chat_push_subscriptions
                SET last_success_at=CURRENT_TIMESTAMP, failure_count=0,
                    updated_at=CURRENT_TIMESTAMP
                WHERE endpoint=?
            ''', (row['endpoint'],))
            sent += 1
            changed = True
        except WebPushException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (404, 410):
                conn.execute('DELETE FROM chat_push_subscriptions WHERE endpoint=?', (row['endpoint'],))
            else:
                conn.execute('''
                    UPDATE chat_push_subscriptions
                    SET failure_count=failure_count+1, updated_at=CURRENT_TIMESTAMP
                    WHERE endpoint=?
                ''', (row['endpoint'],))
                current_app.logger.warning('메신저 Web Push 실패 user=%s status=%s', target_user, status)
            changed = True
        except Exception:
            current_app.logger.exception('메신저 Web Push 예외 user=%s', target_user)
    if changed:
        conn.commit()
    return sent


@chat_bp.route('/chat-push-sw.js')
def chat_push_service_worker():
    response = current_app.send_static_file('js/chat_push_sw.js')
    response.headers['Content-Type'] = 'application/javascript; charset=utf-8'
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@chat_bp.route('/api/chat/push/public-key')
def chat_push_public_key():
    if not session.get('user_name'):
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    cfg = _chat_push_config()
    if not _chat_push_ready():
        return jsonify({'status': 'error', 'configured': False, 'message': '서버의 Web Push(VAPID) 설정이 필요합니다.'}), 503
    response = jsonify({'status': 'success', 'configured': True, 'public_key': cfg['public_key']})
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response


@chat_bp.route('/api/chat/push/subscribe', methods=['POST'])
def chat_push_subscribe():
    user = session.get('user_name')
    if not user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    if not _chat_push_ready():
        return jsonify({'status': 'error', 'message': 'Web Push 서버 설정이 필요합니다.'}), 503
    data = request.get_json(silent=True) or {}
    keys = data.get('keys') or {}
    endpoint = str(data.get('endpoint') or '').strip()
    p256dh = str(keys.get('p256dh') or '').strip()
    auth = str(keys.get('auth') or '').strip()
    if not endpoint.startswith('https://') or not p256dh or not auth:
        return jsonify({'status': 'error', 'message': '푸시 구독 정보가 올바르지 않습니다.'}), 400
    conn = get_db()
    _ensure_chat_tables(conn)
    conn.execute('''
        INSERT INTO chat_push_subscriptions (user_name, endpoint, p256dh, auth, user_agent)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(endpoint) DO UPDATE SET
            user_name=excluded.user_name,
            p256dh=excluded.p256dh,
            auth=excluded.auth,
            user_agent=excluded.user_agent,
            updated_at=CURRENT_TIMESTAMP,
            failure_count=0
    ''', (user, endpoint, p256dh, auth, str(request.headers.get('User-Agent') or '')[:500]))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'subscribed': True})


@chat_bp.route('/api/chat/push/unsubscribe', methods=['POST'])
def chat_push_unsubscribe():
    user = session.get('user_name')
    if not user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    endpoint = str((request.get_json(silent=True) or {}).get('endpoint') or '').strip()
    if endpoint:
        conn = get_db()
        _ensure_chat_tables(conn)
        conn.execute('DELETE FROM chat_push_subscriptions WHERE user_name=? AND endpoint=?', (user, endpoint))
        conn.commit()
        conn.close()
    return jsonify({'status': 'success', 'subscribed': False})


@chat_bp.route('/api/chat/push/test', methods=['POST'])
def chat_push_test():
    user = session.get('user_name')
    if not user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    conn = get_db()
    _ensure_chat_tables(conn)
    sent = _send_chat_push(conn, user, user, '새담 사내메신저', content='휴대폰 푸시 알림 테스트입니다.')
    conn.close()
    return jsonify({'status': 'success', 'sent': sent})


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

    db_users = conn.execute("SELECT name, profile_icon, profile_path FROM users WHERE status='승인' ORDER BY level ASC, id ASC").fetchall()
    user_list = []
    user_icons = {}
    user_profile_paths = {}
    for u in db_users:
        if u['name'] not in user_list: user_list.append(u['name'])
        user_icons[u['name']] = u['profile_icon'] if u['profile_icon'] else '👤'
        if u['profile_path']:
            user_profile_paths[u['name']] = u['profile_path']
        
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
        chat_user_profile_paths=user_profile_paths,
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


@chat_bp.route('/api/chat/mark-read', methods=['POST'])
def mark_chat_room_read():
    """알림의 [읽음 처리] 버튼처럼 대화방을 열지 않고 읽음만 표시한다."""
    current_user = session.get('user_name')
    data = request.get_json(silent=True) or {}
    room_key = str(data.get('partner') or '').strip()
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if not room_key:
        return jsonify({"status": "error", "message": "대화방 정보가 없습니다."}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _can_access_room(conn, room_key, current_user):
        conn.close()
        return jsonify({"status": "error", "message": "대화방 접근 권한이 없습니다."}), 403

    cutoff_id = _message_access_cutoff(conn, room_key, current_user) or 0
    if ',' in room_key:
        cursor = conn.execute(
            "UPDATE messages SET is_read=1 "
            "WHERE receiver=? AND room_id=? AND id>? AND " + UNREAD_SQL,
            (current_user, room_key, cutoff_id)
        )
    else:
        cursor = conn.execute(
            "UPDATE messages SET is_read=1 "
            "WHERE receiver=? AND sender=? AND room_id IS NULL AND id>? AND " + UNREAD_SQL,
            (current_user, room_key, cutoff_id)
        )
    updated = int(cursor.rowcount or 0)
    conn.commit()
    if updated:
        _emit_chat_event(conn, room_key, current_user, 'message_changed')
    conn.close()
    return jsonify({"status": "success", "updated": updated})


@chat_bp.route('/api/chat/organization')
def chat_organization():
    """로그인 사용자가 메신저 조직도에 필요한 최소 회원정보만 조회한다."""
    if not session.get('user_name'):
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401

    conn = get_db()
    _ensure_chat_tables(conn)
    users = conn.execute('''
        SELECT emp_no, name, department, position, level, profile_icon, profile_path
        FROM users
        WHERE status = '승인'
          AND LOWER(COALESCE(emp_no, '')) <> 'admin'
          AND LOWER(COALESCE(name, '')) <> 'admin'
        ORDER BY level ASC, id ASC
    ''').fetchall()
    point_balances = get_point_balances(conn, [user['name'] or '' for user in users])
    # 조직도에서도 자리비움·연차 같은 상태를 바로 보여준다.
    user_statuses = _get_user_statuses(conn, [user['name'] or '' for user in users])
    current_user_points = point_balances.get(str(session.get('user_name') or ''), 0)
    conn.close()

    return jsonify({
        "status": "success",
        "organization_groups": list(MESSENGER_ORGANIZATION_GROUPS),
        "users": [
            {
                "emp_no": user['emp_no'] or '',
                "name": user['name'] or '',
                "department": normalize_messenger_department(user['department']),
                "position": user['position'] or '',
                "level": user['level'] if user['level'] is not None else 99,
                "organization_group": classify_messenger_organization_group(
                    user['department'], user['position'], user['level']
                ),
                "icon": user['profile_icon'] or '👤',
                "profile_path": user['profile_path'] or '',
                "points": point_balances.get(user['name'] or '', 0),
                "status": user_statuses.get(user['name'] or ''),
            }
            for user in users
        ],
        "current_user_points": current_user_points,
    })

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
        for receiver in receivers:
            _send_chat_push(
                conn, receiver, room_id, sender,
                content=content, filename=filename, message_id=first_message_id,
            )
    else:
        for partner, message_id in emitted_rooms:
            _emit_chat_event(
                conn, partner, sender, 'message',
                message_id=message_id,
                content=content[:120],
                filename=filename,
            )
            _send_chat_push(
                conn, partner, sender, sender,
                content=content, filename=filename, message_id=message_id,
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
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=CHAT_RETENTION_DAYS)
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

    resolved = _resolve_chat_path(filepath)
    if not resolved:
        return jsonify({"status": "error", "message": "첨부파일을 찾을 수 없습니다."}), 404
    return encrypted_response(
        resolved,
        filename,
        as_attachment=request.args.get('download') == '1',
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

    # 변경 사항: 안 읽은 메시지가 있을 때만 업데이트를 진행하고, 변경이 일어나면 소켓 이벤트를 발송합니다.
    if ',' in other_user:
        cursor = conn.execute('''
            UPDATE messages SET is_read=1
            WHERE receiver=? AND room_id=? AND id>? AND (is_read IN (0, '0', 'False', 'false') OR is_read IS NULL)
        ''', (current_user, other_user, cutoff_id))
    else:
        cursor = conn.execute('''
            UPDATE messages SET is_read=1
            WHERE receiver=? AND sender=? AND room_id IS NULL AND id>? AND (is_read IN (0, '0', 'False', 'false') OR is_read IS NULL)
        ''', (current_user, other_user, cutoff_id))
    
    rows_updated = cursor.rowcount
    conn.commit()

    # DB에 읽음 처리 업데이트가 실제로 일어났다면 프론트엔드에 상태 변경 알림 전송
    if rows_updated > 0:
        _emit_chat_event(conn, other_user, current_user, 'message_changed')

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
        return jsonify({"status": "error", "message": "방장만 대화방 이름을 변경할 수 있습니다."}), 403
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
    muted = data.get('muted')
    if not current_user:
        return jsonify({"status": "error", "message": "로그인이 필요합니다."}), 401
    if not isinstance(muted, bool):
        return jsonify({"status": "error", "message": "알림 설정 값이 올바르지 않습니다."}), 400

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
        return jsonify({"status": "error", "message": "방장만 멤버를 초대할 수 있습니다."}), 403
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
        return jsonify({"status": "error", "message": "방장만 멤버를 내보낼 수 있습니다."}), 403
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
        return jsonify({"status": "error", "message": "현재 방장만 방장을 넘길 수 있습니다."}), 403
    target = conn.execute('''
        SELECT user_name FROM chat_room_members
        WHERE room_key=? AND user_name=? AND left_at IS NULL
    ''', (room_key, admin_user)).fetchone()
    if not target:
        conn.close()
        return jsonify({"status": "error", "message": "방장으로 지정할 멤버가 없습니다."}), 404

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


# ---------------------------------------------------------------------------
# 대화창 상단 메뉴바 3종: 업무요청 / 설문·투표 / 상태표시
# ---------------------------------------------------------------------------

CHAT_STATUS_PRESETS = (
    {'key': 'away', 'label': '자리비움', 'icon': '🚶', 'color': '#f59e0b'},
    {'key': 'meeting', 'label': '회의중', 'icon': '📋', 'color': '#6366f1'},
    {'key': 'field', 'label': '외근', 'icon': '🚗', 'color': '#0ea5e9'},
    {'key': 'leave', 'label': '연차', 'icon': '🌴', 'color': '#10b981'},
    {'key': 'call', 'label': '통화중', 'icon': '📞', 'color': '#ec4899'},
    {'key': 'absent', 'label': '부재중', 'icon': '🌙', 'color': '#94a3b8'},
)
CHAT_STATUS_MAP = {item['key']: item for item in CHAT_STATUS_PRESETS}
CHAT_TASK_SUMMARY_LIMIT = 500
CHAT_POLL_MAX_OPTIONS = 10


def _status_payload(user_name, status_key, status_message='', updated_at=''):
    preset = CHAT_STATUS_MAP.get(str(status_key or ''))
    return {
        'user': user_name,
        'status': preset['key'] if preset else '',
        'label': preset['label'] if preset else '',
        'icon': preset['icon'] if preset else '',
        'color': preset['color'] if preset else '',
        'message': str(status_message or ''),
        'updated_at': str(updated_at or ''),
    }


def _get_user_statuses(conn, names):
    names = [str(name) for name in dict.fromkeys(names) if str(name or '').strip()]
    if not names:
        return {}
    placeholders = ','.join(['?'] * len(names))
    rows = conn.execute(f"""
        SELECT user_name, status, status_message, updated_at
        FROM chat_user_status
        WHERE user_name IN ({placeholders}) AND status <> ''
    """, names).fetchall()
    return {
        row['user_name']: _status_payload(
            row['user_name'], row['status'], row['status_message'], row['updated_at']
        )
        for row in rows
        if row['status'] in CHAT_STATUS_MAP
    }


def _deliver_chat_message(conn, sender, room_key, content, filename='', filepath=''):
    """설문·업무요청처럼 서버가 대신 보내는 메시지를 대화방에 남긴다.

    반환값은 (첫 메시지 id, 논리 메시지 uid) 이며 실패하면 (None, None).
    """
    room_key = str(room_key or '').strip()
    content = str(content or '')
    if not sender or not room_key or not (content.strip() or filename):
        return None, None

    is_group = ',' in room_key
    room_id = room_key if is_group else None
    if is_group:
        receivers = [
            row['user_name'] for row in _active_group_members(conn, room_key)
            if row['user_name'] != sender
        ]
    else:
        receivers = [room_key]
    if not receivers:
        return None, None

    first_message_id = None
    first_message_uid = None
    direct_targets = []
    group_message_uid = uuid.uuid4().hex if room_id else None
    for receiver in receivers:
        message_uid = group_message_uid or uuid.uuid4().hex
        cursor = conn.execute("""
            INSERT INTO messages
                (sender, receiver, content, filename, filepath, room_id, is_read, message_uid)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
        """, (sender, receiver, content, filename, filepath, room_id, message_uid))
        if first_message_id is None:
            first_message_id = int(cursor.lastrowid)
            first_message_uid = message_uid
        if not room_id:
            for user_name, key in ((sender, receiver), (receiver, sender)):
                conn.execute("""
                    INSERT OR IGNORE INTO chat_user_room_settings (user_name, room_key)
                    VALUES (?, ?)
                """, (user_name, key))
                conn.execute("""
                    UPDATE chat_user_room_settings SET left_at=NULL
                    WHERE user_name=? AND room_key=?
                """, (user_name, key))
            direct_targets.append((receiver, int(cursor.lastrowid)))
    conn.commit()

    if room_id:
        _emit_chat_event(
            conn, room_id, sender, 'message',
            message_id=first_message_id, content=content[:120], filename=filename,
        )
        for receiver in receivers:
            _send_chat_push(
                conn, receiver, room_id, sender,
                content=content, filename=filename, message_id=first_message_id,
            )
    else:
        for partner, message_id in direct_targets:
            _emit_chat_event(
                conn, partner, sender, 'message',
                message_id=message_id, content=content[:120], filename=filename,
            )
            _send_chat_push(
                conn, partner, sender, sender,
                content=content, filename=filename, message_id=message_id,
            )
    return first_message_id, first_message_uid


def _emit_user_event(target_user, event_type, **payload):
    """대화방과 무관하게 특정 사용자에게만 전달하는 사이드 이벤트."""
    if not target_user:
        return
    socketio.emit(
        'chat_side_event',
        {'type': event_type, **payload},
        to=f"user:{target_user}",
        namespace='/chat',
    )


def _task_room_key(row, viewer):
    """1:1 업무요청은 보는 사람에 따라 대화방 키가 달라진다."""
    room_key = str(row['room_key'] or '')
    if room_key:
        return room_key
    return row['requester'] if viewer == row['assignee'] else row['assignee']


def _serialize_task_request(row, viewer):
    return {
        'id': row['id'],
        'room_key': _task_room_key(row, viewer),
        'is_group': bool(str(row['room_key'] or '')),
        'message_id': row['message_id'],
        'requester': row['requester'],
        'assignee': row['assignee'],
        'content': row['content'] or '',
        'status': row['status'],
        'created_at': row['created_at'] or '',
        'responded_at': row['responded_at'] or '',
        'response_text': row['response_text'] or '',
        'direction': 'sent' if viewer == row['requester'] else 'received',
    }


def _fetch_task_request(conn, request_id):
    return conn.execute("""
        SELECT id, room_key, message_id, message_uid, requester, assignee, content,
               status, created_at, responded_at, response_text, response_message_id
        FROM chat_task_requests WHERE id=?
    """, (request_id,)).fetchone()


def _parse_poll_deadline(value):
    """클라이언트가 보낸 ISO 마감일시를 UTC 기준 문자열로 바꾼다."""
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return False
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime('%Y-%m-%d %H:%M:%S')


def _close_expired_polls(conn, room_key=None):
    """마감기한이 지난 설문은 조회/투표 시점에 자동으로 마감한다."""
    sql = """
        UPDATE chat_polls
        SET status='closed', closed_at=CURRENT_TIMESTAMP
        WHERE status='open' AND deadline IS NOT NULL AND deadline <> ''
          AND deadline <= CURRENT_TIMESTAMP
    """
    params = []
    if room_key:
        sql += " AND room_key=?"
        params.append(room_key)
    cursor = conn.execute(sql, params)
    if cursor.rowcount:
        conn.commit()
    return cursor.rowcount


def _poll_row(conn, poll_id):
    return conn.execute("""
        SELECT id, room_key, creator, question, options, allow_multiple, is_anonymous,
               status, message_id, message_uid, deadline, created_at, closed_at
        FROM chat_polls WHERE id=?
    """, (poll_id,)).fetchone()


def _is_room_admin(conn, room_key, current_user):
    profile = conn.execute(
        "SELECT admin_user FROM chat_room_profiles WHERE room_key=?", (room_key,)
    ).fetchone()
    return bool(profile and profile['admin_user'] == current_user)


def _poll_payload(conn, poll, current_user, is_room_admin=False):
    try:
        options = json.loads(poll['options'] or '[]')
    except (TypeError, ValueError):
        options = []
    vote_rows = conn.execute(
        "SELECT user_name, option_index FROM chat_poll_votes WHERE poll_id=? ORDER BY voted_at ASC",
        (poll['id'],)
    ).fetchall()
    counts = [0] * len(options)
    voters = [[] for _ in options]
    my_votes = []
    participants = set()
    for vote in vote_rows:
        index = int(vote['option_index'])
        participants.add(vote['user_name'])
        if 0 <= index < len(options):
            counts[index] += 1
            voters[index].append(vote['user_name'])
        if vote['user_name'] == current_user:
            my_votes.append(index)
    is_anonymous = bool(poll['is_anonymous'])
    return {
        'id': poll['id'],
        'room_key': poll['room_key'],
        'creator': poll['creator'],
        'question': poll['question'],
        'allow_multiple': bool(poll['allow_multiple']),
        'is_anonymous': is_anonymous,
        'status': poll['status'],
        'created_at': poll['created_at'] or '',
        'closed_at': poll['closed_at'] or '',
        'deadline': poll['deadline'] or '',
        'message_id': poll['message_id'],
        'message_uid': poll['message_uid'] or '',
        'is_owner': poll['creator'] == current_user,
        'can_close': poll['creator'] == current_user or bool(is_room_admin),
        'my_votes': sorted(my_votes),
        'total_voters': len(participants),
        'options': [
            {
                'index': index,
                'text': str(text),
                'count': counts[index],
                'voters': [] if is_anonymous else voters[index],
            }
            for index, text in enumerate(options)
        ],
    }


@chat_bp.route('/api/chat/status', methods=['GET'])
def get_chat_status():
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    requested = [
        name.strip() for name in str(request.args.get('users') or '').split(',')
        if name.strip()
    ]
    conn = get_db()
    _ensure_chat_tables(conn)
    statuses = _get_user_statuses(conn, requested + [current_user])
    conn.close()
    return jsonify({
        'status': 'success',
        'presets': list(CHAT_STATUS_PRESETS),
        'my_status': statuses.get(current_user) or _status_payload(current_user, ''),
        'statuses': {
            name: statuses.get(name) or _status_payload(name, '')
            for name in requested
        },
    })


@chat_bp.route('/api/chat/status', methods=['POST'])
def set_chat_status():
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    status_key = str(data.get('status') or '').strip()
    status_message = str(data.get('message') or '').strip()[:60]
    if status_key and status_key not in CHAT_STATUS_MAP:
        return jsonify({'status': 'error', 'message': '지원하지 않는 상태입니다.'}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    conn.execute("""
        INSERT INTO chat_user_status (user_name, status, status_message, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_name) DO UPDATE SET
            status=excluded.status,
            status_message=excluded.status_message,
            updated_at=CURRENT_TIMESTAMP
    """, (current_user, status_key, status_message if status_key else ''))
    conn.commit()
    row = conn.execute(
        "SELECT user_name, status, status_message, updated_at FROM chat_user_status WHERE user_name=?",
        (current_user,)
    ).fetchone()
    conn.close()

    payload = _status_payload(
        current_user,
        row['status'] if row else '',
        row['status_message'] if row else '',
        row['updated_at'] if row else '',
    )
    socketio.emit('chat_status', payload, namespace='/chat')
    return jsonify({'status': 'success', 'my_status': payload})


@chat_bp.route('/api/chat/task-request', methods=['POST'])
def create_chat_task_request():
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    try:
        message_id = int(data.get('message_id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': '업무로 등록할 메시지를 선택해주세요.'}), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    message = conn.execute("""
        SELECT id, message_uid, sender, receiver, room_id, content, filename,
               COALESCE(deleted_for_all, 0) AS deleted_for_all
        FROM messages WHERE id=?
    """, (message_id,)).fetchone()
    if not message or not _can_access_message(conn, message, current_user):
        conn.close()
        return jsonify({'status': 'error', 'message': '메시지를 찾을 수 없습니다.'}), 404
    if message['sender'] != current_user:
        conn.close()
        return jsonify({'status': 'error', 'message': '내가 보낸 메시지만 업무요청으로 등록할 수 있습니다.'}), 403
    if message['deleted_for_all']:
        conn.close()
        return jsonify({'status': 'error', 'message': '삭제된 메시지는 업무요청으로 등록할 수 없습니다.'}), 400

    summary = str(message['content'] or '').strip()
    if not summary and message['filename']:
        summary = '📎 ' + str(message['filename'])
    if not summary:
        conn.close()
        return jsonify({'status': 'error', 'message': '내용이 없는 메시지는 업무요청으로 등록할 수 없습니다.'}), 400
    summary = summary[:CHAT_TASK_SUMMARY_LIMIT]

    room_key = str(message['room_id'] or '')
    if message['message_uid']:
        assignee_rows = conn.execute(
            "SELECT DISTINCT receiver FROM messages WHERE message_uid=?",
            (message['message_uid'],)
        ).fetchall()
        assignees = [row['receiver'] for row in assignee_rows if row['receiver']]
    else:
        assignees = [message['receiver']] if message['receiver'] else []
    assignees = [name for name in dict.fromkeys(assignees) if name != current_user]
    if not assignees:
        conn.close()
        return jsonify({'status': 'error', 'message': '업무를 요청할 상대가 없습니다.'}), 400

    created = []
    skipped = []
    for assignee in assignees:
        existing = conn.execute("""
            SELECT id FROM chat_task_requests
            WHERE assignee=? AND status='pending'
              AND ((message_uid IS NOT NULL AND message_uid=?) OR message_id=?)
        """, (assignee, message['message_uid'], message_id)).fetchone()
        if existing:
            skipped.append(assignee)
            continue
        cursor = conn.execute("""
            INSERT INTO chat_task_requests
                (room_key, message_id, message_uid, requester, assignee, content, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (room_key, message_id, message['message_uid'], current_user, assignee, summary))
        created.append((assignee, int(cursor.lastrowid)))
    conn.commit()

    for assignee, request_id in created:
        _emit_user_event(
            assignee, 'task_request_created',
            request_id=request_id,
            requester=current_user,
            room_key=room_key or current_user,
            content=summary[:120],
        )
        _send_chat_push(
            conn, assignee, room_key or current_user, current_user,
            content='[업무요청] ' + summary, message_id=message_id,
        )
    conn.close()

    if not created:
        return jsonify({'status': 'error', 'message': '이미 업무요청으로 등록된 메시지입니다.'}), 409
    return jsonify({
        'status': 'success',
        'created': [assignee for assignee, _ in created],
        'skipped': skipped,
    })


@chat_bp.route('/api/chat/task-requests', methods=['GET'])
def list_chat_task_requests():
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    box = str(request.args.get('box') or 'received').strip()
    if box not in ('received', 'sent', 'all'):
        box = 'received'

    conn = get_db()
    _ensure_chat_tables(conn)
    base_sql = """
        SELECT id, room_key, message_id, message_uid, requester, assignee, content,
               status, created_at, responded_at, response_text, response_message_id
        FROM chat_task_requests
    """
    if box == 'received':
        rows = conn.execute(
            base_sql + " WHERE assignee=? ORDER BY id DESC LIMIT 100", (current_user,)
        ).fetchall()
    elif box == 'sent':
        rows = conn.execute(
            base_sql + " WHERE requester=? ORDER BY id DESC LIMIT 100", (current_user,)
        ).fetchall()
    else:
        rows = conn.execute(
            base_sql + " WHERE assignee=? OR requester=? ORDER BY id DESC LIMIT 100",
            (current_user, current_user)
        ).fetchall()
    pending_received = conn.execute(
        "SELECT COUNT(*) AS c FROM chat_task_requests WHERE assignee=? AND status='pending'",
        (current_user,)
    ).fetchone()['c']
    pending_sent = conn.execute(
        "SELECT COUNT(*) AS c FROM chat_task_requests WHERE requester=? AND status='pending'",
        (current_user,)
    ).fetchone()['c']
    conn.close()

    return jsonify({
        'status': 'success',
        'box': box,
        'requests': [_serialize_task_request(row, current_user) for row in rows],
        'pending_received': int(pending_received or 0),
        'pending_sent': int(pending_sent or 0),
    })


@chat_bp.route('/api/chat/task-request/<int:request_id>/respond', methods=['POST'])
def respond_chat_task_request(request_id):
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    if request.is_json:
        data = request.get_json(silent=True) or {}
        response_text = str(data.get('content') or '').strip()[:1000]
    else:
        response_text = str(request.form.get('content') or '').strip()[:1000]
    upload = request.files.get('file')
    has_file = bool(upload and upload.filename)
    if not response_text:
        response_text = '첨부파일을 확인해주세요.' if has_file else '업무를 완료했습니다.'

    conn = get_db()
    _ensure_chat_tables(conn)
    task = _fetch_task_request(conn, request_id)
    if not task:
        conn.close()
        return jsonify({'status': 'error', 'message': '업무요청을 찾을 수 없습니다.'}), 404
    if task['assignee'] != current_user:
        conn.close()
        return jsonify({'status': 'error', 'message': '요청받은 담당자만 응답할 수 있습니다.'}), 403
    if task['status'] != 'pending':
        conn.close()
        return jsonify({'status': 'error', 'message': '이미 처리된 업무요청입니다.'}), 400

    filename, filepath = '', ''
    if has_file:
        try:
            filename, filepath = _save_chat_attachment(upload)
        except ValueError as exc:
            conn.close()
            return jsonify({'status': 'error', 'message': str(exc)}), 413
        except OSError:
            conn.close()
            current_app.logger.exception('업무요청 응답 첨부파일 저장 실패')
            return jsonify({'status': 'error', 'message': '첨부파일을 저장하지 못했습니다.'}), 500

    room_key = _task_room_key(task, current_user)
    body = '✅ 업무요청 응답\n· 요청: ' + (task['content'] or '') + '\n· 응답: ' + response_text
    try:
        response_message_id, _ = _deliver_chat_message(
            conn, current_user, room_key, body, filename=filename, filepath=filepath
        )
    except Exception:
        conn.rollback()
        _remove_physical_file(filepath)
        conn.close()
        current_app.logger.exception('업무요청 응답 저장 실패')
        return jsonify({'status': 'error', 'message': '응답을 전송하지 못했습니다.'}), 500
    conn.execute("""
        UPDATE chat_task_requests
        SET status='done', responded_at=CURRENT_TIMESTAMP,
            response_text=?, response_message_id=?
        WHERE id=?
    """, (response_text, response_message_id, request_id))
    conn.commit()
    conn.close()

    _emit_user_event(
        task['requester'], 'task_request_done',
        request_id=request_id, assignee=current_user, response=response_text[:120],
    )
    _emit_user_event(
        current_user, 'task_request_done',
        request_id=request_id, assignee=current_user,
    )
    return jsonify({
        'status': 'success',
        'message_id': response_message_id,
        'filename': filename,
    })


@chat_bp.route('/api/chat/task-request/<int:request_id>/cancel', methods=['POST'])
def cancel_chat_task_request(request_id):
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    conn = get_db()
    _ensure_chat_tables(conn)
    task = _fetch_task_request(conn, request_id)
    if not task:
        conn.close()
        return jsonify({'status': 'error', 'message': '업무요청을 찾을 수 없습니다.'}), 404
    if task['requester'] != current_user:
        conn.close()
        return jsonify({'status': 'error', 'message': '요청한 사람만 취소할 수 있습니다.'}), 403
    if task['status'] != 'pending':
        conn.close()
        return jsonify({'status': 'error', 'message': '이미 처리된 업무요청입니다.'}), 400

    conn.execute("UPDATE chat_task_requests SET status='canceled' WHERE id=?", (request_id,))
    conn.commit()
    conn.close()
    _emit_user_event(
        task['assignee'], 'task_request_canceled',
        request_id=request_id, requester=current_user,
    )
    return jsonify({'status': 'success'})


@chat_bp.route('/api/chat/polls', methods=['GET'])
def list_chat_polls():
    current_user = session.get('user_name')
    room_key = str(request.args.get('partner') or '').strip()
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    if not room_key:
        return jsonify({'status': 'success', 'polls': []})

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _can_access_room(conn, room_key, current_user):
        conn.close()
        return jsonify({'status': 'error', 'message': '대화방 접근 권한이 없습니다.'}), 403
    _close_expired_polls(conn, room_key)
    is_admin = _is_room_admin(conn, room_key, current_user)
    rows = conn.execute("""
        SELECT id, room_key, creator, question, options, allow_multiple, is_anonymous,
               status, message_id, message_uid, deadline, created_at, closed_at
        FROM chat_polls WHERE room_key=? ORDER BY id DESC LIMIT 30
    """, (room_key,)).fetchall()
    polls = [_poll_payload(conn, row, current_user, is_admin) for row in rows]
    conn.close()
    return jsonify({'status': 'success', 'polls': polls})


@chat_bp.route('/api/chat/poll', methods=['POST'])
def create_chat_poll():
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    room_key = str(data.get('partner') or '').strip()
    question = str(data.get('question') or '').strip()[:200]
    raw_options = data.get('options') or []
    allow_multiple = 1 if data.get('allow_multiple') else 0
    is_anonymous = 1 if data.get('is_anonymous') else 0
    deadline = _parse_poll_deadline(data.get('deadline'))
    if deadline is False:
        return jsonify({'status': 'error', 'message': '마감기한 형식이 올바르지 않습니다.'}), 400

    options = []
    for option in raw_options if isinstance(raw_options, list) else []:
        text = str(option or '').strip()[:100]
        if text and text not in options:
            options.append(text)
    if not room_key or not question:
        return jsonify({'status': 'error', 'message': '질문을 입력해주세요.'}), 400
    if len(options) < 2:
        return jsonify({'status': 'error', 'message': '보기를 2개 이상 입력해주세요.'}), 400
    if len(options) > CHAT_POLL_MAX_OPTIONS:
        return jsonify({
            'status': 'error',
            'message': '보기는 최대 ' + str(CHAT_POLL_MAX_OPTIONS) + '개까지 등록할 수 있습니다.',
        }), 400

    conn = get_db()
    _ensure_chat_tables(conn)
    if not _can_access_room(conn, room_key, current_user):
        conn.close()
        return jsonify({'status': 'error', 'message': '대화방 접근 권한이 없습니다.'}), 403
    if ',' not in room_key:
        conn.close()
        return jsonify({'status': 'error', 'message': '설문/투표는 단체 대화방에서만 만들 수 있습니다.'}), 400

    body = '📊 설문/투표 · ' + question
    message_id, message_uid = _deliver_chat_message(conn, current_user, room_key, body)
    cursor = conn.execute("""
        INSERT INTO chat_polls
            (room_key, creator, question, options, allow_multiple, is_anonymous, status,
             message_id, message_uid, deadline)
        VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
    """, (
        room_key, current_user, question, json.dumps(options, ensure_ascii=False),
        allow_multiple, is_anonymous, message_id, message_uid, deadline,
    ))
    poll_id = int(cursor.lastrowid)
    conn.commit()
    _emit_chat_event(conn, room_key, current_user, 'poll_changed', poll_id=poll_id)
    poll = _poll_payload(
        conn, _poll_row(conn, poll_id), current_user,
        _is_room_admin(conn, room_key, current_user),
    )
    conn.close()
    return jsonify({'status': 'success', 'poll': poll})


@chat_bp.route('/api/chat/poll/<int:poll_id>/vote', methods=['POST'])
def vote_chat_poll(poll_id):
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    raw_options = data.get('options')
    if raw_options is None:
        raw_options = [data.get('option')]
    selected = []
    for value in raw_options if isinstance(raw_options, list) else []:
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if index not in selected:
            selected.append(index)

    conn = get_db()
    _ensure_chat_tables(conn)
    poll = _poll_row(conn, poll_id)
    if not poll:
        conn.close()
        return jsonify({'status': 'error', 'message': '설문을 찾을 수 없습니다.'}), 404
    if not _can_access_room(conn, poll['room_key'], current_user):
        conn.close()
        return jsonify({'status': 'error', 'message': '대화방 접근 권한이 없습니다.'}), 403
    if _close_expired_polls(conn, poll['room_key']):
        poll = _poll_row(conn, poll_id)
    if poll['status'] != 'open':
        conn.close()
        return jsonify({'status': 'error', 'message': '마감된 설문입니다.'}), 400

    try:
        options = json.loads(poll['options'] or '[]')
    except (TypeError, ValueError):
        options = []
    selected = [index for index in selected if 0 <= index < len(options)]
    if not poll['allow_multiple']:
        selected = selected[:1]

    conn.execute(
        "DELETE FROM chat_poll_votes WHERE poll_id=? AND user_name=?",
        (poll_id, current_user)
    )
    for index in selected:
        conn.execute("""
            INSERT OR IGNORE INTO chat_poll_votes (poll_id, user_name, option_index)
            VALUES (?, ?, ?)
        """, (poll_id, current_user, index))
    conn.commit()
    _emit_chat_event(conn, poll['room_key'], current_user, 'poll_changed', poll_id=poll_id)
    payload = _poll_payload(
        conn, poll, current_user, _is_room_admin(conn, poll['room_key'], current_user)
    )
    conn.close()
    return jsonify({'status': 'success', 'poll': payload})


@chat_bp.route('/api/chat/poll/<int:poll_id>/close', methods=['POST'])
def close_chat_poll(poll_id):
    current_user = session.get('user_name')
    if not current_user:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    conn = get_db()
    _ensure_chat_tables(conn)
    poll = _poll_row(conn, poll_id)
    if not poll:
        conn.close()
        return jsonify({'status': 'error', 'message': '설문을 찾을 수 없습니다.'}), 404
    if not _can_access_room(conn, poll['room_key'], current_user):
        conn.close()
        return jsonify({'status': 'error', 'message': '대화방 접근 권한이 없습니다.'}), 403
    if poll['creator'] != current_user and not _is_room_admin(conn, poll['room_key'], current_user):
        conn.close()
        return jsonify({'status': 'error', 'message': '설문 작성자나 방장만 마감할 수 있습니다.'}), 403

    conn.execute(
        "UPDATE chat_polls SET status='closed', closed_at=CURRENT_TIMESTAMP WHERE id=?",
        (poll_id,)
    )
    conn.commit()
    _emit_chat_event(conn, poll['room_key'], current_user, 'poll_changed', poll_id=poll_id)
    conn.close()
    return jsonify({'status': 'success'})


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
