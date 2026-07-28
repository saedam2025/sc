import secrets

from .database import get_db


RETENTION_MONTHS = 3
PAGE_DEDUPE_SECONDS = 10

_NON_PAGE_PREFIXES = (
    '/api/',
    '/check_',
    '/get_',
    '/send_message',
    '/widget/',
    '/uploads/',
    '/socket.io',
    '/user/my_info',
)
_NON_PAGE_PARTS = (
    '/api/',
    '/thumb/',
    '/attachment/',
    '/download/',
    '/weblink-file/',
)
_NON_PAGE_ENDPOINT_PARTS = (
    '.api_',
    '.get_',
    '.serve_',
    '.download_',
    '.widget_',
    '.bootstrap',
    '.campaign_status',
    '.get_status',
)
_NON_PAGE_ENDPOINT_PREFIXES = (
    'api_',
    'get_',
    'serve_',
    'download_',
    'check_',
)
_FILE_EXTENSIONS = (
    '.avif', '.bmp', '.css', '.csv', '.doc', '.docx', '.gif', '.ico', '.jpeg',
    '.jpg', '.js', '.json', '.mp3', '.mp4', '.pdf', '.png', '.svg', '.webp',
    '.xls', '.xlsx', '.xml', '.zip',
)


def _client_ip(req):
    return req.headers.get('X-Forwarded-For', req.remote_addr or '').split(',')[0].strip()


def _cleanup_if_due(conn):
    today = conn.execute("SELECT DATE('now', '+9 hours')").fetchone()[0]
    row = conn.execute(
        "SELECT value FROM admin_settings WHERE key='usage_cleanup_date'"
    ).fetchone()
    if row and row['value'] == today:
        return

    conn.execute(
        f"DELETE FROM usage_logs WHERE created_at < DATETIME('now', '-{RETENTION_MONTHS} months')"
    )
    conn.execute(
        f"DELETE FROM login_activity WHERE created_at < DATETIME('now', '-{RETENTION_MONTHS} months')"
    )
    conn.execute("DELETE FROM usage_page_sessions WHERE last_seen < DATETIME('now', '-1 day')")
    conn.execute('''
        INSERT INTO admin_settings (key, value, updated_at)
        VALUES ('usage_cleanup_date', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=CURRENT_TIMESTAMP
    ''', (today,))


def _is_page_navigation(req):
    path = (req.path or '').lower()
    endpoint = (req.endpoint or '').lower()
    if req.method != 'GET' or not path or path.startswith('/static/'):
        return False
    if path.startswith(_NON_PAGE_PREFIXES) or any(part in path for part in _NON_PAGE_PARTS):
        return False
    if path.endswith(_FILE_EXTENSIONS):
        return False
    if endpoint.startswith(_NON_PAGE_ENDPOINT_PREFIXES) or any(part in endpoint for part in _NON_PAGE_ENDPOINT_PARTS):
        return False
    if req.headers.get('X-Requested-With', '').lower() == 'xmlhttprequest':
        return False

    fetch_destination = req.headers.get('Sec-Fetch-Dest', '').lower()
    if fetch_destination and fetch_destination not in ('document', 'iframe'):
        return False

    accepted = req.headers.get('Accept', '').lower()
    if accepted and 'text/html' not in accepted and '*/*' not in accepted:
        return False
    return True


def start_usage_session(session):
    session['_usage_session_id'] = secrets.token_urlsafe(18)


def record_page_usage(req, session, menu_name):
    if not _is_page_navigation(req):
        return False

    session_id = session.get('_usage_session_id')
    if not session_id:
        start_usage_session(session)
        session_id = session['_usage_session_id']

    emp_no = str(session.get('emp_no') or session.get('user_name') or 'unknown')
    user_name = str(session.get('user_name') or emp_no)
    path = req.path or '/'
    if path != '/':
        path = path.rstrip('/') or '/'

    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        duplicate = conn.execute(f'''
            SELECT 1
            FROM usage_page_sessions
            WHERE session_id=?
              AND emp_no=?
              AND path=?
              AND last_seen >= DATETIME('now', '-{PAGE_DEDUPE_SECONDS} seconds')
        ''', (session_id, emp_no, path)).fetchone()

        conn.execute('''
            INSERT INTO usage_page_sessions (session_id, emp_no, path, last_seen)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                emp_no=excluded.emp_no,
                path=excluded.path,
                last_seen=CURRENT_TIMESTAMP
        ''', (session_id, emp_no, path))

        if duplicate:
            _cleanup_if_due(conn)
            conn.commit()
            return False

        conn.execute('''
            INSERT INTO usage_logs (
                emp_no, user_name, menu_name, endpoint, path, method,
                ip_address, session_id
            ) VALUES (?, ?, ?, ?, ?, 'GET', ?, ?)
        ''', (
            emp_no, user_name, menu_name, req.endpoint, path,
            _client_ip(req), session_id
        ))
        conn.execute('''
            INSERT INTO usage_user_totals (
                emp_no, user_name, access_count, first_used, last_used, updated_at
            ) VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(emp_no) DO UPDATE SET
                user_name=excluded.user_name,
                access_count=usage_user_totals.access_count + 1,
                first_used=COALESCE(usage_user_totals.first_used, CURRENT_TIMESTAMP),
                last_used=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
        ''', (emp_no, user_name))
        conn.execute('''
            INSERT INTO usage_user_menu_totals (
                emp_no, user_name, menu_name, access_count, first_used, last_used
            ) VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(emp_no, menu_name) DO UPDATE SET
                user_name=excluded.user_name,
                access_count=usage_user_menu_totals.access_count + 1,
                first_used=COALESCE(usage_user_menu_totals.first_used, CURRENT_TIMESTAMP),
                last_used=CURRENT_TIMESTAMP
        ''', (emp_no, user_name, menu_name))
        _cleanup_if_due(conn)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_login_activity(req, session, action):
    if action not in ('login', 'logout') or not session.get('emp_no'):
        return

    emp_no = str(session.get('emp_no'))
    user_name = str(session.get('user_name') or emp_no)
    login_increment = 1 if action == 'login' else 0
    logout_increment = 1 if action == 'logout' else 0
    last_login = 'CURRENT_TIMESTAMP' if action == 'login' else 'NULL'
    last_logout = 'CURRENT_TIMESTAMP' if action == 'logout' else 'NULL'

    conn = get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''
            INSERT INTO login_activity (emp_no, user_name, action, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            emp_no, user_name, action, _client_ip(req),
            req.headers.get('User-Agent', '')[:255]
        ))
        conn.execute(f'''
            INSERT INTO usage_user_totals (
                emp_no, user_name, login_count, logout_count,
                last_login, last_logout, updated_at
            ) VALUES (?, ?, ?, ?, {last_login}, {last_logout}, CURRENT_TIMESTAMP)
            ON CONFLICT(emp_no) DO UPDATE SET
                user_name=excluded.user_name,
                login_count=usage_user_totals.login_count + excluded.login_count,
                logout_count=usage_user_totals.logout_count + excluded.logout_count,
                last_login=CASE
                    WHEN excluded.login_count=1 THEN CURRENT_TIMESTAMP
                    ELSE usage_user_totals.last_login
                END,
                last_logout=CASE
                    WHEN excluded.logout_count=1 THEN CURRENT_TIMESTAMP
                    ELSE usage_user_totals.last_logout
                END,
                updated_at=CURRENT_TIMESTAMP
        ''', (emp_no, user_name, login_increment, logout_increment))
        _cleanup_if_due(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
