from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, session, url_for
import json
import os
import re
import shutil
from datetime import datetime

from .database import BASE_DIR, GALLERY_ROOT, PROFILE_ROOT, SCHOOL_UPLOADS, get_db

admin_bp = Blueprint('admin', __name__)


THEME_CATEGORY_NAMES = {'custom', 'gallery', 'accent', 'deep-color', 'seasonal', 'default'}
THEME_VAR_KEYS = {
    '--body-bg', '--app-bg', '--main-bg', '--nav-bg', '--primary-color', '--primary-light',
    '--primary-dark', '--text-dark', '--text-gray', '--border-color', '--border-light',
    '--card-bg', '--card-border', '--card-shadow', '--card-backdrop', '--input-bg',
    '--input-text', '--widget-bg', '--widget-hover', '--widget-border', '--widget-border-color',
    '--tooltip-bg', '--tooltip-text', '--effect-color1', '--effect-color2', '--effect-color3',
}


ADMIN_TABS = [
    ('people', '인사관리', 'fa-user-gear', '/user'),
    ('boards', '게시판관리', 'fa-clipboard-list', '/admin/boards'),
    ('disk', '디스크관리', 'fa-hard-drive', '/admin/disk'),
    ('themes', '테마관리', 'fa-palette', '/admin/theme'),
    ('stats', '이용통계', 'fa-chart-line', '/admin/stats'),
    ('settings', 'Admin설정', 'fa-user-shield', '/admin/settings'),
]


def is_admin_level():
    return session.get('user_name') == 'admin' or session.get('emp_no') == 'admin' or int(session.get('user_level', 99)) <= 2


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
        return json.loads(row['value'])
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
    data_root = '/mnt/data' if os.path.exists('/mnt/data') else BASE_DIR
    return [
        {'key': 'board', 'label': '게시판', 'path': os.path.join(data_root, 'board_uploads'), 'icon': 'fa-clipboard-list'},
        {'key': 'messenger', 'label': '사내메신저', 'path': os.path.join(data_root, 'uploads'), 'icon': 'fa-comments'},
        {'key': 'school', 'label': '학교업무메뉴', 'path': SCHOOL_UPLOADS, 'icon': 'fa-school'},
        {'key': 'certificate', 'label': '증명발급', 'path': os.path.join(data_root, 'output_pdfs'), 'icon': 'fa-file-invoice'},
        {'key': 'contract', 'label': '계약시스템', 'path': os.path.join(data_root, 'contracts'), 'icon': 'fa-file-contract'},
        {'key': 'gallery', 'label': '갤러리', 'path': GALLERY_ROOT, 'icon': 'fa-images'},
        {'key': 'gall2', 'label': '사내 갤러리', 'path': os.path.join(data_root, 'gall2'), 'icon': 'fa-photo-film'},
        {'key': 'profiles', 'label': '인사/프로필', 'path': PROFILE_ROOT, 'icon': 'fa-id-card'},
        {'key': 'deposit', 'label': '입금 엑셀', 'path': os.path.join(BASE_DIR, 'uploads_deposit'), 'icon': 'fa-file-excel'},
        {'key': 'app', 'label': '앱 루트', 'path': BASE_DIR, 'icon': 'fa-folder-tree'},
    ]


def _root_by_key(root_key):
    roots = {item['key']: item for item in _storage_roots()}
    return roots.get(root_key) or roots['app']


def _safe_target(root_info, rel_path=''):
    root = os.path.abspath(root_info['path'])
    target = os.path.abspath(os.path.join(root, rel_path or ''))
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

    roots = []
    for item in _storage_roots():
        size, count = _folder_size(item['path'])
        row = dict(item)
        row.update(size=size, size_text=_format_size(size), count=count, exists=os.path.exists(item['path']))
        roots.append(row)

    files = []
    parent_path = None
    if os.path.exists(target) and os.path.isdir(target):
        for name in os.listdir(target):
            path = os.path.join(target, name)
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
    return _render(
        'disk',
        roots=roots,
        selected_root=root_info,
        current_path=rel_path,
        parent_path=parent_path,
        target_exists=os.path.exists(target),
        files=files,
        disk_stats=disk_stats,
    )


@admin_bp.route('/disk/download')
def disk_download():
    require_admin()
    root_info = _root_by_key(request.args.get('root', 'app'))
    root, target = _safe_target(root_info, request.args.get('path', ''))
    if not os.path.isfile(target):
        abort(404)
    return send_file(target, as_attachment=True, download_name=os.path.basename(target))


@admin_bp.route('/disk/delete', methods=['POST'])
def disk_delete():
    require_admin()
    root_info = _root_by_key(request.form.get('root', 'app'))
    root, target = _safe_target(root_info, request.form.get('path', ''))
    if os.path.abspath(root) == os.path.abspath(target):
        return jsonify({'status': 'error', 'message': '최상위 폴더는 삭제할 수 없습니다.'}), 400
    if not os.path.exists(target):
        return jsonify({'status': 'error', 'message': '파일을 찾을 수 없습니다.'}), 404
    if os.path.isdir(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    return jsonify({'status': 'success'})


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
        SELECT DATE(created_at) AS day,
               SUM(CASE WHEN action='login' THEN 1 ELSE 0 END) AS login_count,
               SUM(CASE WHEN action='logout' THEN 1 ELSE 0 END) AS logout_count
        FROM login_activity
        GROUP BY DATE(created_at)
        ORDER BY day DESC
        LIMIT 30
    ''').fetchall()
    menu_stats = conn.execute('''
        SELECT menu_name, COUNT(*) AS count
        FROM usage_logs
        GROUP BY menu_name
        ORDER BY count DESC
        LIMIT 20
    ''').fetchall()
    user_stats = conn.execute('''
        SELECT user_name, emp_no, COUNT(*) AS count, MAX(created_at) AS last_used
        FROM usage_logs
        GROUP BY emp_no, user_name
        ORDER BY count DESC
        LIMIT 50
    ''').fetchall()
    recent = conn.execute('''
        SELECT user_name, menu_name, path, method, created_at
        FROM usage_logs
        ORDER BY id DESC
        LIMIT 30
    ''').fetchall()
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
        recent=recent,
        content_counts=content_counts,
    )


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
    conn.execute("UPDATE users SET password=? WHERE emp_no='admin'", (new_password,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin.settings'))
