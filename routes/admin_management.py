from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, session, url_for
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime

from .database import BASE_DIR, GALLERY_ROOT, PROFILE_ROOT, SCHOOL_UPLOADS, get_db
from .security import hash_password, is_admin_session
from .storage import (
    AI_MAIL_UPLOADS,
    APP_ROOT,
    BOARD_UPLOADS,
    CHAT_UPLOADS,
    CONTRACTS_ROOT,
    DATA_ROOT,
    DEPOSIT_UPLOADS,
    EBOOK_UPLOADS,
    GALL2_ROOT,
    LEGACY_ARCHIVE_ROOT,
    MANUAL_UPLOADS,
    MEMO_UPLOADS,
    UPLOADS_ROOT,
    VERIFIED_CONTRACT_ROOT,
    delete_storage_target,
)
from .menu_access import (
    MENU_CATALOG,
    MENU_GROUPS,
    SCHOOL_DIRECTOR_SCOPE_SETTING,
    ensure_menu_access_schema,
    load_menu_max_levels,
    school_director_scope_enabled,
)

admin_bp = Blueprint('admin', __name__)

TRACKABLE_USAGE_SQL = '''
    (
        session_id IS NOT NULL
        OR (
            session_id IS NULL
            AND method='GET'
            AND COALESCE(path, '') <> ''
            AND path NOT LIKE '/api/%'
            AND path NOT LIKE '%/api/%'
            AND path NOT LIKE '/widget/%'
            AND path NOT LIKE '/uploads/%'
            AND path NOT LIKE '%/thumb/%'
            AND path NOT LIKE '%/attachment/%'
            AND path NOT LIKE '%/download/%'
            AND path NOT LIKE '%/file/%'
            AND path NOT LIKE '%/weblink-file/%'
            AND path NOT LIKE '%/center-weblink-file/%'
            AND path NOT LIKE '/get_%'
            AND path NOT LIKE '/check_%'
            AND path <> '/user/my_info'
            AND LOWER(path) NOT LIKE '%.ico'
            AND LOWER(path) NOT LIKE '%.jpg'
            AND LOWER(path) NOT LIKE '%.jpeg'
            AND LOWER(path) NOT LIKE '%.png'
            AND LOWER(path) NOT LIKE '%.gif'
            AND LOWER(path) NOT LIKE '%.svg'
            AND LOWER(path) NOT LIKE '%.webp'
            AND LOWER(path) NOT LIKE '%.css'
            AND LOWER(path) NOT LIKE '%.js'
            AND LOWER(path) NOT LIKE '%.json'
            AND LOWER(path) NOT LIKE '%.pdf'
            AND LOWER(path) NOT LIKE '%.xlsx'
            AND COALESCE(endpoint, '') NOT LIKE 'api_%'
            AND COALESCE(endpoint, '') NOT LIKE 'get_%'
            AND COALESCE(endpoint, '') NOT LIKE 'serve_%'
            AND COALESCE(endpoint, '') NOT LIKE 'download_%'
            AND COALESCE(endpoint, '') NOT LIKE 'check_%'
            AND COALESCE(endpoint, '') NOT LIKE '%.api_%'
            AND COALESCE(endpoint, '') NOT LIKE '%.get_%'
            AND COALESCE(endpoint, '') NOT LIKE '%.serve_%'
            AND COALESCE(endpoint, '') NOT LIKE '%.download_%'
            AND COALESCE(endpoint, '') NOT LIKE '%.widget_%'
            AND COALESCE(endpoint, '') NOT LIKE '%.bootstrap'
            AND COALESCE(endpoint, '') NOT LIKE '%.%status%'
        )
    )
'''


THEME_CATEGORY_NAMES = {'custom', 'gallery', 'accent', 'deep-color', 'seasonal', 'default'}
DEFAULT_THEME_PAGE_BACKGROUND = '#f5f6f8'
THEME_VAR_KEYS = {
    '--body-bg', '--app-bg', '--main-bg', '--nav-bg', '--primary-color', '--primary-light',
    '--primary-dark', '--text-dark', '--text-gray', '--border-color', '--border-light',
    '--card-bg', '--card-border', '--card-shadow', '--card-backdrop', '--input-bg',
    '--input-text', '--widget-bg', '--widget-hover', '--widget-border', '--widget-border-color',
    '--theme-line-color', '--dashboard-top-line', '--nav-shadow',
    '--tooltip-bg', '--tooltip-text', '--effect-color1', '--effect-color2', '--effect-color3',
}


def _normalize_default_theme_background(theme):
    """기존에 저장된 기본/e리플렛 테마도 최신 공통 변수로 보정한다."""
    if not isinstance(theme, dict):
        return theme
    normalized = dict(theme)
    theme_vars = dict(normalized.get('vars') or {})
    is_eleaflet_theme = (
        str(theme.get('key') or '').strip().lower() == 'gallery:eleaflet'
        or str(theme.get('name') or '').strip() == 'e리플렛테마'
    )
    if is_eleaflet_theme:
        theme_vars['--nav-shadow'] = 'none'
        theme_vars['--theme-line-color'] = '#e1e7e2'
        normalized['vars'] = theme_vars
        return normalized

    is_default_theme = (
        str(theme.get('catalog') or '').strip().lower() == 'default'
        or str(theme.get('key') or '').strip().lower() == 'default:0'
        or str(theme.get('name') or '').strip() == '[기본] 테마없음'
    )
    if not is_default_theme:
        return theme

    for key in ('--body-bg', '--app-bg', '--main-bg'):
        theme_vars[key] = DEFAULT_THEME_PAGE_BACKGROUND
    normalized['vars'] = theme_vars
    return normalized


ADMIN_TABS = [
    ('people', '인사관리', 'fa-user-gear', '/user'),
    ('menu_permissions', '메뉴 권한관리', 'fa-key', '/admin/menu-permissions'),
    ('boards', '게시판관리', 'fa-clipboard-list', '/admin/boards'),
    ('disk', '디스크관리', 'fa-hard-drive', '/admin/disk'),
    ('themes', '테마관리', 'fa-palette', '/admin/theme'),
    ('stats', '이용통계', 'fa-chart-line', '/admin/stats'),
    ('settings', 'Admin설정', 'fa-user-shield', '/admin/settings'),
]


def is_admin_level():
    return is_admin_session()


def require_admin():
    if not session.get('emp_no'):
        abort(401)
    if not is_admin_level():
        abort(403)


def get_active_theme():
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM admin_settings WHERE key='active_theme'").fetchone()
        conn.close()
        if not row or not row['value']:
            return None
        return _normalize_default_theme_background(json.loads(row['value']))
    except Exception:
        return None


def _set_setting(key, value):
    conn = get_db()
    conn.execute('''
        INSERT INTO admin_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
    ''', (key, value))
    conn.commit()
    conn.close()


def _theme_owner():
    return str(session.get('emp_no') or session.get('user_name') or 'admin')


def _clean_theme_vars(raw_vars):
    if not isinstance(raw_vars, dict):
        return {}
    cleaned = {}
    for key, value in raw_vars.items():
        if key not in THEME_VAR_KEYS or not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if not text or len(text) > 600 or '</' in text.lower() or 'javascript:' in text.lower():
            continue
        cleaned[key] = text
    return cleaned


def _clean_theme_effect(value):
    effect = str(value or 'blobs').strip()
    return effect if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,48}', effect) else 'blobs'


def _custom_theme_dict(row):
    try:
        vars_data = json.loads(row['vars_json'] or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        vars_data = {}
    return {
        'id': row['id'],
        'key': f"custom:{row['id']}",
        'name': row['name'],
        'type': row['effect'] or 'blobs',
        'effect': row['effect'] or 'blobs',
        'category': row['category'] or 'custom',
        'catalog': 'custom',
        'vars': vars_data,
        'isCustom': True,
        'created_at': row['created_at'],
        'updated_at': row['updated_at'] if 'updated_at' in row.keys() else row['created_at'],
    }


def _format_size(size):
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == 'B' else f"{value:.2f} {unit}"
        value /= 1024


def _folder_size(path):
    total = 0
    count = 0
    if not os.path.exists(path):
        return 0, 0
    if os.path.isfile(path):
        return os.path.getsize(path), 1
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            fp = os.path.join(root, name)
            if not os.path.islink(fp) and os.path.exists(fp):
                try:
                    total += os.path.getsize(fp)
                    count += 1
                except OSError:
                    pass
    return total, count


def _storage_roots():
    data_root = str(DATA_ROOT)
    roots = [
        {'key': 'approval_expense', 'label': '사내결재·지출결의', 'path': str(UPLOADS_ROOT), 'icon': 'fa-file-signature'},
        {'key': 'board', 'label': '게시판·자료실·업무메뉴얼', 'path': str(BOARD_UPLOADS), 'icon': 'fa-clipboard-list'},
        {'key': 'messenger', 'label': '사내메신저', 'path': str(CHAT_UPLOADS), 'icon': 'fa-comments'},
        {'key': 'memo', 'label': '개인화이트보드', 'path': str(MEMO_UPLOADS), 'icon': 'fa-chalkboard'},
        {'key': 'school', 'label': '학교업무메뉴', 'path': SCHOOL_UPLOADS, 'icon': 'fa-school'},
        {'key': 'certificate', 'label': '증명발급', 'path': os.path.join(data_root, 'output_pdfs'), 'icon': 'fa-file-invoice'},
        {'key': 'certificate_logos', 'label': '증명서 로고', 'path': os.path.join(data_root, 'certificate_logos'), 'icon': 'fa-image'},
        {'key': 'certificate_seals', 'label': '증명서 직인', 'path': os.path.join(data_root, 'certificate_seals'), 'icon': 'fa-stamp'},
        {'key': 'contract', 'label': '전자계약', 'path': str(CONTRACTS_ROOT), 'icon': 'fa-file-contract'},
        {'key': 'verified_contract', 'label': '인증전자계약', 'path': str(VERIFIED_CONTRACT_ROOT), 'icon': 'fa-file-signature'},
        {'key': 'gallery', 'label': '갤러리', 'path': GALLERY_ROOT, 'icon': 'fa-images'},
        {'key': 'gall2', 'label': '사내 갤러리', 'path': str(GALL2_ROOT), 'icon': 'fa-photo-film'},
        {'key': 'ai_mail', 'label': '스마트 메일 발송', 'path': str(AI_MAIL_UPLOADS), 'icon': 'fa-wand-magic-sparkles'},
        {'key': 'ebook', 'label': 'e리플렛·eBook', 'path': str(EBOOK_UPLOADS), 'icon': 'fa-book-open-reader'},
        {'key': 'manual', 'label': '신규 업무메뉴얼', 'path': str(MANUAL_UPLOADS), 'icon': 'fa-book'},
        {'key': 'profiles', 'label': '인사/프로필', 'path': PROFILE_ROOT, 'icon': 'fa-id-card'},
        {'key': 'deposit', 'label': '입금용 엑셀 생성기', 'path': str(DEPOSIT_UPLOADS), 'icon': 'fa-file-excel'},
        {'key': 'company_stamps', 'label': '회사 직인', 'path': os.path.join(data_root, 'company_stamps'), 'icon': 'fa-stamp'},
        {'key': 'legacy', 'label': '이전 데이터 보관', 'path': str(LEGACY_ARCHIVE_ROOT), 'icon': 'fa-box-archive'},
        {'key': 'app', 'label': '앱 루트', 'path': BASE_DIR, 'icon': 'fa-folder-tree'},
    ]

    # 새 기능이 전용 데이터 폴더를 만들면 디스크관리에도 자동으로 노출한다.
    registered_paths = {os.path.realpath(item['path']) for item in roots}
    if os.path.isdir(data_root):
        for name in sorted(os.listdir(data_root), key=str.lower):
            path = os.path.join(data_root, name)
            real_path = os.path.realpath(path)
            if not os.path.isdir(path) or real_path in registered_paths:
                continue
            if _is_sensitive_storage_target(path):
                continue
            safe_key = re.sub(r'[^a-z0-9_-]+', '-', name.lower()).strip('-') or 'storage'
            roots.insert(-1, {
                'key': f'auto-{safe_key}',
                'label': f'{name} (자동 발견)',
                'path': path,
                'icon': 'fa-folder-plus',
                'auto_discovered': True,
            })
            registered_paths.add(real_path)
    return roots


def _logical_storage_usage(conn):
    """DB 안에 직접 저장되는 메뉴 데이터와 소유자별 논리 용량을 집계한다."""
    features = {
        'approval_expense': {'label': '사내결재·지출결의', 'icon': 'fa-file-signature'},
        'messenger': {'label': '사내메신저', 'icon': 'fa-comments'},
        'memo': {'label': '개인화이트보드', 'icon': 'fa-chalkboard'},
        'board': {'label': '게시판·자료실·업무메뉴얼', 'icon': 'fa-clipboard-list'},
        'school': {'label': '학교업무메뉴', 'icon': 'fa-school'},
        'ai_mail': {'label': '스마트 메일 발송', 'icon': 'fa-wand-magic-sparkles'},
        'payroll': {'label': '스마트 명세서 발송', 'icon': 'fa-envelope-open-text'},
        'ebook': {'label': 'e리플렛·eBook', 'icon': 'fa-book-open-reader'},
        'manual': {'label': '신규 업무메뉴얼', 'icon': 'fa-book'},
        'contacts': {'label': '본사연락망', 'icon': 'fa-address-book'},
        'parent_notifications': {'label': '학부모알림전송', 'icon': 'fa-bell'},
    }
    usage = {
        key: {**meta, 'key': key, 'size': 0, 'count': 0, 'owners': {}}
        for key, meta in features.items()
    }

    def collect(feature_key, sql):
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error:
            return
        feature = usage[feature_key]
        for row in rows:
            owner = str(row['owner'] or '').strip() or '미분류/공용'
            size = int(row['size_bytes'] or 0)
            count = int(row['item_count'] or 0)
            feature['size'] += size
            feature['count'] += count
            owner_row = feature['owners'].setdefault(owner, {'size': 0, 'count': 0})
            owner_row['size'] += size
            owner_row['count'] += count

    payload = lambda column: f"LENGTH(CAST(COALESCE({column}, '') AS BLOB))"
    queries = {
        'approval_expense': [
            f"SELECT drafter owner, COUNT(*) item_count, SUM({payload('doc_data')}) size_bytes FROM approvals GROUP BY drafter",
            """SELECT r.drafter owner, COUNT(i.id) item_count,
                      COALESCE(SUM(LENGTH(CAST(COALESCE(i.description, '') AS BLOB))
                                 + LENGTH(CAST(COALESCE(i.vendor, '') AS BLOB))
                                 + LENGTH(CAST(COALESCE(i.note, '') AS BLOB))), 0) size_bytes
                   FROM expense_reports r LEFT JOIN expense_items i ON i.report_id=r.id
                  GROUP BY r.drafter""",
        ],
        'messenger': [
            f"SELECT sender owner, COUNT(*) item_count, SUM({payload('content')}) size_bytes FROM messages GROUP BY sender",
        ],
        'memo': [
            f"SELECT COALESCE(owner_key, owner) owner, COUNT(*) item_count, SUM({payload('content')}) size_bytes FROM memos GROUP BY COALESCE(owner_key, owner)",
            f"SELECT owner, COUNT(*) item_count, SUM({payload('content')}) size_bytes FROM whiteboard_memos GROUP BY owner",
        ],
        'board': [
            f"SELECT author owner, COUNT(*) item_count, SUM({payload('content')}) size_bytes FROM board_posts GROUP BY author",
        ],
        'school': [
            f"SELECT author owner, COUNT(*) item_count, SUM({payload('content')}) size_bytes FROM school_posts GROUP BY author",
            f"SELECT author owner, COUNT(*) item_count, SUM({payload('content')}) size_bytes FROM school_post_comments GROUP BY author",
        ],
        'ai_mail': [
            f"SELECT owner_emp_no owner, COUNT(*) item_count, SUM({payload('body_html')} + {payload('body_text')}) size_bytes FROM ai_mail_templates GROUP BY owner_emp_no",
            f"SELECT owner_emp_no owner, COUNT(*) item_count, SUM({payload('body_html')} + {payload('body_text')} + {payload('preflight_json')}) size_bytes FROM ai_mail_campaigns GROUP BY owner_emp_no",
        ],
        'payroll': [
            f"SELECT owner_emp_no owner, COUNT(*) item_count, SUM({payload('body_html')} + {payload('banner1_data')} + {payload('banner2_data')}) size_bytes FROM payroll_workgroups GROUP BY owner_emp_no",
            f"SELECT owner_emp_no owner, COUNT(*) item_count, SUM({payload('body_html')} + {payload('field_mappings_json')}) size_bytes FROM payroll_mail_templates GROUP BY owner_emp_no",
            f"SELECT owner_emp_no owner, COUNT(*) item_count, SUM({payload('source_value')}) size_bytes FROM payroll_image_assets GROUP BY owner_emp_no",
            "SELECT owner_emp_no owner, COUNT(*) item_count, COALESCE(SUM(LENGTH(statement_html_zlib)), 0) size_bytes FROM payroll_campaign_recipients GROUP BY owner_emp_no",
        ],
        'ebook': [
            f"SELECT created_by owner, COUNT(*) item_count, SUM({payload('content_text')}) size_bytes FROM ebooks GROUP BY created_by",
            f"""SELECT e.created_by owner, COUNT(*) item_count, SUM({payload('p.content_html')}) size_bytes
                    FROM ebook_pages p JOIN ebooks e ON e.id=p.ebook_id GROUP BY e.created_by""",
        ],
        'manual': [
            f"""SELECT m.created_by owner, COUNT(*) item_count, SUM({payload('s.content_html')}) size_bytes
                    FROM manual_sections s JOIN manuals m ON m.id=s.manual_id GROUP BY m.created_by""",
        ],
        'contacts': [
            f"SELECT owner_emp_no owner, COUNT(*) item_count, SUM({payload('settings_json')}) size_bytes FROM saved_contact_directories GROUP BY owner_emp_no",
        ],
        'parent_notifications': [
            f"SELECT created_by owner, COUNT(*) item_count, SUM({payload('title')} + {payload('body')}) size_bytes FROM parent_notifications GROUP BY created_by",
        ],
    }
    for feature_key, statements in queries.items():
        for statement in statements:
            collect(feature_key, statement)
    return usage


def _personal_storage_usage(conn, logical_usage, storage_roots):
    """파일 관계와 DB 소유자 컬럼을 이용해 개인별 저장공간을 계산한다."""
    try:
        user_rows = conn.execute('''
            SELECT emp_no, name, position, department, status
            FROM users ORDER BY name, emp_no
        ''').fetchall()
    except sqlite3.Error:
        user_rows = []

    people = {}
    emp_lookup = {}
    name_lookup = {}
    for row in user_rows:
        emp_no = str(row['emp_no'] or '').strip()
        name = str(row['name'] or '').strip() or emp_no or '이름 없음'
        key = f'emp:{emp_no.lower()}' if emp_no else f'name:{name.lower()}'
        people[key] = {
            'key': key,
            'emp_no': emp_no or '-',
            'name': name,
            'position': str(row['position'] or '').strip(),
            'department': str(row['department'] or '').strip(),
            'status': str(row['status'] or '').strip(),
            'file_bytes': 0,
            'file_count': 0,
            'db_bytes': 0,
            'db_items': 0,
        }
        if emp_no:
            emp_lookup[emp_no.lower()] = key
        if name:
            name_lookup.setdefault(name.lower(), []).append(key)

    def person_for(raw_owner):
        owner = str(raw_owner or '').strip()
        if (
            not owner
            or owner == '미분류/공용'
            or owner.lower() in {'system', '시스템', '시스템알림', '🔔시스템알림'}
        ):
            key = 'shared'
        elif owner.lower() in emp_lookup:
            key = emp_lookup[owner.lower()]
        elif len(name_lookup.get(owner.lower(), [])) == 1:
            key = name_lookup[owner.lower()][0]
        else:
            key = f'legacy:{owner.lower()}'
        if key not in people:
            people[key] = {
                'key': key,
                'emp_no': '-' if key == 'shared' else owner,
                'name': '미분류/공용' if key == 'shared' else owner,
                'position': '', 'department': '', 'status': '',
                'file_bytes': 0, 'file_count': 0, 'db_bytes': 0, 'db_items': 0,
            }
        return people[key]

    for feature in logical_usage.values():
        for owner, stats in feature['owners'].items():
            person = person_for(owner)
            person['db_bytes'] += stats['size']
            person['db_items'] += stats['count']

    physical_roots = [
        item for item in storage_roots
        if item['key'] != 'app' and os.path.isdir(item['path'])
    ]
    file_index = {}
    all_files = []
    for root_info in physical_roots:
        for walk_root, dirs, files in os.walk(root_info['path']):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(walk_root, d))]
            for filename in files:
                path = os.path.realpath(os.path.join(walk_root, filename))
                if os.path.islink(path) or _is_sensitive_storage_target(path):
                    continue
                file_index.setdefault(filename.lower(), []).append(path)
                all_files.append(path)

    seen_files = set()

    def resolve_reference(reference, fallback_root=None):
        text = str(reference or '').strip().strip('"').strip("'")
        if not text or text.startswith(('http://', 'https://', 'data:')):
            return None
        normalized = text.replace('\\', '/')
        if normalized.startswith('/mnt/data/'):
            candidate = os.path.join(BASE_DIR, normalized[len('/mnt/data/'):].replace('/', os.sep))
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)
        if normalized.startswith('/static/'):
            candidate = os.path.join(str(APP_ROOT), normalized.lstrip('/').replace('/', os.sep))
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)
        if os.path.isfile(text):
            return os.path.realpath(text)
        basename = os.path.basename(normalized)
        if fallback_root and basename:
            candidate = os.path.join(str(fallback_root), basename)
            if os.path.isfile(candidate):
                return os.path.realpath(candidate)
        matches = file_index.get(basename.lower(), []) if basename else []
        return matches[0] if len(matches) == 1 else None

    def add_file(owner, reference, fallback_root=None):
        path = resolve_reference(reference, fallback_root)
        if not path or path in seen_files:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        seen_files.add(path)
        person = person_for(owner)
        person['file_bytes'] += size
        person['file_count'] += 1

    def add_references(owner, references, fallback_root=None):
        for reference in references:
            for token in str(reference or '').split(','):
                add_file(owner, token, fallback_root)

    query_specs = [
        ("SELECT p.author owner, f.saved_name ref FROM board_files f JOIN board_posts p ON p.id=f.post_id", str(BOARD_UPLOADS)),
        ("SELECT sender owner, filepath ref FROM messages WHERE TRIM(COALESCE(filepath,''))<>''", str(CHAT_UPLOADS)),
        ("SELECT COALESCE(owner_key, owner) owner, filepath ref FROM memos WHERE TRIM(COALESCE(filepath,''))<>''", str(MEMO_UPLOADS)),
        ("SELECT drafter owner, filepath ref FROM approvals WHERE TRIM(COALESCE(filepath,''))<>''", str(UPLOADS_ROOT)),
        ("SELECT drafter owner, source_filepath ref FROM expense_reports WHERE TRIM(COALESCE(source_filepath,''))<>''", str(UPLOADS_ROOT)),
        ("SELECT drafter owner, receipt_filepath ref FROM expense_reports WHERE TRIM(COALESCE(receipt_filepath,''))<>''", str(UPLOADS_ROOT)),
        ("SELECT author owner, filepath ref FROM school_posts WHERE TRIM(COALESCE(filepath,''))<>''", SCHOOL_UPLOADS),
        ("SELECT author owner, filepath ref FROM school_post_comments WHERE TRIM(COALESCE(filepath,''))<>''", SCHOOL_UPLOADS),
        ("SELECT emp_no owner, profile_path ref FROM users WHERE TRIM(COALESCE(profile_path,''))<>''", PROFILE_ROOT),
        ("""SELECT c.owner_emp_no owner, a.filepath ref
                FROM ai_mail_campaign_attachments a JOIN ai_mail_campaigns c ON c.id=a.campaign_id""", str(AI_MAIL_UPLOADS)),
        ("""SELECT t.owner_emp_no owner, a.filepath ref
                FROM ai_mail_template_assets a JOIN ai_mail_templates t ON t.id=a.template_id""", str(AI_MAIL_UPLOADS)),
        ("SELECT created_by owner, cover_path ref FROM ebooks WHERE TRIM(COALESCE(cover_path,''))<>''", str(EBOOK_UPLOADS)),
        ("""SELECT e.created_by owner, p.image_path ref
                FROM ebook_pages p JOIN ebooks e ON e.id=p.ebook_id
               WHERE TRIM(COALESCE(p.image_path,''))<>''""", str(EBOOK_UPLOADS)),
        ("SELECT created_by owner, filepath ref FROM ebook_media WHERE TRIM(COALESCE(filepath,''))<>''", str(EBOOK_UPLOADS)),
    ]
    for sql, fallback_root in query_specs:
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            add_references(row['owner'], [row['ref']], fallback_root)

    try:
        gallery_rows = conn.execute('SELECT filename, thumb_name FROM gallery').fetchall()
    except sqlite3.Error:
        gallery_rows = []
    for row in gallery_rows:
        add_file('', row['filename'], os.path.join(GALLERY_ROOT, 'uploads'))
        add_file('', row['thumb_name'], os.path.join(GALLERY_ROOT, 'thumbnails'))

    try:
        gall2_rows = conn.execute('''
            SELECT p.author owner, g.filename, g.thumb_name
            FROM gall2 g LEFT JOIN gall2_posts p ON p.id=g.post_id
        ''').fetchall()
    except sqlite3.Error:
        gall2_rows = []
    for row in gall2_rows:
        add_file(row['owner'], row['filename'], os.path.join(str(GALL2_ROOT), 'uploads'))
        add_file(row['owner'], row['thumb_name'], os.path.join(str(GALL2_ROOT), 'thumbnails'))

    try:
        contract_rows = conn.execute('SELECT created_by owner, signature_filename, pdf_filename FROM verified_contracts').fetchall()
    except sqlite3.Error:
        contract_rows = []
    for row in contract_rows:
        add_file(row['owner'], row['signature_filename'], os.path.join(str(VERIFIED_CONTRACT_ROOT), 'signatures'))
        add_file(row['owner'], row['pdf_filename'], os.path.join(str(VERIFIED_CONTRACT_ROOT), 'completed'))

    try:
        manual_rows = conn.execute('''
            SELECT m.created_by owner, i.manual_id, i.filename
            FROM manual_images i JOIN manuals m ON m.id=i.manual_id
        ''').fetchall()
    except sqlite3.Error:
        manual_rows = []
    for row in manual_rows:
        add_file(row['owner'], row['filename'], os.path.join(str(MANUAL_UPLOADS), str(row['manual_id'])))

    # 관계가 없는 과거 파일도 총량에서 사라지지 않도록 공용 사용량에 포함한다.
    for path in all_files:
        if path not in seen_files:
            add_file('', path)

    rows = []
    for person in people.values():
        person['total_bytes'] = person['file_bytes'] + person['db_bytes']
        person['file_size_text'] = _format_size(person['file_bytes'])
        person['db_size_text'] = _format_size(person['db_bytes'])
        person['total_size_text'] = _format_size(person['total_bytes'])
        rows.append(person)
    rows.sort(key=lambda row: (-row['total_bytes'], row['name'], row['emp_no']))
    return rows


def _is_sensitive_storage_target(path):
    """암호화 키와 운영 DB는 디스크 관리 UI에서 열람·다운로드·삭제하지 않는다."""
    target = os.path.realpath(path)
    security_root = os.path.realpath(os.path.join(BASE_DIR, 'security'))
    try:
        if os.path.commonpath([security_root, target]) == security_root:
            return True
    except ValueError:
        return True
    return os.path.basename(target).lower() in {'saedam.db', 'contracts.db'}


def _root_by_key(root_key):
    roots = {item['key']: item for item in _storage_roots()}
    return roots.get(root_key) or roots['app']


def _safe_target(root_info, rel_path=''):
    root = os.path.realpath(root_info['path'])
    target = os.path.realpath(os.path.join(root, rel_path or ''))
    try:
        if os.path.commonpath([root, target]) != root:
            abort(403)
    except ValueError:
        abort(403)
    return root, target


def _menu_usage_label(path):
    if not path:
        return '기타'
    mapping = [
        ('/admin', '통합관리'), ('/user', '인사관리'), ('/board', '게시판'), ('/chat', '사내메신저'),
        ('/chat_popup', '사내메신저'), ('/school', '학교업무메뉴'), ('/document', '증명발급'),
        ('/contract', '계약시스템'), ('/gall2', '갤러리'), ('/gallery', '갤러리'), ('/approval', '사내결재'),
        ('/expense', '지출결의'), ('/ai-mail', 'AI메일전송'), ('/payroll', '급여/업무지원'), ('/attendance', '근태관리'),
        ('/contacts', '본사연락망'), ('/memo', '개인화이트보드'), ('/excel-generator', '입금용 엑셀 생성기'),
        ('/ebook/books', 'eBook'), ('/ebook', 'e리플렛'),
    ]
    if path == '/':
        return '메인메뉴'
    for prefix, label in mapping:
        if path.startswith(prefix):
            return label
    return '기타'


def _render(section, **context):
    context.update(admin_tabs=ADMIN_TABS, active_section=section, active_theme=get_active_theme())
    return render_template('admin_management.html', **context)


@admin_bp.route('/')
def index():
    require_admin()
    return redirect(url_for('admin.boards'))


@admin_bp.route('/menu-permissions', methods=['GET', 'POST'])
def menu_permissions():
    require_admin()
    conn = get_db()
    try:
        ensure_menu_access_schema(conn)
        if request.method == 'POST':
            updates = {}
            for menu_key in MENU_CATALOG:
                raw_value = request.form.get(menu_key)
                try:
                    max_level = int(raw_value)
                except (TypeError, ValueError):
                    return f'잘못된 메뉴 권한 값입니다: {menu_key}', 400
                if max_level < -1 or max_level > 99:
                    return f'메뉴 권한 레벨은 -1~99 범위여야 합니다: {menu_key}', 400
                updates[menu_key] = max_level

            updated_by = str(session.get('emp_no') or session.get('user_name') or 'admin')
            conn.executemany('''
                INSERT INTO menu_access_permissions (menu_key, max_level, updated_by, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(menu_key) DO UPDATE SET
                    max_level=excluded.max_level,
                    updated_by=excluded.updated_by,
                    updated_at=CURRENT_TIMESTAMP
            ''', ((key, value, updated_by) for key, value in updates.items()))
            director_scope_enabled = request.form.get('school_director_scope_enabled') == '1'
            conn.execute('''
                INSERT INTO admin_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=CURRENT_TIMESTAMP
            ''', (
                SCHOOL_DIRECTOR_SCOPE_SETTING,
                '1' if director_scope_enabled else '0',
            ))
            conn.commit()
            return redirect(url_for('admin.menu_permissions', saved=1))

        max_levels = load_menu_max_levels(conn)
        position_rows = conn.execute('''
            SELECT p.level,
                   GROUP_CONCAT(p.name, ', ') AS position_names,
                   (
                       SELECT COUNT(*)
                       FROM users u
                       WHERE u.level=p.level
                         AND u.status='승인'
                         AND LOWER(TRIM(COALESCE(u.emp_no, ''))) <> 'admin'
                   ) AS user_count
            FROM hr_positions p
            GROUP BY p.level
            ORDER BY p.level ASC
        ''').fetchall()
        positions_by_level = {
            int(row['level']): {
                'names': row['position_names'] or '',
                'user_count': int(row['user_count'] or 0),
            }
            for row in position_rows
        }
        configured_levels = set(max_levels.values())
        level_numbers = sorted(
            set(range(0, 15)) | set(positions_by_level) | configured_levels | {99}
        )
        return _render(
            'menu_permissions',
            menu_groups=MENU_GROUPS,
            menu_max_levels=max_levels,
            level_numbers=level_numbers,
            positions_by_level=positions_by_level,
            school_director_scope=school_director_scope_enabled(conn),
            saved=request.args.get('saved') == '1',
        )
    finally:
        conn.close()


@admin_bp.route('/boards')
def boards():
    require_admin()
    try:
        from .board import init_board_db
        init_board_db()
    except Exception:
        pass

    conn = get_db()
    boards_data = conn.execute('''
        SELECT
            c.*,
            (SELECT COUNT(*) FROM board_posts p WHERE p.board_en = c.name_en) AS post_count,
            (SELECT COALESCE(SUM(p.views), 0) FROM board_posts p WHERE p.board_en = c.name_en) AS view_count,
            (SELECT COUNT(*)
             FROM board_comments cm
             JOIN board_posts p ON p.id = cm.post_id
             WHERE p.board_en = c.name_en) AS comment_count,
            (SELECT COALESCE(SUM(f.file_size), 0)
             FROM board_files f
             JOIN board_posts p ON p.id = f.post_id
             WHERE p.board_en = c.name_en) AS file_size
        FROM board_config c
        ORDER BY c.id ASC
    ''').fetchall()
    conn.close()
    return _render('boards', boards=boards_data, format_size=_format_size)


@admin_bp.route('/boards/create', methods=['POST'])
def create_board():
    require_admin()
    payload = request.get_json(silent=True) or request.form
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO board_config (name_en, name_kr, desc_text, lvl_access, lvl_read, lvl_write, lvl_delete, lvl_comment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            payload.get('name_en', '').strip(),
            payload.get('name_kr', '').strip(),
            payload.get('desc_text', '').strip(),
            int(payload.get('lvl_access', 10)),
            int(payload.get('lvl_read', 10)),
            int(payload.get('lvl_write', 2)),
            int(payload.get('lvl_delete', 2)),
            int(payload.get('lvl_comment', 10)),
        ))
        conn.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 400
    finally:
        conn.close()


@admin_bp.route('/boards/<int:board_id>/permissions', methods=['POST'])
def update_board_permissions(board_id):
    require_admin()
    data = request.form
    conn = get_db()
    conn.execute('''
        UPDATE board_config
        SET name_kr=?, desc_text=?, lvl_access=?, lvl_read=?, lvl_write=?, lvl_delete=?, lvl_comment=?
        WHERE id=?
    ''', (
        data.get('name_kr', '').strip(),
        data.get('desc_text', '').strip(),
        int(data.get('lvl_access', 10)),
        int(data.get('lvl_read', 10)),
        int(data.get('lvl_write', 2)),
        int(data.get('lvl_delete', 2)),
        int(data.get('lvl_comment', 10)),
        board_id,
    ))
    conn.commit()
    conn.close()
    return redirect(url_for('admin.boards'))


@admin_bp.route('/boards/<int:board_id>/delete', methods=['POST'])
def delete_board(board_id):
    require_admin()
    conn = get_db()
    try:
        board = conn.execute("SELECT name_en, name_kr FROM board_config WHERE id=?", (board_id,)).fetchone()
        if not board:
            return jsonify({'status': 'error', 'message': '게시판을 찾을 수 없습니다.'}), 404

        from .board import UPLOAD_FOLDER

        file_rows = conn.execute('''
            SELECT f.saved_name
            FROM board_files f
            JOIN board_posts p ON p.id = f.post_id
            WHERE p.board_en=?
        ''', (board['name_en'],)).fetchall()

        for file_row in file_rows:
            file_path = os.path.abspath(os.path.join(UPLOAD_FOLDER, file_row['saved_name']))
            upload_root = os.path.abspath(UPLOAD_FOLDER)
            if os.path.commonpath([upload_root, file_path]) == upload_root and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass

        conn.execute("DELETE FROM board_comments WHERE post_id IN (SELECT id FROM board_posts WHERE board_en=?)", (board['name_en'],))
        conn.execute("DELETE FROM board_files WHERE post_id IN (SELECT id FROM board_posts WHERE board_en=?)", (board['name_en'],))
        conn.execute("DELETE FROM board_posts WHERE board_en=?", (board['name_en'],))
        conn.execute("DELETE FROM board_config WHERE id=?", (board_id,))
        conn.commit()
        return jsonify({'status': 'success', 'message': f"{board['name_kr']} 게시판이 삭제되었습니다."})
    except Exception as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        conn.close()


@admin_bp.route('/disk')
def disk():
    require_admin()
    root_key = request.args.get('root', 'app')
    rel_path = request.args.get('path', '')
    root_info = _root_by_key(root_key)
    root, target = _safe_target(root_info, rel_path)
    if _is_sensitive_storage_target(target):
        abort(403)

    storage_roots = _storage_roots()
    conn = get_db()
    try:
        logical_usage = _logical_storage_usage(conn)
        personal_usage = _personal_storage_usage(conn, logical_usage, storage_roots)
    finally:
        conn.close()

    roots = []
    for item in storage_roots:
        size, count = _folder_size(item['path'])
        logical = logical_usage.get(item['key'], {})
        db_size = int(logical.get('size') or 0)
        db_count = int(logical.get('count') or 0)
        row = dict(item)
        row.update(
            size=size + db_size,
            physical_size=size,
            db_size=db_size,
            size_text=_format_size(size + db_size),
            count=count + db_count,
            file_count=count,
            db_count=db_count,
            exists=os.path.exists(item['path']),
            browseable=True,
        )
        roots.append(row)

    physical_keys = {item['key'] for item in roots}
    for key, logical in logical_usage.items():
        if key in physical_keys:
            continue
        roots.insert(-1, {
            'key': key,
            'label': logical['label'],
            'icon': logical['icon'],
            'path': None,
            'size': logical['size'],
            'physical_size': 0,
            'db_size': logical['size'],
            'size_text': _format_size(logical['size']),
            'count': logical['count'],
            'file_count': 0,
            'db_count': logical['count'],
            'exists': True,
            'browseable': False,
        })

    files = []
    parent_path = None
    if os.path.exists(target) and os.path.isdir(target):
        for name in os.listdir(target):
            path = os.path.join(target, name)
            if _is_sensitive_storage_target(path):
                continue
            try:
                stat = os.stat(path)
                is_dir = os.path.isdir(path)
                child_rel = os.path.relpath(path, root).replace('\\', '/')
                files.append({
                    'name': name,
                    'is_dir': is_dir,
                    'size': '-' if is_dir else _format_size(stat.st_size),
                    'mtime': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M'),
                    'rel_path': child_rel,
                })
            except OSError:
                pass
        files.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        if os.path.abspath(target) != os.path.abspath(root):
            parent_path = os.path.dirname(rel_path).replace('\\', '/')

    total, used, free = shutil.disk_usage(BASE_DIR)
    disk_stats = {
        'total': _format_size(total),
        'used': _format_size(used),
        'free': _format_size(free),
        'percent': round((used / total) * 100, 1) if total else 0,
    }
    personal_totals = {
        'users': sum(1 for row in personal_usage if row['key'].startswith('emp:')),
        'total': sum(row['total_bytes'] for row in personal_usage),
        'total_text': _format_size(sum(row['total_bytes'] for row in personal_usage)),
        'shared': next((row['total_bytes'] for row in personal_usage if row['key'] == 'shared'), 0),
        'shared_text': _format_size(next((row['total_bytes'] for row in personal_usage if row['key'] == 'shared'), 0)),
    }
    return _render(
        'disk',
        roots=roots,
        selected_root=root_info,
        current_path=rel_path,
        parent_path=parent_path,
        target_exists=os.path.exists(target),
        files=files,
        disk_stats=disk_stats,
        personal_usage=personal_usage,
        personal_totals=personal_totals,
    )


@admin_bp.route('/disk/download')
def disk_download():
    require_admin()
    root_info = _root_by_key(request.args.get('root', 'app'))
    root, target = _safe_target(root_info, request.args.get('path', ''))
    if _is_sensitive_storage_target(target):
        abort(403)
    if not os.path.isfile(target):
        abort(404)
    return send_file(target, as_attachment=True, download_name=os.path.basename(target))


@admin_bp.route('/disk/delete', methods=['POST'])
def disk_delete():
    require_admin()
    root_info = _root_by_key(request.form.get('root', 'app'))
    root, target = _safe_target(root_info, request.form.get('path', ''))
    if _is_sensitive_storage_target(target):
        abort(403)
    if os.path.abspath(root) == os.path.abspath(target):
        return jsonify({'status': 'error', 'message': '최상위 폴더는 삭제할 수 없습니다.'}), 400
    if not os.path.exists(target):
        return jsonify({'status': 'error', 'message': '파일을 찾을 수 없습니다.'}), 404
    try:
        delete_storage_target(target)
    except OSError as exc:
        return jsonify({
            'status': 'error',
            'message': f'삭제하지 못했습니다: {exc}',
        }), 500
    return jsonify({'status': 'success', 'message': '파일을 삭제했습니다.'})


@admin_bp.route('/themes')
def themes():
    require_admin()
    conn = get_db()
    custom_themes = conn.execute("SELECT * FROM custom_themes ORDER BY id DESC").fetchall()
    conn.close()
    return _render('themes', custom_themes=custom_themes)


@admin_bp.route('/theme')
def theme_gallery():
    require_admin()
    conn = get_db()
    custom_rows = conn.execute(
        "SELECT * FROM custom_themes WHERE enabled=1 ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    preference_rows = conn.execute('''
        SELECT theme_key, is_favorite, is_hidden
        FROM theme_catalog_preferences
        WHERE owner_emp_no=?
    ''', (_theme_owner(),)).fetchall()
    conn.close()
    preferences = {
        row['theme_key']: {
            'is_favorite': bool(row['is_favorite']),
            'is_hidden': bool(row['is_hidden']),
        }
        for row in preference_rows
    }
    return render_template(
        'theme.html',
        custom_themes=[_custom_theme_dict(row) for row in custom_rows],
        theme_preferences=preferences,
    )


@admin_bp.route('/themes/apply', methods=['POST'])
def apply_theme():
    require_admin()
    data = request.get_json(silent=True) or {}
    theme = {
        'key': str(data.get('key') or '')[:120],
        'name': str(data.get('name') or '사용자 테마')[:160],
        'index': data.get('index'),
        'catalog': str(data.get('catalog') or '')[:40],
        'effect': _clean_theme_effect(data.get('effect') or data.get('type')),
        'vars': _clean_theme_vars(data.get('vars')),
    }
    theme = _normalize_default_theme_background(theme)
    if not theme['vars']:
        return jsonify({'status': 'error', 'message': '테마 변수 정보가 없습니다.'}), 400
    _set_setting('active_theme', json.dumps(theme, ensure_ascii=False))
    return jsonify({'status': 'success'})


@admin_bp.route('/themes/clear', methods=['POST'])
def clear_theme():
    require_admin()
    _set_setting('active_theme', '')
    return jsonify({'status': 'success'})


@admin_bp.route('/themes/custom', methods=['POST'])
def add_custom_theme():
    require_admin()
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()[:160]
    vars_data = _clean_theme_vars(data.get('vars'))
    if not name or not vars_data:
        return jsonify({'status': 'error', 'message': '테마명과 변수 정보가 필요합니다.'}), 400
    category = str(data.get('category') or 'custom').strip()
    if category not in THEME_CATEGORY_NAMES:
        category = 'custom'
    conn = get_db()
    cursor = conn.execute('''
        INSERT INTO custom_themes (name, effect, category, vars_json, enabled, updated_at)
        VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
    ''', (name, _clean_theme_effect(data.get('effect')), category, json.dumps(vars_data, ensure_ascii=False)))
    conn.commit()
    saved = conn.execute("SELECT * FROM custom_themes WHERE id=?", (cursor.lastrowid,)).fetchone()
    conn.close()
    return jsonify({'status': 'success', 'message': '새 테마를 저장했습니다.', 'theme': _custom_theme_dict(saved)})


@admin_bp.route('/themes/custom/<int:theme_id>', methods=['PATCH', 'PUT'])
def update_custom_theme(theme_id):
    require_admin()
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()[:160]
    vars_data = _clean_theme_vars(data.get('vars'))
    if not name or not vars_data:
        return jsonify({'status': 'error', 'message': '테마명과 변수 정보가 필요합니다.'}), 400
    category = str(data.get('category') or 'custom').strip()
    if category not in THEME_CATEGORY_NAMES:
        category = 'custom'
    conn = get_db()
    current = conn.execute("SELECT id FROM custom_themes WHERE id=? AND enabled=1", (theme_id,)).fetchone()
    if not current:
        conn.close()
        return jsonify({'status': 'error', 'message': '수정할 테마를 찾을 수 없습니다.'}), 404
    conn.execute('''
        UPDATE custom_themes
        SET name=?, effect=?, category=?, vars_json=?, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
    ''', (
        name,
        _clean_theme_effect(data.get('effect')),
        category,
        json.dumps(vars_data, ensure_ascii=False),
        theme_id,
    ))
    conn.commit()
    saved = conn.execute("SELECT * FROM custom_themes WHERE id=?", (theme_id,)).fetchone()
    conn.close()
    return jsonify({'status': 'success', 'message': '테마를 수정했습니다.', 'theme': _custom_theme_dict(saved)})


@admin_bp.route('/themes/custom/<int:theme_id>/delete', methods=['POST'])
def delete_custom_theme(theme_id):
    require_admin()
    conn = get_db()
    theme = conn.execute("SELECT * FROM custom_themes WHERE id=?", (theme_id,)).fetchone()
    if not theme:
        conn.close()
        return jsonify({'status': 'error', 'message': '삭제할 테마를 찾을 수 없습니다.'}), 404
    conn.execute("DELETE FROM custom_themes WHERE id=?", (theme_id,))
    conn.execute(
        "DELETE FROM theme_catalog_preferences WHERE owner_emp_no=? AND theme_key=?",
        (_theme_owner(), f'custom:{theme_id}'),
    )
    cleared_active = False
    active_row = conn.execute("SELECT value FROM admin_settings WHERE key='active_theme'").fetchone()
    if active_row and active_row['value']:
        try:
            active_theme = json.loads(active_row['value'])
        except (TypeError, ValueError, json.JSONDecodeError):
            active_theme = {}
        if active_theme.get('key') == f'custom:{theme_id}':
            conn.execute(
                "UPDATE admin_settings SET value='', updated_at=CURRENT_TIMESTAMP WHERE key='active_theme'"
            )
            cleared_active = True
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'message': '사용자 제작 테마를 삭제했습니다.',
        'cleared_active': cleared_active,
    })


@admin_bp.route('/themes/preferences', methods=['POST'])
def save_theme_preference():
    require_admin()
    data = request.get_json(silent=True) or {}
    theme_key = str(data.get('key') or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', theme_key):
        return jsonify({'status': 'error', 'message': '올바르지 않은 테마 식별값입니다.'}), 400
    favorite = 1 if data.get('is_favorite') else 0
    hidden = 1 if data.get('is_hidden') else 0
    conn = get_db()
    conn.execute('''
        INSERT INTO theme_catalog_preferences (
            owner_emp_no, theme_key, is_favorite, is_hidden, updated_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(owner_emp_no, theme_key) DO UPDATE SET
            is_favorite=excluded.is_favorite,
            is_hidden=excluded.is_hidden,
            updated_at=CURRENT_TIMESTAMP
    ''', (_theme_owner(), theme_key, favorite, hidden))
    conn.commit()
    conn.close()
    return jsonify({
        'status': 'success',
        'preference': {'is_favorite': bool(favorite), 'is_hidden': bool(hidden)},
    })


@admin_bp.route('/themes/preferences/restore-hidden', methods=['POST'])
def restore_hidden_themes():
    require_admin()
    conn = get_db()
    conn.execute('''
        UPDATE theme_catalog_preferences
        SET is_hidden=0, updated_at=CURRENT_TIMESTAMP
        WHERE owner_emp_no=? AND is_hidden=1
    ''', (_theme_owner(),))
    restored = conn.total_changes
    conn.commit()
    conn.close()
    return jsonify({'status': 'success', 'message': f'숨김 테마 {restored}개를 복원했습니다.', 'restored': restored})


@admin_bp.route('/stats')
def stats():
    require_admin()
    conn = get_db()
    daily_login = conn.execute('''
        SELECT DATE(DATETIME(created_at, '+9 hours')) AS day,
               SUM(CASE WHEN action='login' THEN 1 ELSE 0 END) AS login_count,
               SUM(CASE WHEN action='logout' THEN 1 ELSE 0 END) AS logout_count,
               COUNT(DISTINCT CASE WHEN action='login' THEN COALESCE(emp_no, user_name) END) AS user_count
        FROM login_activity
        WHERE created_at >= DATETIME('now', '-3 months')
        GROUP BY DATE(DATETIME(created_at, '+9 hours'))
        ORDER BY day DESC
        LIMIT 93
    ''').fetchall()
    menu_stats = conn.execute('''
        SELECT m.menu_name,
               SUM(m.access_count) AS access_count,
               COUNT(DISTINCT m.emp_no) AS user_count,
               DATETIME(MAX(m.last_used), '+9 hours') AS last_used
        FROM usage_user_menu_totals m
        JOIN users u ON CAST(u.emp_no AS TEXT)=m.emp_no
        GROUP BY m.menu_name
        ORDER BY access_count DESC
    ''').fetchall()
    user_stats = conn.execute('''
        SELECT t.user_name,
               t.emp_no,
               t.access_count,
               t.login_count,
               t.logout_count,
               DATETIME(t.first_used, '+9 hours') AS first_used,
               DATETIME(t.last_used, '+9 hours') AS last_used,
               COUNT(m.menu_name) AS menu_count
        FROM usage_user_totals t
        JOIN users u ON CAST(u.emp_no AS TEXT)=t.emp_no
        LEFT JOIN usage_user_menu_totals m ON m.emp_no=t.emp_no
        GROUP BY t.emp_no, t.user_name, t.access_count, t.login_count,
                 t.logout_count, t.first_used, t.last_used
        ORDER BY t.access_count DESC, t.login_count DESC, t.user_name
    ''').fetchall()

    today_login = conn.execute('''
        SELECT
            SUM(CASE WHEN action='login' THEN 1 ELSE 0 END) AS login_count,
            SUM(CASE WHEN action='logout' THEN 1 ELSE 0 END) AS logout_count,
            COUNT(DISTINCT CASE WHEN action='login' THEN COALESCE(emp_no, user_name) END) AS user_count
        FROM login_activity
        WHERE DATE(DATETIME(created_at, '+9 hours'))=DATE('now', '+9 hours')
    ''').fetchone()
    today_access = conn.execute(f'''
        SELECT COUNT(*) FROM (
            SELECT COALESCE(emp_no, user_name), path,
                   STRFTIME('%Y-%m-%d %H:%M:%S', created_at)
            FROM usage_logs
            WHERE {TRACKABLE_USAGE_SQL}
              AND DATE(DATETIME(created_at, '+9 hours'))=DATE('now', '+9 hours')
            GROUP BY COALESCE(emp_no, user_name), path,
                     STRFTIME('%Y-%m-%d %H:%M:%S', created_at)
        )
    ''').fetchone()[0]
    active_7d = conn.execute('''
        SELECT COUNT(DISTINCT COALESCE(emp_no, user_name))
        FROM login_activity
        WHERE action='login' AND created_at >= DATETIME('now', '-7 days')
    ''').fetchone()[0]
    active_30d = conn.execute('''
        SELECT COUNT(DISTINCT COALESCE(emp_no, user_name))
        FROM login_activity
        WHERE action='login' AND created_at >= DATETIME('now', '-30 days')
    ''').fetchone()[0]
    cumulative = conn.execute('''
        SELECT COALESCE(SUM(access_count), 0) AS access_count,
               COALESCE(SUM(login_count), 0) AS login_count,
               COALESCE(SUM(logout_count), 0) AS logout_count,
               COUNT(*) AS user_count
        FROM usage_user_totals t
        JOIN users u ON CAST(u.emp_no AS TEXT)=t.emp_no
    ''').fetchone()
    summary_cards = [
        {'label': '오늘 로그인', 'value': today_login['login_count'] or 0, 'hint': f"고유 회원 {today_login['user_count'] or 0}명", 'icon': 'fa-right-to-bracket'},
        {'label': '오늘 로그아웃', 'value': today_login['logout_count'] or 0, 'hint': '로그아웃 이벤트', 'icon': 'fa-right-from-bracket'},
        {'label': '오늘 화면 접속', 'value': today_access, 'hint': '중복·API 요청 제외', 'icon': 'fa-display'},
        {'label': '최근 7일 이용 회원', 'value': active_7d, 'hint': f"최근 30일 {active_30d}명", 'icon': 'fa-user-clock'},
        {'label': '누적 화면 접속', 'value': cumulative['access_count'], 'hint': '3개월 이후에도 누계 유지', 'icon': 'fa-chart-column'},
        {'label': '누적 로그인', 'value': cumulative['login_count'], 'hint': f"로그아웃 {cumulative['logout_count']:,}회", 'icon': 'fa-key'},
        {'label': '통계 회원', 'value': cumulative['user_count'], 'hint': '누계가 기록된 회원', 'icon': 'fa-users'},
        {'label': '이용 메뉴', 'value': len(menu_stats), 'hint': '누적 메뉴 분류', 'icon': 'fa-bars-progress'},
    ]

    certificate_size, certificate_count = _folder_size(os.path.join(BASE_DIR, 'output_pdfs'))
    content_counts = {
        '게시판 게시물': conn.execute("SELECT COUNT(*) FROM board_posts").fetchone()[0] if _table_exists(conn, 'board_posts') else 0,
        '메신저 메시지': conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] if _table_exists(conn, 'messages') else 0,
        '학교업무 게시물': conn.execute("SELECT COUNT(*) FROM school_posts").fetchone()[0] if _table_exists(conn, 'school_posts') else 0,
        '증명발급 PDF': certificate_count,
        '갤러리 파일': conn.execute("SELECT COUNT(*) FROM gall2").fetchone()[0] if _table_exists(conn, 'gall2') else 0,
    }
    conn.close()
    return _render(
        'stats',
        daily_login=daily_login,
        menu_stats=menu_stats,
        user_stats=user_stats,
        summary_cards=summary_cards,
        content_counts=content_counts,
    )


@admin_bp.route('/stats/daily-users')
def stats_daily_users():
    require_admin()
    day = (request.args.get('day') or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        return jsonify({'status': 'error', 'message': '올바른 일자를 입력해 주세요.'}), 400

    conn = get_db()
    users = conn.execute('''
        SELECT COALESCE(emp_no, '') AS emp_no,
               MAX(COALESCE(user_name, emp_no, '알 수 없음')) AS user_name,
               SUM(CASE WHEN action='login' THEN 1 ELSE 0 END) AS login_count,
               SUM(CASE WHEN action='logout' THEN 1 ELSE 0 END) AS logout_count,
               MIN(STRFTIME('%H:%M:%S', DATETIME(created_at, '+9 hours'))) AS first_time,
               MAX(STRFTIME('%H:%M:%S', DATETIME(created_at, '+9 hours'))) AS last_time
        FROM login_activity
        WHERE DATE(DATETIME(created_at, '+9 hours'))=?
        GROUP BY COALESCE(emp_no, user_name, 'unknown')
        ORDER BY login_count DESC, user_name
    ''', (day,)).fetchall()
    conn.close()
    return jsonify({
        'status': 'success',
        'day': day,
        'users': [dict(row) for row in users],
    })


@admin_bp.route('/stats/member-detail')
def stats_member_detail():
    require_admin()
    emp_no = (request.args.get('emp_no') or '').strip()
    if not emp_no or len(emp_no) > 80:
        return jsonify({'status': 'error', 'message': '회원을 확인할 수 없습니다.'}), 400

    conn = get_db()
    member = conn.execute('''
        SELECT emp_no, user_name, access_count, login_count, logout_count,
               DATETIME(first_used, '+9 hours') AS first_used,
               DATETIME(last_used, '+9 hours') AS last_used,
               DATETIME(last_login, '+9 hours') AS last_login,
               DATETIME(last_logout, '+9 hours') AS last_logout
        FROM usage_user_totals
        WHERE emp_no=?
    ''', (emp_no,)).fetchone()
    if not member:
        conn.close()
        return jsonify({'status': 'error', 'message': '회원 누계 기록이 없습니다.'}), 404

    menus = conn.execute('''
        SELECT menu_name, access_count,
               DATETIME(first_used, '+9 hours') AS first_used,
               DATETIME(last_used, '+9 hours') AS last_used
        FROM usage_user_menu_totals
        WHERE emp_no=?
        ORDER BY access_count DESC, menu_name
    ''', (emp_no,)).fetchall()
    daily = conn.execute(f'''
        SELECT DATE(DATETIME(created_at, '+9 hours')) AS day,
               COUNT(DISTINCT path || '|' || STRFTIME('%Y-%m-%d %H:%M:%S', created_at)) AS access_count,
               COUNT(DISTINCT menu_name) AS menu_count
        FROM usage_logs
        WHERE emp_no=?
          AND created_at >= DATETIME('now', '-3 months')
          AND {TRACKABLE_USAGE_SQL}
        GROUP BY DATE(DATETIME(created_at, '+9 hours'))
        ORDER BY day DESC
        LIMIT 93
    ''', (emp_no,)).fetchall()
    conn.close()
    return jsonify({
        'status': 'success',
        'member': dict(member),
        'menus': [dict(row) for row in menus],
        'daily': [dict(row) for row in daily],
    })


def _table_exists(conn, table_name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)).fetchone() is not None


@admin_bp.route('/settings')
def settings():
    require_admin()
    conn = get_db()
    admin_user = conn.execute("SELECT id, emp_no, name, position, level, email, status, join_date FROM users WHERE emp_no='admin'").fetchone()
    counts = {
        '전체 회원': conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        '승인 회원': conn.execute("SELECT COUNT(*) FROM users WHERE status='승인'").fetchone()[0],
        '대기 회원': conn.execute("SELECT COUNT(*) FROM users WHERE status!='승인' OR status IS NULL").fetchone()[0],
        '관리자 권한': conn.execute("SELECT COUNT(*) FROM users WHERE level <= 2").fetchone()[0],
    }
    conn.close()
    return _render('settings', admin_user=admin_user, counts=counts)


@admin_bp.route('/settings/admin-password', methods=['POST'])
def reset_admin_password():
    require_admin()
    new_password = (request.form.get('new_password') or '').strip()
    if not new_password:
        return redirect(url_for('admin.settings'))
    conn = get_db()
    conn.execute(
        "UPDATE users SET password=? WHERE emp_no='admin'",
        (hash_password(new_password),),
    )
    conn.commit()
    conn.close()
    return redirect(url_for('admin.settings'))
