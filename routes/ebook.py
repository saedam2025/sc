"""웹 e-book 업로드, 편집, 열람, 책갈피와 독후감 기능."""

from __future__ import annotations

import html
import os
import re
import shutil
import uuid
from pathlib import Path

from bs4 import BeautifulSoup
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

from .database import get_db
from .storage import DATA_ROOT

ebook_bp = Blueprint("ebook", __name__)
EBOOK_ROOT = Path(DATA_ROOT) / "ebook_uploads"
COVER_ROOT = EBOOK_ROOT / "covers"
MEDIA_ROOT = EBOOK_ROOT / "media"
TEXT_EXTENSIONS = {".txt", ".md"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
DEFAULT_PAGE_LENGTH = 1800


def init_ebook_schema():
    COVER_ROOT.mkdir(parents=True, exist_ok=True)
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ebooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                author TEXT NOT NULL, description TEXT DEFAULT '',
                cover_filename TEXT, cover_path TEXT, source_filename TEXT,
                content_text TEXT NOT NULL, page_char_limit INTEGER NOT NULL DEFAULT 1800,
                created_by TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ebook_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ebook_id INTEGER NOT NULL,
                page_no INTEGER NOT NULL, content_html TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP, UNIQUE(ebook_id, page_no)
            );
            CREATE TABLE IF NOT EXISTS ebook_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ebook_id INTEGER NOT NULL,
                original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
                filepath TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ebook_bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ebook_id INTEGER NOT NULL,
                user_key TEXT NOT NULL, slot INTEGER NOT NULL DEFAULT 1 CHECK(slot BETWEEN 1 AND 5),
                page_no INTEGER NOT NULL DEFAULT 1, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ebook_id, user_key, slot)
            );
            CREATE TABLE IF NOT EXISTS ebook_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ebook_id INTEGER NOT NULL,
                author_emp_no TEXT NOT NULL, author_name TEXT NOT NULL,
                content TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ebook_pages_book ON ebook_pages(ebook_id, page_no);
            CREATE INDEX IF NOT EXISTS idx_ebook_reviews_book ON ebook_reviews(ebook_id, created_at DESC);
        """)
        # 이전 버전은 도서/사용자당 책갈피가 하나뿐이었다. 기존 위치를 1번
        # 책갈피로 보존하면서 5개 색상 슬롯 구조로 한 번만 마이그레이션한다.
        bookmark_columns = {row[1] for row in conn.execute("PRAGMA table_info(ebook_bookmarks)")}
        if "slot" not in bookmark_columns:
            conn.execute("ALTER TABLE ebook_bookmarks RENAME TO ebook_bookmarks_legacy")
            conn.execute("""
                CREATE TABLE ebook_bookmarks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ebook_id INTEGER NOT NULL,
                    user_key TEXT NOT NULL, slot INTEGER NOT NULL DEFAULT 1 CHECK(slot BETWEEN 1 AND 5),
                    page_no INTEGER NOT NULL DEFAULT 1, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(ebook_id, user_key, slot)
                )
            """)
            conn.execute("""
                INSERT INTO ebook_bookmarks (ebook_id,user_key,slot,page_no,updated_at)
                SELECT ebook_id,user_key,1,page_no,updated_at FROM ebook_bookmarks_legacy
            """)
            conn.execute("DROP TABLE ebook_bookmarks_legacy")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ebook_bookmarks_user
            ON ebook_bookmarks(ebook_id, user_key, slot)
        """)
        conn.commit()
    finally:
        conn.close()


def _require_admin():
    if not session.get("emp_no"):
        abort(401)
    try:
        level = int(session.get("user_level", 99))
    except (TypeError, ValueError):
        level = 99
    if level > 2 and session.get("user_name") != "admin":
        abort(403)


@ebook_bp.before_request
def ebook_access_control():
    _require_admin()
    init_ebook_schema()


def _user_key():
    return str(session.get("emp_no") or session.get("user_name") or "")


def _get_book(conn, ebook_id):
    book = conn.execute("SELECT * FROM ebooks WHERE id=?", (ebook_id,)).fetchone()
    if not book:
        abort(404)
    return book


def _read_text(upload):
    raw = upload.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("텍스트 파일은 20MB 이하만 업로드할 수 있습니다.")
    if b"\x00" in raw:
        raise ValueError("일반 텍스트 파일만 업로드할 수 있습니다.")
    for encoding in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("UTF-8 또는 CP949 형식의 텍스트 파일을 사용해 주세요.")


def _split_long_text(text, limit):
    pieces, remaining = [], text.strip()
    while len(remaining) > limit:
        window = remaining[:limit + 1]
        cut = max(window.rfind(mark) for mark in ("다. ", "요. ", ". ", "! ", "? ", " "))
        cut = limit if cut < int(limit * .55) else cut + 1
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def paginate_text(text, limit=DEFAULT_PAGE_LENGTH):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    paragraphs = paragraphs or ["내용이 없습니다."]
    pages, current, size = [], [], 0
    for paragraph in paragraphs:
        for chunk in _split_long_text(paragraph, limit):
            if current and size + len(chunk) + 2 > limit:
                pages.append(current)
                current, size = [], 0
            current.append(chunk)
            size += len(chunk) + 2
    if current:
        pages.append(current)
    return ["".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in page) for page in pages]


def _save_image(upload, folder):
    raw_name = Path((upload.filename or "").replace("\\", "/")).name
    extension = Path(raw_name).suffix.lower()
    safe_stem = secure_filename(Path(raw_name).stem) or "image"
    original = f"{safe_stem}{extension}"
    if extension not in IMAGE_EXTENSIONS:
        raise ValueError("이미지는 JPG, PNG, GIF, WEBP만 사용할 수 있습니다.")
    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size > 12 * 1024 * 1024:
        raise ValueError("이미지는 한 장당 12MB 이하만 업로드할 수 있습니다.")
    stored = uuid.uuid4().hex + extension
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored
    upload.save(path)
    return original, stored, str(path)


def _sanitize_page_html(raw_html, allowed_media_ids=None):
    allowed_media_ids = allowed_media_ids or set()
    soup = BeautifulSoup(raw_html or "", "html.parser")
    allowed = {"p", "br", "div", "h2", "h3", "blockquote", "ul", "ol", "li",
               "strong", "b", "em", "i", "u", "s", "figure", "figcaption", "img", "span"}
    for tag in list(soup.find_all(True)):
        if tag.name not in allowed:
            tag.unwrap()
            continue
        attrs = {}
        if tag.name == "img":
            src = str(tag.get("src") or "")
            media_id = src.rsplit("/", 1)[-1].split("?", 1)[0]
            if not src.startswith("/ebook/media/") or not media_id.isdigit() or media_id not in allowed_media_ids:
                tag.decompose()
                continue
            classes = set(tag.get("class") or [])
            alignment = next((name for name in ("image-left", "image-right", "image-center") if name in classes), "image-center")
            image_size = next((name for name in ("image-size-small", "image-size-medium", "image-size-large", "image-size-full") if name in classes), "image-size-medium")
            attrs = {"src": src, "alt": str(tag.get("alt") or "본문 이미지")[:200],
                     "class": ["ebook-inline-image", alignment, image_size]}
        tag.attrs = attrs
    return str(soup).strip() or "<p><br></p>"


@ebook_bp.route("/")
def library():
    query = (request.args.get("q") or "").strip()
    where, params = "", []
    if query:
        where, params = "WHERE e.title LIKE ? OR e.author LIKE ?", [f"%{query}%", f"%{query}%"]
    conn = get_db()
    try:
        books = conn.execute(f"""
            SELECT e.*, COUNT(p.id) page_count,
              (SELECT COUNT(*) FROM ebook_reviews r WHERE r.ebook_id=e.id) review_count
            FROM ebooks e LEFT JOIN ebook_pages p ON p.ebook_id=e.id {where}
            GROUP BY e.id ORDER BY e.updated_at DESC, e.id DESC
        """, params).fetchall()
    finally:
        conn.close()
    return render_template("ebook/library.html", books=books, query=query)


@ebook_bp.route("/new", methods=["GET", "POST"])
def create_book():
    if request.method == "GET":
        return render_template("ebook/form.html")
    title, author = (request.form.get("title") or "").strip(), (request.form.get("author") or "").strip()
    description = (request.form.get("description") or "").strip()
    text_file, cover = request.files.get("text_file"), request.files.get("cover")
    try:
        page_length = max(800, min(3500, int(request.form.get("page_length") or DEFAULT_PAGE_LENGTH)))
    except (TypeError, ValueError):
        page_length = DEFAULT_PAGE_LENGTH
    errors = []
    if not title: errors.append("제목을 입력해 주세요.")
    if not author: errors.append("저자를 입력해 주세요.")
    if not text_file or not text_file.filename:
        errors.append("e-book 본문 텍스트 파일을 선택해 주세요.")
    elif Path(text_file.filename).suffix.lower() not in TEXT_EXTENSIONS:
        errors.append("본문은 TXT 또는 MD 파일만 업로드할 수 있습니다.")
    if errors:
        for message in errors: flash(message, "error")
        return render_template("ebook/form.html"), 400
    cover_name = cover_path = None
    try:
        content_text = _read_text(text_file)
        pages = paginate_text(content_text, page_length)
        if cover and cover.filename:
            cover_name, _, cover_path = _save_image(cover, COVER_ROOT)
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("ebook/form.html"), 400
    conn = get_db()
    try:
        cursor = conn.execute("""INSERT INTO ebooks
            (title,author,description,cover_filename,cover_path,source_filename,content_text,page_char_limit,created_by)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (title, author, description, cover_name, cover_path, secure_filename(text_file.filename) or "book.txt",
             content_text, page_length, session.get("user_name") or _user_key()))
        ebook_id = cursor.lastrowid
        conn.executemany("INSERT INTO ebook_pages (ebook_id,page_no,content_html) VALUES (?,?,?)",
                         [(ebook_id, i, page) for i, page in enumerate(pages, 1)])
        conn.commit()
    except Exception:
        conn.rollback()
        if cover_path and os.path.exists(cover_path): os.remove(cover_path)
        raise
    finally:
        conn.close()
    flash(f"‘{title}’을(를) {len(pages)}페이지 e-book으로 만들었습니다.", "success")
    return redirect(url_for("ebook.read_book", ebook_id=ebook_id))


@ebook_bp.route("/<int:ebook_id>")
def read_book(ebook_id):
    conn = get_db()
    try:
        book = _get_book(conn, ebook_id)
        pages = conn.execute("SELECT id,page_no,content_html FROM ebook_pages WHERE ebook_id=? ORDER BY page_no", (ebook_id,)).fetchall()
        bookmarks = conn.execute("""SELECT slot,page_no,updated_at FROM ebook_bookmarks
            WHERE ebook_id=? AND user_key=? ORDER BY slot""", (ebook_id, _user_key())).fetchall()
        latest_mark = conn.execute("""SELECT page_no FROM ebook_bookmarks
            WHERE ebook_id=? AND user_key=? ORDER BY updated_at DESC,id DESC LIMIT 1""",
            (ebook_id, _user_key())).fetchone()
    finally:
        conn.close()
    return render_template("ebook/reader.html", book=book, pages=pages,
                           bookmark_page=latest_mark["page_no"] if latest_mark else 1,
                           bookmarks=bookmarks)


@ebook_bp.route("/<int:ebook_id>/bookmark", methods=["POST"])
def save_bookmark(ebook_id):
    data = request.get_json(silent=True) or request.form
    try: page_no = max(1, int(data.get("page_no") or 1))
    except (TypeError, ValueError): return jsonify(ok=False, message="페이지 번호가 올바르지 않습니다."), 400
    try: slot = int(data.get("slot") or 1)
    except (TypeError, ValueError): return jsonify(ok=False, message="책갈피 색상이 올바르지 않습니다."), 400
    if slot not in range(1, 6):
        return jsonify(ok=False, message="책갈피는 1번부터 5번까지만 사용할 수 있습니다."), 400
    conn = get_db()
    try:
        _get_book(conn, ebook_id)
        maximum = conn.execute("SELECT COALESCE(MAX(page_no),1) FROM ebook_pages WHERE ebook_id=?", (ebook_id,)).fetchone()[0]
        page_no = min(page_no, int(maximum or 1))
        conn.execute("""INSERT INTO ebook_bookmarks (ebook_id,user_key,slot,page_no,updated_at)
            VALUES (?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(ebook_id,user_key,slot) DO UPDATE SET
            page_no=excluded.page_no,updated_at=CURRENT_TIMESTAMP""", (ebook_id, _user_key(), slot, page_no))
        conn.commit()
    finally: conn.close()
    return jsonify(ok=True, slot=slot, page_no=page_no)


@ebook_bp.route("/<int:ebook_id>/bookmark/<int:slot>", methods=["DELETE"])
def delete_bookmark(ebook_id, slot):
    if slot not in range(1, 6):
        return jsonify(ok=False, message="책갈피 색상이 올바르지 않습니다."), 400
    conn = get_db()
    try:
        _get_book(conn, ebook_id)
        conn.execute("DELETE FROM ebook_bookmarks WHERE ebook_id=? AND user_key=? AND slot=?",
                     (ebook_id, _user_key(), slot))
        conn.commit()
    finally:
        conn.close()
    return jsonify(ok=True, slot=slot)


@ebook_bp.route("/<int:ebook_id>/reviews", methods=["POST"])
def create_review(ebook_id):
    content = (request.form.get("content") or "").strip()
    if not content or len(content) > 10000:
        flash("독후감은 1자 이상 10,000자 이하로 작성해 주세요.", "error")
    else:
        conn = get_db()
        try:
            _get_book(conn, ebook_id)
            conn.execute("INSERT INTO ebook_reviews (ebook_id,author_emp_no,author_name,content) VALUES (?,?,?,?)",
                         (ebook_id, _user_key(), session.get("user_name") or "사용자", content))
            conn.commit()
            flash("독후감을 등록했습니다.", "success")
        finally: conn.close()
    return redirect(url_for("ebook.library", reviews=ebook_id))


@ebook_bp.route("/<int:ebook_id>/reviews-panel")
def reviews_panel(ebook_id):
    conn = get_db()
    try:
        book = _get_book(conn, ebook_id)
        reviews = conn.execute("SELECT * FROM ebook_reviews WHERE ebook_id=? ORDER BY created_at DESC,id DESC",
                               (ebook_id,)).fetchall()
    finally:
        conn.close()
    return render_template("ebook/_reviews_panel.html", book=book, reviews=reviews)


@ebook_bp.route("/<int:ebook_id>/reviews/<int:review_id>/delete", methods=["POST"])
def delete_review(ebook_id, review_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM ebook_reviews WHERE id=? AND ebook_id=?", (review_id, ebook_id))
        conn.commit()
    finally: conn.close()
    flash("독후감을 삭제했습니다.", "success")
    return redirect(url_for("ebook.library", reviews=ebook_id))


@ebook_bp.route("/<int:ebook_id>/edit", methods=["GET", "POST"])
def edit_book(ebook_id):
    conn = get_db()
    try:
        book = _get_book(conn, ebook_id)
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            author = (request.form.get("author") or "").strip()
            if not title or not author:
                flash("제목과 저자를 모두 입력해 주세요.", "error")
            else:
                cover_name, cover_path = book["cover_filename"], book["cover_path"]
                cover = request.files.get("cover")
                if cover and cover.filename:
                    try:
                        cover_name, _, new_path = _save_image(cover, COVER_ROOT)
                        old_path, cover_path = cover_path, new_path
                        if old_path and os.path.exists(old_path): os.remove(old_path)
                    except ValueError as exc:
                        flash(str(exc), "error")
                        return redirect(url_for("ebook.edit_book", ebook_id=ebook_id))
                conn.execute("""UPDATE ebooks SET title=?,author=?,description=?,cover_filename=?,cover_path=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (title, author, (request.form.get("description") or "").strip(), cover_name, cover_path, ebook_id))
                conn.commit()
                flash("도서 정보를 저장했습니다.", "success")
                return redirect(url_for("ebook.edit_book", ebook_id=ebook_id))
        pages = conn.execute("SELECT * FROM ebook_pages WHERE ebook_id=? ORDER BY page_no", (ebook_id,)).fetchall()
        book = _get_book(conn, ebook_id)
    finally: conn.close()
    return render_template("ebook/edit.html", book=book, pages=pages)


@ebook_bp.route("/<int:ebook_id>/pages/<int:page_id>", methods=["POST"])
def update_page(ebook_id, page_id):
    data = request.get_json(silent=True) or request.form
    conn = get_db()
    obsolete_paths = []
    try:
        allowed_ids = {str(row[0]) for row in conn.execute(
            "SELECT id FROM ebook_media WHERE ebook_id=?", (ebook_id,)
        )}
        cleaned = _sanitize_page_html(str(data.get("content_html") or ""), allowed_ids)
        cursor = conn.execute("UPDATE ebook_pages SET content_html=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND ebook_id=?",
                              (cleaned, page_id, ebook_id))
        if not cursor.rowcount: abort(404)
        conn.execute("UPDATE ebooks SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ebook_id,))
        all_html = "".join(row[0] or "" for row in conn.execute(
            "SELECT content_html FROM ebook_pages WHERE ebook_id=?", (ebook_id,)
        ))
        for media in conn.execute("SELECT id,filepath FROM ebook_media WHERE ebook_id=?", (ebook_id,)).fetchall():
            if f"/ebook/media/{media['id']}\"" not in all_html:
                obsolete_paths.append(media["filepath"])
                conn.execute("DELETE FROM ebook_media WHERE id=?", (media["id"],))
        conn.commit()
    finally: conn.close()
    for path in obsolete_paths:
        try:
            if path and os.path.isfile(path): os.remove(path)
        except OSError: pass
    return jsonify(ok=True, content_html=cleaned)


@ebook_bp.route("/<int:ebook_id>/pages", methods=["POST"])
def add_page(ebook_id):
    conn = get_db()
    try:
        _get_book(conn, ebook_id)
        page_no = conn.execute("SELECT COALESCE(MAX(page_no),0)+1 FROM ebook_pages WHERE ebook_id=?", (ebook_id,)).fetchone()[0]
        cursor = conn.execute("INSERT INTO ebook_pages (ebook_id,page_no,content_html) VALUES (?,?,'<p><br></p>')", (ebook_id, page_no))
        conn.execute("UPDATE ebooks SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ebook_id,))
        conn.commit()
    finally: conn.close()
    return jsonify(ok=True, id=cursor.lastrowid, page_no=page_no)


@ebook_bp.route("/<int:ebook_id>/pages/<int:page_id>/delete", methods=["POST"])
def delete_page(ebook_id, page_id):
    conn = get_db()
    try:
        if conn.execute("SELECT COUNT(*) FROM ebook_pages WHERE ebook_id=?", (ebook_id,)).fetchone()[0] <= 1:
            return jsonify(ok=False, message="e-book에는 최소 한 페이지가 필요합니다."), 400
        page = conn.execute("SELECT page_no FROM ebook_pages WHERE id=? AND ebook_id=?", (page_id, ebook_id)).fetchone()
        if not page: abort(404)
        conn.execute("DELETE FROM ebook_pages WHERE id=? AND ebook_id=?", (page_id, ebook_id))
        conn.execute("UPDATE ebook_pages SET page_no=page_no-1 WHERE ebook_id=? AND page_no>?", (ebook_id, page["page_no"]))
        conn.execute("UPDATE ebook_bookmarks SET page_no=MAX(1,page_no-1) WHERE ebook_id=? AND page_no>=?",
                     (ebook_id, page["page_no"]))
        conn.execute("UPDATE ebooks SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (ebook_id,))
        conn.commit()
    finally: conn.close()
    return jsonify(ok=True)


@ebook_bp.route("/<int:ebook_id>/media", methods=["POST"])
def upload_media(ebook_id):
    upload = request.files.get("image")
    if not upload or not upload.filename: return jsonify(ok=False, message="이미지를 선택해 주세요."), 400
    conn, filepath = get_db(), None
    try:
        _get_book(conn, ebook_id)
        original, stored, filepath = _save_image(upload, MEDIA_ROOT / str(ebook_id))
        cursor = conn.execute("INSERT INTO ebook_media (ebook_id,original_name,stored_name,filepath,created_by) VALUES (?,?,?,?,?)",
                              (ebook_id, original, stored, filepath, _user_key()))
        conn.commit()
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    except Exception:
        conn.rollback()
        if filepath and os.path.exists(filepath): os.remove(filepath)
        raise
    finally: conn.close()
    return jsonify(ok=True, url=url_for("ebook.serve_media", media_id=cursor.lastrowid))


@ebook_bp.route("/media/<int:media_id>")
def serve_media(media_id):
    conn = get_db()
    try: row = conn.execute("SELECT filepath FROM ebook_media WHERE id=?", (media_id,)).fetchone()
    finally: conn.close()
    if not row or not os.path.isfile(row["filepath"]): abort(404)
    return send_file(row["filepath"], conditional=True)


@ebook_bp.route("/<int:ebook_id>/cover")
def serve_cover(ebook_id):
    conn = get_db()
    try: cover_path = _get_book(conn, ebook_id)["cover_path"]
    finally: conn.close()
    if not cover_path or not os.path.isfile(cover_path): abort(404)
    return send_file(cover_path, conditional=True)


@ebook_bp.route("/<int:ebook_id>/delete", methods=["POST"])
def delete_book(ebook_id):
    conn, paths = get_db(), []
    media_directory = MEDIA_ROOT / str(ebook_id)
    try:
        book = _get_book(conn, ebook_id)
        if book["cover_path"]: paths.append(book["cover_path"])
        paths += [row["filepath"] for row in conn.execute("SELECT filepath FROM ebook_media WHERE ebook_id=?", (ebook_id,))]
        for table in ("ebook_reviews", "ebook_bookmarks", "ebook_media", "ebook_pages"):
            conn.execute(f"DELETE FROM {table} WHERE ebook_id=?", (ebook_id,))
        conn.execute("DELETE FROM ebooks WHERE id=?", (ebook_id,))
        conn.commit()
    finally: conn.close()
    for path in paths:
        try:
            if path and os.path.isfile(path): os.remove(path)
        except OSError: pass
    if media_directory.is_dir():
        shutil.rmtree(media_directory, ignore_errors=True)
    flash("e-book을 삭제했습니다.", "success")
    return redirect(url_for("ebook.library"))
