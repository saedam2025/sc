from flask import Blueprint, render_template, request, jsonify, session, current_app, redirect, url_for, abort
import os
import re
from bs4 import BeautifulSoup, Comment
from .secure_files import delete_file, encrypted_response, encrypted_storage_name, encrypt_upload, original_filename
from datetime import datetime, timezone, timedelta
from .database import get_db
from .points import deduct_deleted_post_points
from .security import admin_required
from .storage import BOARD_UPLOADS

board_bp = Blueprint('board', __name__, url_prefix='/board')

UPLOAD_FOLDER = str(BOARD_UPLOADS)
INLINE_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

RICH_TEXT_ALLOWED_TAGS = {
    'p', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'strike',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td',
    'div', 'span', 'a', 'img', 'blockquote', 'code', 'pre', 'hr',
}
RICH_TEXT_DROP_TAGS = {
    'script', 'style', 'iframe', 'object', 'embed', 'link', 'meta',
    'form', 'input', 'button', 'textarea', 'select', 'option', 'svg', 'math',
}
RICH_TEXT_ALLOWED_STYLES = {
    'color', 'background-color', 'font-size', 'font-family', 'font-weight',
    'font-style', 'line-height', 'text-align', 'text-decoration',
    'width', 'height', 'min-width', 'max-width', 'min-height', 'max-height',
    'vertical-align', 'white-space', 'word-break',
    'margin', 'margin-top', 'margin-right', 'margin-bottom', 'margin-left',
    'padding', 'padding-top', 'padding-right', 'padding-bottom', 'padding-left',
    'border', 'border-width', 'border-style', 'border-color',
    'border-collapse', 'table-layout', 'list-style-type',
}


def _clean_rich_text_style(value):
    cleaned = []
    for declaration in str(value or '').split(';'):
        if ':' not in declaration:
            continue
        property_name, property_value = declaration.split(':', 1)
        property_name = property_name.strip().lower()
        property_value = property_value.strip()
        lowered = property_value.lower().replace(' ', '')
        if property_name not in RICH_TEXT_ALLOWED_STYLES:
            continue
        if any(token in lowered for token in (
            'url(', 'expression(', 'javascript:', 'vbscript:', '@import',
            'behavior:', '-moz-binding', '<', '>',
        )):
            continue
        cleaned.append(f'{property_name}:{property_value}')
    return ';'.join(cleaned)


def sanitize_board_content(value):
    """생성형 게시판 에디터 본문에 필요한 안전한 HTML만 보존한다."""
    raw = str(value or '')
    if len(raw.encode('utf-8')) > 2 * 1024 * 1024:
        raise ValueError('게시글 내용은 2MB 이하로 작성해주세요.')

    soup = BeautifulSoup(raw, 'html.parser')
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    for tag in list(soup.find_all(True)):
        if not tag.name:
            continue
        name = tag.name.lower()
        if name in RICH_TEXT_DROP_TAGS:
            tag.decompose()
            continue
        if name not in RICH_TEXT_ALLOWED_TAGS:
            tag.unwrap()
            continue

        attributes = {}
        style = _clean_rich_text_style(tag.attrs.get('style', ''))
        if style:
            attributes['style'] = style
        if name in {'td', 'th'}:
            for attribute in ('colspan', 'rowspan', 'scope'):
                value = str(tag.attrs.get(attribute, '')).strip()
                if value:
                    attributes[attribute] = value
        elif name == 'a':
            href = str(tag.attrs.get('href', '')).strip()
            if re.match(r'^(?:https?://|mailto:|/)', href, flags=re.I):
                attributes.update({'href': href, 'target': '_blank', 'rel': 'noopener noreferrer'})
            title = str(tag.attrs.get('title', '')).strip()
            if title:
                attributes['title'] = title[:300]
        elif name == 'img':
            src = str(tag.attrs.get('src', '')).strip()
            if not re.match(r'^(?:https?://|/)', src, flags=re.I):
                tag.decompose()
                continue
            attributes['src'] = src
            for attribute in ('alt', 'title'):
                text = str(tag.attrs.get(attribute, '')).strip()
                if text:
                    attributes[attribute] = text[:300]
            for attribute in ('width', 'height'):
                size = str(tag.attrs.get(attribute, '')).strip()
                if re.match(r'^\d{1,4}$', size):
                    attributes[attribute] = size
        tag.attrs = attributes

    return str(soup).strip()


def board_content_has_value(value):
    soup = BeautifulSoup(str(value or ''), 'html.parser')
    return bool(soup.get_text(strip=True) or soup.find(['img', 'table', 'hr']))

def init_board_db():
    conn = get_db()
    
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS board_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name_en TEXT UNIQUE NOT NULL,
            name_kr TEXT NOT NULL,
            desc_text TEXT,
            lvl_access INTEGER DEFAULT 0,
            lvl_read INTEGER DEFAULT 0,
            lvl_write INTEGER DEFAULT 0,
            lvl_delete INTEGER DEFAULT 0,
            lvl_comment INTEGER DEFAULT 0
        );
        
        CREATE TABLE IF NOT EXISTS board_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            board_en TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT NOT NULL,
            views INTEGER DEFAULT 0,
            is_notice INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (board_en) REFERENCES board_config(name_en)
        );
        
        CREATE TABLE IF NOT EXISTS board_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            original_name TEXT NOT NULL,
            saved_name TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            FOREIGN KEY (post_id) REFERENCES board_posts(id) ON DELETE CASCADE
        );
        
        CREATE TABLE IF NOT EXISTS board_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            content TEXT NOT NULL,
            parent_id INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (post_id) REFERENCES board_posts(id) ON DELETE CASCADE
        );
    ''')
    
    try:
        conn.execute("ALTER TABLE board_posts ADD COLUMN is_notice INTEGER DEFAULT 0")
        conn.commit()
    except: pass
    
    try:
        conn.execute("ALTER TABLE board_comments ADD COLUMN parent_id INTEGER DEFAULT 0")
        conn.commit()
    except: pass
    
    try:
        conn.execute('''
            INSERT OR IGNORE INTO board_config 
            (name_en, name_kr, desc_text, lvl_access, lvl_read, lvl_write, lvl_delete, lvl_comment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', ('news', '새담소식', 'News & Notice', 10, 10, 2, 2, 10)) 
        conn.commit()
    except Exception as e:
        print(f"기본 게시판 생성 중 오류 발생: {e}")
    finally:
        conn.close()

def check_permission(board_en, action):
    user_level = session.get('user_level', 99)
    conn = get_db()
    config = conn.execute("SELECT * FROM board_config WHERE name_en=?", (board_en,)).fetchone()
    conn.close()
    
    if not config: return False, "게시판이 존재하지 않습니다."
    if user_level > config[f'lvl_{action}']: return False, "접근 권한이 없습니다."
    return True, config

@board_bp.route('/<board_en>')
def board_list(board_en):
    has_perm, config = check_permission(board_en, 'access')
    if not has_perm: return config, 403 

    page = request.args.get('page', 1, type=int)
    per_page = 10
    offset = (page - 1) * per_page

    conn = get_db()
    total_count = conn.execute("SELECT COUNT(*) as cnt FROM board_posts WHERE board_en=?", (board_en,)).fetchone()['cnt']
    
    posts = conn.execute('''
        SELECT 
            p.id, p.title, p.author, p.views, p.is_notice, p.created_at, substr(p.created_at, 1, 10) as date,
            (SELECT COUNT(*) FROM board_files f WHERE f.post_id = p.id) as file_count,
            (SELECT COUNT(*) FROM board_comments c WHERE c.post_id = p.id) as comment_count
        FROM board_posts p 
        WHERE p.board_en=? 
        ORDER BY p.is_notice DESC, p.id DESC LIMIT ? OFFSET ?
    ''', (board_en, per_page, offset)).fetchall()
    conn.close()

    total_pages = max(1, (total_count + per_page - 1) // per_page)
    now = datetime.now()
    return render_template('board/list.html', board=config, posts=posts, page=page, total_pages=total_pages, now=now)

@board_bp.route('/<board_en>/write', methods=['GET', 'POST'])
def board_write(board_en):
    has_perm, config = check_permission(board_en, 'write')
    if not has_perm: return "권한이 없습니다.", 403

    if request.method == 'GET':
        return render_template('board/write.html', board=config)

    title = str(request.form.get('title') or '').strip()
    try:
        content = sanitize_board_content(request.form.get('content'))
    except ValueError as error:
        return jsonify({"status": "error", "message": str(error)}), 400
    if not title or not board_content_has_value(content):
        return jsonify({"status": "error", "message": "제목과 내용을 입력하세요."}), 400
    is_notice = request.form.get('is_notice', 0, type=int)
    author = session.get('user_name', '익명')
    files = request.files.getlist('files[]') 

    if len(files) > 5: return jsonify({"status": "error", "message": "파일은 최대 5개까지만 첨부 가능합니다."}), 400

    kst = timezone(timedelta(hours=9))
    current_kst = datetime.now(kst).strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    created_paths = []
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO board_posts (board_en, title, content, author, is_notice, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (board_en, title, content, author, is_notice, current_kst))
        post_id = cursor.lastrowid

        for file in files:
            if file and file.filename:
                original_name = original_filename(file.filename)
                saved_name = encrypted_storage_name(original_name)
                filepath = os.path.join(UPLOAD_FOLDER, saved_name)
                file_size = encrypt_upload(file, filepath)
                created_paths.append(filepath)
                cursor.execute("INSERT INTO board_files (post_id, original_name, saved_name, file_size) VALUES (?, ?, ?, ?)",
                               (post_id, original_name, saved_name, file_size))
        conn.commit()
    except Exception:
        conn.rollback()
        for filepath in created_paths:
            delete_file(filepath)
        raise
    finally:
        conn.close()
    return jsonify({"status": "success", "url": url_for('board.board_list', board_en=board_en)})

@board_bp.route('/<board_en>/read/<int:post_id>')
def board_read(board_en, post_id):
    has_perm, config = check_permission(board_en, 'read')
    if not has_perm: return "권한이 없습니다.", 403

    conn = get_db()
    conn.execute("UPDATE board_posts SET views = views + 1 WHERE id=?", (post_id,))
    conn.commit()

    post = conn.execute("SELECT * FROM board_posts WHERE id=?", (post_id,)).fetchone()
    if not post or post['board_en'] != board_en:
        conn.close()
        abort(404)
    post = dict(post)
    post['content'] = sanitize_board_content(post.get('content'))
    files = conn.execute("SELECT * FROM board_files WHERE post_id=?", (post_id,)).fetchall()
    image_files = [
        file for file in files
        if os.path.splitext(file['original_name'] or '')[1].lower() in INLINE_IMAGE_EXTENSIONS
    ]
    all_comments = conn.execute("SELECT * FROM board_comments WHERE post_id=? ORDER BY created_at ASC", (post_id,)).fetchall()
    conn.close()

    top_comments = []
    replies_map = {}
    for row in all_comments:
        c = dict(row)
        pid = c.get('parent_id') or 0
        if pid == 0:
            c['replies'] = []
            top_comments.append(c)
            replies_map[c['id']] = c['replies']
        else:
            if pid in replies_map: replies_map[pid].append(c)
            else: top_comments.append(c)

    return render_template(
        'board/read.html', board=config, post=post, files=files,
        image_files=image_files, comments=top_comments
    )

@board_bp.route('/<board_en>/comment/<int:post_id>', methods=['POST'])
def add_comment(board_en, post_id):
    has_perm, config = check_permission(board_en, 'comment')
    if not has_perm: return jsonify({"status": "error", "message": "댓글 작성 권한이 없습니다."}), 403

    content = request.json.get('content')
    parent_id = request.json.get('parent_id', 0)
    author = session.get('user_name', '익명')

    kst = timezone(timedelta(hours=9))
    current_kst = datetime.now(kst).strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db()
    conn.execute("INSERT INTO board_comments (post_id, author, content, created_at, parent_id) VALUES (?, ?, ?, ?, ?)", 
                 (post_id, author, content, current_kst, parent_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@board_bp.route('/<board_en>/comment_delete/<int:comment_id>', methods=['POST'])
def delete_comment(board_en, comment_id):
    current_user = session.get('user_name')
    user_level = session.get('user_level', 99)
    conn = get_db()
    comment = conn.execute("SELECT author FROM board_comments WHERE id=?", (comment_id,)).fetchone()
    if not comment:
        conn.close()
        return jsonify({"status": "error", "message": "존재하지 않는 댓글입니다."}), 404
    if comment['author'] != current_user and user_level > 3:
        conn.close()
        return jsonify({"status": "error", "message": "삭제 권한이 없습니다."}), 403
    conn.execute("DELETE FROM board_comments WHERE id=? OR parent_id=?", (comment_id, comment_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

# ... (나머지 edit, delete, admin 라우트 등은 동일하게 유지)
# [기존 라우트들 생략... 동일함]

@board_bp.route('/<board_en>/edit/<int:post_id>', methods=['GET', 'POST'])
def board_edit(board_en, post_id):
    current_user = session.get('user_name')
    user_level = session.get('user_level', 99)
    conn = get_db()
    post = conn.execute("SELECT * FROM board_posts WHERE id=?", (post_id,)).fetchone()
    if not post or post['board_en'] != board_en:
        conn.close()
        abort(404)
    post = dict(post)
    post['content'] = sanitize_board_content(post.get('content'))
    if post['author'] != current_user and user_level > 3:
        conn.close()
        return "수정 권한이 없습니다.", 403
    if request.method == 'GET':
        config = conn.execute("SELECT * FROM board_config WHERE name_en=?", (board_en,)).fetchone()
        files = conn.execute("SELECT * FROM board_files WHERE post_id=?", (post_id,)).fetchall()
        conn.close()
        return render_template('board/edit.html', board=config, post=post, files=files)
    title = str(request.form.get('title') or '').strip()
    try:
        content = sanitize_board_content(request.form.get('content'))
    except ValueError as error:
        conn.close()
        return jsonify({"status": "error", "message": str(error)}), 400
    if not title or not board_content_has_value(content):
        conn.close()
        return jsonify({"status": "error", "message": "제목과 내용을 모두 입력하세요."}), 400
    is_notice = request.form.get('is_notice', 0, type=int)
    removed_paths = []
    created_paths = []
    try:
        conn.execute("UPDATE board_posts SET title=?, content=?, is_notice=? WHERE id=?", (title, content, is_notice, post_id))
        deleted_files = request.form.getlist('deleted_files[]')
        for file_id in deleted_files:
            f = conn.execute(
                "SELECT saved_name FROM board_files WHERE id=? AND post_id=?",
                (file_id, post_id)
            ).fetchone()
            if f:
                removed_paths.append(os.path.join(UPLOAD_FOLDER, f['saved_name']))
                conn.execute("DELETE FROM board_files WHERE id=? AND post_id=?", (file_id, post_id))
        files = request.files.getlist('files[]')
        for file in files:
            if file and file.filename:
                original_name = original_filename(file.filename)
                saved_name = encrypted_storage_name(original_name)
                filepath = os.path.join(UPLOAD_FOLDER, saved_name)
                file_size = encrypt_upload(file, filepath)
                created_paths.append(filepath)
                conn.execute("INSERT INTO board_files (post_id, original_name, saved_name, file_size) VALUES (?, ?, ?, ?)",
                               (post_id, original_name, saved_name, file_size))
        conn.commit()
    except Exception:
        conn.rollback()
        for filepath in created_paths:
            delete_file(filepath)
        raise
    finally:
        conn.close()
    for filepath in removed_paths:
        delete_file(filepath)
    return jsonify({"status": "success", "url": url_for('board.board_read', board_en=board_en, post_id=post_id)})

@board_bp.route('/<board_en>/delete/<int:post_id>', methods=['POST'])
def board_delete(board_en, post_id):
    current_user = session.get('user_name')
    user_level = session.get('user_level', 99)
    conn = get_db()
    post = conn.execute("SELECT author FROM board_posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return jsonify({"status": "error", "message": "게시물을 찾을 수 없습니다."}), 404
    if post['author'] != current_user and user_level > 3:
        conn.close()
        return jsonify({"status": "error", "message": "삭제 권한이 없습니다."}), 403
    files = conn.execute("SELECT saved_name FROM board_files WHERE post_id=?", (post_id,)).fetchall()
    conn.execute("DELETE FROM board_posts WHERE id=?", (post_id,))
    conn.commit()
    conn.close()
    if post['author'] == current_user:
        deduct_deleted_post_points(current_user, f'generated-board:{board_en}', post_id)
    for f in files:
        delete_file(os.path.join(UPLOAD_FOLDER, f['saved_name']))
    return jsonify({"status": "success"})

@board_bp.route('/admin/create', methods=['POST'])
@admin_required
def create_board():
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute('''
            INSERT INTO board_config (name_en, name_kr, desc_text, lvl_access, lvl_read, lvl_write, lvl_delete, lvl_comment)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (data['name_en'], data['name_kr'], data['desc_text'], 
              data['lvl_access'], data['lvl_read'], data['lvl_write'], data['lvl_delete'], data['lvl_comment']))
        conn.commit()
        return jsonify({"status": "success", "message": f"{data['name_en']} 게시판이 생성되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": "게시판 생성 실패(영문 이름 중복 등)."}), 400
    finally:
        conn.close()

@board_bp.route('/admin/setup', methods=['GET'])
@admin_required
def admin_setup_page():
    return render_template('board/admin_create.html')

@board_bp.route('/download/<saved_name>')
def download_file(saved_name):
    conn = get_db()
    file_info = conn.execute("SELECT original_name, saved_name FROM board_files WHERE saved_name=?", (saved_name,)).fetchone()
    conn.close()
    if not file_info:
        abort(404)
    show_inline = (
        request.args.get('preview') == '1'
        and os.path.splitext(file_info['original_name'] or '')[1].lower() in INLINE_IMAGE_EXTENSIONS
    )
    return encrypted_response(
        os.path.join(UPLOAD_FOLDER, file_info['saved_name']),
        file_info['original_name'],
        as_attachment=not show_inline,
    )
