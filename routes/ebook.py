"""PNG/JPG 페이지를 공개 e리플렛으로 제작·공유하는 기능."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from .database import get_db
from .storage import DATA_ROOT


ebook_bp = Blueprint("ebook", __name__)
EBOOK_ROOT = Path(DATA_ROOT) / "ebook_uploads"
COVER_ROOT = EBOOK_ROOT / "covers"  # 기존 e-book 표지 호환용
MEDIA_ROOT = EBOOK_ROOT / "media"
LEAFLET_ROOT = EBOOK_ROOT / "leaflets"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
IMAGE_FORMATS = {"JPEG", "PNG"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100


def init_ebook_schema():
    """기존 텍스트 e-book DB를 보존하면서 이미지형 리플렛 필드를 추가한다."""
    for directory in (EBOOK_ROOT, COVER_ROOT, MEDIA_ROOT, LEAFLET_ROOT):
        directory.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS ebooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                description TEXT DEFAULT '',
                cover_filename TEXT,
                cover_path TEXT,
                source_filename TEXT,
                content_text TEXT NOT NULL DEFAULT '',
                page_char_limit INTEGER NOT NULL DEFAULT 1800,
                created_by TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ebook_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ebook_id INTEGER NOT NULL,
                page_no INTEGER NOT NULL,
                content_html TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ebook_id, page_no)
            );
        """)

        ebook_columns = {row[1] for row in conn.execute("PRAGMA table_info(ebooks)")}
        if "kind" not in ebook_columns:
            conn.execute("ALTER TABLE ebooks ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'")
        if "share_token" not in ebook_columns:
            conn.execute("ALTER TABLE ebooks ADD COLUMN share_token TEXT")

        page_columns = {row[1] for row in conn.execute("PRAGMA table_info(ebook_pages)")}
        if "image_filename" not in page_columns:
            conn.execute("ALTER TABLE ebook_pages ADD COLUMN image_filename TEXT")
        if "image_path" not in page_columns:
            conn.execute("ALTER TABLE ebook_pages ADD COLUMN image_path TEXT")

        rows = conn.execute(
            "SELECT id FROM ebooks WHERE share_token IS NULL OR TRIM(share_token)=''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE ebooks SET share_token=? WHERE id=?",
                (secrets.token_urlsafe(24), row["id"]),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_ebooks_share_token ON ebooks(share_token)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ebook_pages_book ON ebook_pages(ebook_id,page_no)"
        )
        conn.commit()
    finally:
        conn.close()


def _is_staff() -> bool:
    return bool(session.get("emp_no"))


def _require_staff() -> None:
    if not _is_staff():
        abort(401)


def _get_leaflet(conn, ebook_id: int):
    book = conn.execute(
        "SELECT * FROM ebooks WHERE id=? AND kind='leaflet'", (ebook_id,)
    ).fetchone()
    if not book:
        abort(404)
    return book


def _get_shared_leaflet(conn, token: str):
    book = conn.execute(
        "SELECT * FROM ebooks WHERE share_token=? AND kind='leaflet'", (token,)
    ).fetchone()
    if not book:
        abort(404)
    return book


def _natural_image_key(upload):
    name = Path((upload.filename or "").replace("\\", "/")).name.casefold()
    return [
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", name)
    ]


def _selected_images(files):
    images = [upload for upload in files if upload and upload.filename]
    if not images:
        raise ValueError("PNG 또는 JPG 페이지를 한 장 이상 선택해 주세요.")
    if len(images) > MAX_PAGES:
        raise ValueError(f"한 리플렛에는 최대 {MAX_PAGES}장까지 올릴 수 있습니다.")
    for upload in images:
        extension = Path(upload.filename).suffix.lower()
        if extension not in IMAGE_EXTENSIONS:
            raise ValueError("PNG, JPG, JPEG 이미지만 올릴 수 있습니다.")
    return sorted(images, key=_natural_image_key)


def _save_page_image(upload, folder: Path):
    raw_name = Path((upload.filename or "").replace("\\", "/")).name
    extension = Path(raw_name).suffix.lower()
    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"‘{raw_name}’ 파일이 20MB를 초과합니다.")
    try:
        with Image.open(upload.stream) as image:
            image.verify()
            if image.format not in IMAGE_FORMATS:
                raise ValueError("PNG, JPG, JPEG 이미지만 올릴 수 있습니다.")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"‘{raw_name}’은(는) 정상적인 이미지 파일이 아닙니다.") from exc
    finally:
        upload.stream.seek(0)

    safe_name = secure_filename(raw_name) or f"page{extension}"
    stored_name = f"{uuid.uuid4().hex}{extension}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored_name
    upload.save(path)
    return safe_name, str(path)


def _leaflet_pages(conn, ebook_id: int):
    return conn.execute(
        """SELECT id,page_no,image_filename,image_path
           FROM ebook_pages WHERE ebook_id=? ORDER BY page_no""",
        (ebook_id,),
    ).fetchall()


def _reader_context(book, pages):
    public_url = url_for("ebook.public_reader", token=book["share_token"], _external=True)
    return {
        "book": book,
        "pages": pages,
        "public_url": public_url,
        "can_manage": _is_staff(),
    }


@ebook_bp.route("/")
def library():
    query = (request.args.get("q") or "").strip()
    where = "WHERE e.kind='leaflet'"
    params = []
    if query:
        where += " AND (e.title LIKE ? OR e.description LIKE ?)"
        params.extend((f"%{query}%", f"%{query}%"))
    conn = get_db()
    try:
        books = conn.execute(
            f"""
            SELECT e.*, COUNT(p.id) AS page_count
            FROM ebooks e LEFT JOIN ebook_pages p ON p.ebook_id=e.id
            {where}
            GROUP BY e.id ORDER BY e.updated_at DESC,e.id DESC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return render_template(
        "ebook/library.html", books=books, query=query, can_manage=_is_staff()
    )


@ebook_bp.route("/new", methods=["GET", "POST"])
def create_book():
    _require_staff()
    if request.method == "GET":
        return render_template("ebook/form.html")

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not title:
        flash("제목을 입력해 주세요.", "error")
        return render_template("ebook/form.html"), 400
    if len(title) > 200 or len(description) > 2000:
        flash("제목 또는 소개 글이 너무 깁니다.", "error")
        return render_template("ebook/form.html"), 400
    try:
        images = _selected_images(request.files.getlist("page_images"))
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("ebook/form.html"), 400

    conn = get_db()
    leaflet_dir = None
    try:
        token = secrets.token_urlsafe(24)
        creator = session.get("user_name") or str(session.get("emp_no"))
        cursor = conn.execute(
            """INSERT INTO ebooks
               (title,author,description,cover_filename,cover_path,source_filename,
                content_text,page_char_limit,created_by,kind,share_token)
               VALUES (?,?,?,'','','','',1800,?,'leaflet',?)""",
            (title, creator, description, creator, token),
        )
        ebook_id = cursor.lastrowid
        leaflet_dir = LEAFLET_ROOT / str(ebook_id)
        saved_pages = []
        for page_no, upload in enumerate(images, 1):
            filename, path = _save_page_image(upload, leaflet_dir)
            saved_pages.append((ebook_id, page_no, "", filename, path))
        conn.executemany(
            """INSERT INTO ebook_pages
               (ebook_id,page_no,content_html,image_filename,image_path)
               VALUES (?,?,?,?,?)""",
            saved_pages,
        )
        first_filename, first_path = saved_pages[0][3], saved_pages[0][4]
        conn.execute(
            """UPDATE ebooks SET cover_filename=?,cover_path=?,source_filename=?
               WHERE id=?""",
            (first_filename, first_path, first_filename, ebook_id),
        )
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        if leaflet_dir and leaflet_dir.is_dir():
            shutil.rmtree(leaflet_dir, ignore_errors=True)
        flash(str(exc), "error")
        return render_template("ebook/form.html"), 400
    except Exception:
        conn.rollback()
        if leaflet_dir and leaflet_dir.is_dir():
            shutil.rmtree(leaflet_dir, ignore_errors=True)
        raise
    finally:
        conn.close()

    flash(f"‘{title}’ e리플렛을 {len(images)}페이지로 만들었습니다.", "success")
    return redirect(url_for("ebook.library"))


@ebook_bp.route("/<int:ebook_id>")
def read_book(ebook_id):
    conn = get_db()
    try:
        book = _get_leaflet(conn, ebook_id)
    finally:
        conn.close()
    return redirect(url_for("ebook.public_reader", token=book["share_token"]))


@ebook_bp.route("/view/<token>")
def public_reader(token):
    conn = get_db()
    try:
        book = _get_shared_leaflet(conn, token)
        pages = _leaflet_pages(conn, book["id"])
    finally:
        conn.close()
    return render_template("ebook/reader.html", **_reader_context(book, pages))


@ebook_bp.route("/<int:ebook_id>/edit", methods=["GET", "POST"])
def edit_book(ebook_id):
    _require_staff()
    conn = get_db()
    replacement_dir = None
    backup_dir = None
    final_dir = LEAFLET_ROOT / str(ebook_id)
    filesystem_swapped = False
    try:
        book = _get_leaflet(conn, ebook_id)
        if request.method == "POST":
            title = (request.form.get("title") or "").strip()
            description = (request.form.get("description") or "").strip()
            if not title:
                flash("제목을 입력해 주세요.", "error")
                return redirect(url_for("ebook.edit_book", ebook_id=ebook_id))
            if len(title) > 200 or len(description) > 2000:
                flash("제목 또는 소개 글이 너무 깁니다.", "error")
                return redirect(url_for("ebook.edit_book", ebook_id=ebook_id))

            uploads = [item for item in request.files.getlist("page_images") if item.filename]
            new_pages = []
            if uploads:
                images = _selected_images(uploads)
                replacement_dir = LEAFLET_ROOT / f"{ebook_id}_replacement_{uuid.uuid4().hex}"
                for page_no, upload in enumerate(images, 1):
                    filename, path = _save_page_image(upload, replacement_dir)
                    new_pages.append((ebook_id, page_no, "", filename, path))

                # 새 파일을 먼저 완성한 뒤 폴더를 교체한다. DB 저장이
                # 실패하면 백업 폴더로 즉시 되돌릴 수 있다.
                backup_dir = LEAFLET_ROOT / f"{ebook_id}_backup_{uuid.uuid4().hex}"
                if final_dir.is_dir():
                    final_dir.rename(backup_dir)
                replacement_dir.rename(final_dir)
                replacement_dir = None
                filesystem_swapped = True
                new_pages = [
                    (row[0], row[1], row[2], row[3], str(final_dir / Path(row[4]).name))
                    for row in new_pages
                ]
                conn.execute("DELETE FROM ebook_pages WHERE ebook_id=?", (ebook_id,))
                conn.executemany(
                    """INSERT INTO ebook_pages
                       (ebook_id,page_no,content_html,image_filename,image_path)
                       VALUES (?,?,?,?,?)""",
                    new_pages,
                )
                cover_filename, cover_path = new_pages[0][3], new_pages[0][4]
            else:
                cover_filename, cover_path = book["cover_filename"], book["cover_path"]

            conn.execute(
                """UPDATE ebooks SET title=?,description=?,cover_filename=?,cover_path=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (title, description, cover_filename, cover_path, ebook_id),
            )
            conn.commit()
            if backup_dir and backup_dir.is_dir():
                shutil.rmtree(backup_dir, ignore_errors=True)
            flash("리플렛 정보를 수정했습니다.", "success")
            return redirect(url_for("ebook.library"))

        pages = _leaflet_pages(conn, ebook_id)
    except ValueError as exc:
        conn.rollback()
        if replacement_dir and replacement_dir.is_dir():
            shutil.rmtree(replacement_dir, ignore_errors=True)
        if filesystem_swapped:
            if final_dir.is_dir():
                shutil.rmtree(final_dir, ignore_errors=True)
            if backup_dir and backup_dir.is_dir():
                backup_dir.rename(final_dir)
        flash(str(exc), "error")
        return redirect(url_for("ebook.edit_book", ebook_id=ebook_id))
    except Exception:
        conn.rollback()
        if replacement_dir and replacement_dir.is_dir():
            shutil.rmtree(replacement_dir, ignore_errors=True)
        if filesystem_swapped:
            if final_dir.is_dir():
                shutil.rmtree(final_dir, ignore_errors=True)
            if backup_dir and backup_dir.is_dir():
                backup_dir.rename(final_dir)
        raise
    finally:
        conn.close()
    return render_template(
        "ebook/edit.html",
        book=book,
        pages=pages,
        public_url=url_for("ebook.public_reader", token=book["share_token"], _external=True),
    )


@ebook_bp.route("/<int:ebook_id>/cover")
def serve_cover(ebook_id):
    conn = get_db()
    try:
        book = _get_leaflet(conn, ebook_id)
        cover_path = book["cover_path"]
    finally:
        conn.close()
    if not cover_path or not os.path.isfile(cover_path):
        abort(404)
    return send_file(cover_path, conditional=True, max_age=3600)


@ebook_bp.route("/<int:ebook_id>/pages/<int:page_id>/image")
def serve_page_image(ebook_id, page_id):
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT p.image_path FROM ebook_pages p
               JOIN ebooks e ON e.id=p.ebook_id
               WHERE p.id=? AND p.ebook_id=? AND e.kind='leaflet'""",
            (page_id, ebook_id),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["image_path"] or not os.path.isfile(row["image_path"]):
        abort(404)
    return send_file(row["image_path"], conditional=True, max_age=3600)


@ebook_bp.route("/<int:ebook_id>/delete", methods=["POST"])
def delete_book(ebook_id):
    _require_staff()
    conn = get_db()
    directory = LEAFLET_ROOT / str(ebook_id)
    try:
        _get_leaflet(conn, ebook_id)
        conn.execute("DELETE FROM ebook_pages WHERE ebook_id=?", (ebook_id,))
        for table in ("ebook_reviews", "ebook_bookmarks", "ebook_media"):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists:
                conn.execute(f"DELETE FROM {table} WHERE ebook_id=?", (ebook_id,))
        conn.execute("DELETE FROM ebooks WHERE id=?", (ebook_id,))
        conn.commit()
    finally:
        conn.close()
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
    flash("e리플렛을 삭제했습니다.", "success")
    return redirect(url_for("ebook.library"))
