from __future__ import annotations

import html
import os
import re
import shutil
import uuid
from datetime import datetime
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, jsonify, redirect, render_template,
    request, send_from_directory, url_for
)
from .storage import MANUAL_UPLOADS
from werkzeug.utils import secure_filename

from routes.database import get_db

manual_bp = Blueprint("manual", __name__)

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "jpe", "jfif", "webp", "gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TXT_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------------
# Storage / DB helpers
# ---------------------------------------------------------------------
def _manual_root() -> Path:
    path = Path(current_app.config.get("MANUAL_UPLOAD_ROOT", str(MANUAL_UPLOADS)))
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _connect():
    """새담인트라넷의 routes.database.get_db()를 그대로 사용합니다."""
    conn = get_db()
    try:
        # 기존 DB 설정에 foreign_keys가 꺼져 있어도 이 연결에서는 활성화합니다.
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_columns(conn, table_name: str):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_column(conn, table_name: str, column_name: str, ddl: str):
    if column_name not in _table_columns(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")


def init_manual_schema() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS manuals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '새 메뉴얼',
                description TEXT NOT NULL DEFAULT '',
                thumbnail TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                published_at TEXT
            );

            CREATE TABLE IF NOT EXISTS manual_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manual_id INTEGER NOT NULL,
                section_no INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                content_html TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (manual_id) REFERENCES manuals(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_manual_sections_manual
            ON manual_sections(manual_id, sort_order);

            CREATE TABLE IF NOT EXISTS manual_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manual_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (manual_id) REFERENCES manuals(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_manual_images_manual
            ON manual_images(manual_id);
            """
        )

        # v5: 썸네일은 본문 첫 이미지와 분리하여 별도 업로드만 사용합니다.
        # 기존 DB에는 아래 컬럼이 없으므로 안전하게 자동 추가합니다.
        _ensure_column(conn, "manuals", "thumbnail_source", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "manuals", "thumbnail_filename", "TEXT NOT NULL DEFAULT ''")


# 이전 패키지명과의 호환용 별칭
init_manual_tables = init_manual_schema

@manual_bp.before_request
def _ensure_tables():
    init_manual_schema()


# ---------------------------------------------------------------------
# Optional write permission hook
# ---------------------------------------------------------------------
def manual_write_required(view):
    """
    Optional integration point.

    In app config:
        app.config["MANUAL_WRITE_GUARD"] = callable

    The callable may:
      - return True / None -> allow
      - return False       -> 403
      - return a Flask Response -> returned immediately

    If no guard is configured, access is allowed so this module can be
    dropped into an existing intranet first. Connect your existing login/
    permission decorator before production use.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        guard = current_app.config.get("MANUAL_WRITE_GUARD")
        if callable(guard):
            result = guard()
            if result is False:
                abort(403)
            if result not in (None, True):
                return result
        return view(*args, **kwargs)
    return wrapped


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rowdict(row):
    return dict(row) if row is not None else None


def _get_manual_or_404(manual_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM manuals WHERE id = ?", (manual_id,)).fetchone()
    if not row:
        abort(404)
    return _rowdict(row)


def _get_sections(manual_id: int):
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM manual_sections
            WHERE manual_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (manual_id,),
        ).fetchall()
    return [_rowdict(r) for r in rows]


# ---------------------------------------------------------------------
# HTML sanitation
# ---------------------------------------------------------------------
def _sanitize_html(value: str) -> str:
    """
    메뉴얼 본문에 필요한 제한된 HTML/CSS만 허용합니다.
    글자색/글자크기, 표 셀 폭·높이를 저장하기 위해 style 속성을 허용하되
    bleach가 있으면 CSS 속성을 화이트리스트로 제한합니다.
    """
    value = value or ""

    allowed_css_properties = [
        "color", "font-size",
        "width", "height", "min-width", "max-width",
        "text-align", "vertical-align",
        "margin-left", "margin-right",
        "table-layout",
    ]

    try:
        import bleach  # type: ignore
        from bleach.css_sanitizer import CSSSanitizer  # type: ignore

        allowed_tags = [
            "p", "br", "strong", "b", "em", "i", "u", "s",
            "h2", "h3", "h4", "ul", "ol", "li",
            "table", "thead", "tbody", "tfoot", "tr", "th", "td",
            "div", "span", "a", "figure", "figcaption", "img",
            "blockquote", "code", "pre", "hr"
        ]
        allowed_attributes = {
            "*": ["class", "style"],
            "a": ["href", "title", "target", "rel", "class", "style"],
            "img": ["src", "alt", "title", "data-filename", "class", "style"],
            "figure": ["data-filename", "class", "style"],
            "td": ["colspan", "rowspan", "class", "style"],
            "th": ["colspan", "rowspan", "class", "style"],
            "table": ["class", "style"],
        }
        css_sanitizer = CSSSanitizer(allowed_css_properties=allowed_css_properties)
        return bleach.clean(
            value,
            tags=allowed_tags,
            attributes=allowed_attributes,
            protocols=["http", "https"],
            css_sanitizer=css_sanitizer,
            strip=True,
        )
    except Exception:
        # bleach가 없는 환경에서도 script/event/javascript URL은 제거합니다.
        value = re.sub(
            r"<\s*(script|iframe|object|embed|style)[^>]*>.*?<\s*/\s*\1\s*>",
            "",
            value,
            flags=re.I | re.S,
        )
        value = re.sub(r'\son\w+\s*=\s*([\'\"]).*?\1', "", value, flags=re.I | re.S)
        value = re.sub(r"\son\w+\s*=\s*[^\s>]+", "", value, flags=re.I)
        value = re.sub(r"javascript\s*:", "", value, flags=re.I)

        # style 속성도 메뉴얼에 필요한 속성만 남깁니다.
        safe_props = set(allowed_css_properties)

        def clean_style(match):
            quote = match.group(1)
            raw = match.group(2)
            cleaned = []
            for item in raw.split(";"):
                if ":" not in item:
                    continue
                prop, val = item.split(":", 1)
                prop = prop.strip().lower()
                val = val.strip()
                low = val.lower()
                if prop not in safe_props:
                    continue
                if any(bad in low for bad in ("url(", "expression", "javascript:", "behavior:", "<", ">")):
                    continue
                cleaned.append(f"{prop}:{val}")
            if not cleaned:
                return ""
            return f' style={quote}{";".join(cleaned)}{quote}'

        value = re.sub(
            r'\sstyle\s*=\s*([\'\"])(.*?)\1',
            clean_style,
            value,
            flags=re.I | re.S,
        )
        return value


def _first_image_url(sections) -> str:
    for sec in sections:
        content = sec.get("content_html", "")
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content, flags=re.I)
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------------
# TXT parser
# ---------------------------------------------------------------------
def _text_lines_to_html(lines):
    out = []
    list_mode = None

    def close_list():
        nonlocal list_mode
        if list_mode:
            out.append(f"</{list_mode}>")
            list_mode = None

    for raw in lines:
        line = raw.rstrip()

        if not line.strip():
            close_list()
            continue

        if line.startswith("### "):
            close_list()
            out.append(f'<h3 class="sub">{html.escape(line[4:].strip())}</h3>')
        elif line.startswith("- "):
            if list_mode != "ul":
                close_list()
                list_mode = "ul"
                out.append("<ul>")
            out.append(f"<li>{html.escape(line[2:].strip())}</li>")
        elif re.match(r"^\d+\.\s+", line):
            if list_mode != "ol":
                close_list()
                list_mode = "ol"
                out.append("<ol>")
            item = re.sub(r"^\d+\.\s+", "", line)
            out.append(f"<li>{html.escape(item.strip())}</li>")
        else:
            close_list()
            out.append(f"<p>{html.escape(line.strip())}</p>")

    close_list()
    return "\n".join(out)


def _parse_txt(text: str):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    title = ""
    sections = []
    current = None

    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            if not title:
                title = line[2:].strip()
            continue

        if line.startswith("## "):
            if current is not None:
                current["content_html"] = _text_lines_to_html(current.pop("_lines"))
                sections.append(current)
            current = {
                "title": line[3:].strip() or "제목 없음",
                "description": "",
                "_lines": [],
            }
            continue

        if current is None:
            current = {"title": "내용", "description": "", "_lines": []}
        current["_lines"].append(line)

    if current is not None:
        current["content_html"] = _text_lines_to_html(current.pop("_lines"))
        sections.append(current)

    sections = [s for s in sections if s["title"].strip() or s["content_html"].strip()]
    if not sections:
        sections = [{"title": "내용", "description": "", "content_html": ""}]

    return {
        "title": title,
        "sections": sections,
    }


# ---------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------
@manual_bp.get("/")
def list_manuals():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                m.*,
                COUNT(s.id) AS section_count
            FROM manuals m
            LEFT JOIN manual_sections s ON s.manual_id = m.id
            GROUP BY m.id
            ORDER BY
                CASE WHEN m.status = 'published' THEN 0 ELSE 1 END,
                COALESCE(m.published_at, m.updated_at) DESC,
                m.id DESC
            """
        ).fetchall()
    manuals = [_rowdict(r) for r in rows]
    return render_template("manual/manual_list.html", manuals=manuals)


@manual_bp.get("/new")
@manual_write_required
def new_manual():
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO manuals(title, description, status, created_at, updated_at)
            VALUES(?, ?, 'draft', ?, ?)
            """,
            ("새 메뉴얼", "", now, now),
        )
        manual_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO manual_sections(
                manual_id, section_no, title, description, content_html, sort_order
            ) VALUES(?, 1, ?, '', '', 0)
            """,
            (manual_id, "1. 새 목차"),
        )
    return redirect(url_for("manual.edit_manual", manual_id=manual_id))


@manual_bp.get("/<int:manual_id>/edit")
@manual_write_required
def edit_manual(manual_id):
    manual = _get_manual_or_404(manual_id)
    sections = _get_sections(manual_id)
    return render_template(
        "manual/manual_editor.html",
        manual=manual,
        sections=sections,
        manual_payload={"manual": manual, "sections": sections},
    )


@manual_bp.get("/<int:manual_id>/preview")
@manual_write_required
def preview_manual(manual_id):
    manual = _get_manual_or_404(manual_id)
    sections = _get_sections(manual_id)
    return render_template(
        "manual/manual_preview.html",
        manual=manual,
        sections=sections,
        is_preview=True,
    )


@manual_bp.get("/<int:manual_id>")
def view_manual(manual_id):
    manual = _get_manual_or_404(manual_id)
    if manual["status"] != "published":
        # Drafts are intentionally not publicly viewable.
        abort(404)
    sections = _get_sections(manual_id)
    return render_template(
        "manual/manual_view.html",
        manual=manual,
        sections=sections,
        is_preview=False,
    )


# ---------------------------------------------------------------------
# Save / publish / delete
# ---------------------------------------------------------------------
@manual_bp.post("/<int:manual_id>/save")
@manual_write_required
def save_manual(manual_id):
    _get_manual_or_404(manual_id)
    payload = request.get_json(silent=True) or {}

    title = (payload.get("title") or "").strip()[:200]
    description = (payload.get("description") or "").strip()[:1000]
    raw_sections = payload.get("sections") or []

    if not title:
        return jsonify(ok=False, message="메뉴얼 제목을 입력해 주세요."), 400
    if not isinstance(raw_sections, list) or not raw_sections:
        return jsonify(ok=False, message="목차를 한 개 이상 만들어 주세요."), 400

    sections = []
    for idx, sec in enumerate(raw_sections):
        sec_title = str(sec.get("title") or "").strip()[:200]
        if not sec_title:
            sec_title = f"{idx + 1}. 제목 없음"
        sections.append(
            {
                "section_no": idx + 1,
                "title": sec_title,
                "description": str(sec.get("description") or "").strip()[:1000],
                "content_html": _sanitize_html(str(sec.get("content_html") or "")),
                "sort_order": idx,
            }
        )

    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE manuals
            SET title = ?, description = ?, updated_at = ?
            WHERE id = ?
            """,
            (title, description, now, manual_id),
        )
        conn.execute("DELETE FROM manual_sections WHERE manual_id = ?", (manual_id,))
        conn.executemany(
            """
            INSERT INTO manual_sections(
                manual_id, section_no, title, description, content_html, sort_order
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    manual_id,
                    sec["section_no"],
                    sec["title"],
                    sec["description"],
                    sec["content_html"],
                    sec["sort_order"],
                )
                for sec in sections
            ],
        )

    return jsonify(ok=True, message="임시저장되었습니다.", updated_at=now)


@manual_bp.post("/<int:manual_id>/publish")
@manual_write_required
def publish_manual(manual_id):
    manual = _get_manual_or_404(manual_id)
    sections = _get_sections(manual_id)

    if not manual["title"].strip():
        return jsonify(ok=False, message="제목을 입력해 주세요."), 400
    if not sections:
        return jsonify(ok=False, message="목차가 없습니다."), 400

    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE manuals
            SET status = 'published',
                published_at = COALESCE(published_at, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, manual_id),
        )

    return jsonify(
        ok=True,
        message="작성완료되었습니다.",
        view_url=url_for("manual.view_manual", manual_id=manual_id),
    )


@manual_bp.post("/<int:manual_id>/unpublish")
@manual_write_required
def unpublish_manual(manual_id):
    _get_manual_or_404(manual_id)
    now = _now()
    with _connect() as conn:
        conn.execute(
            "UPDATE manuals SET status='draft', updated_at=? WHERE id=?",
            (now, manual_id),
        )
    return jsonify(ok=True, message="작성중 상태로 변경되었습니다.")


@manual_bp.post("/<int:manual_id>/delete")
@manual_write_required
def delete_manual(manual_id):
    _get_manual_or_404(manual_id)
    with _connect() as conn:
        conn.execute("DELETE FROM manuals WHERE id = ?", (manual_id,))

    folder = _manual_root() / str(manual_id)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)

    return jsonify(ok=True, message="메뉴얼이 삭제되었습니다.")


# ---------------------------------------------------------------------
# Image upload / delete / media
# ---------------------------------------------------------------------
@manual_bp.post("/api/upload-image")
@manual_write_required
def upload_image():
    try:
        manual_id = int(request.form.get("manual_id", "0"))
    except ValueError:
        manual_id = 0

    _get_manual_or_404(manual_id)

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, message="이미지 파일을 선택해 주세요."), 400

    # 중요:
    # 한글 파일명에 secure_filename()을 먼저 적용하면
    # 예: "캡처이미지.jpg" -> 확장자 판별이 깨지는 환경이 있을 수 있습니다.
    # 따라서 원본 파일명에서 먼저 확장자를 읽고, 실제 서버 저장명만 UUID로 만듭니다.
    original = str(file.filename).replace("\\\\", "/").rsplit("/", 1)[-1].strip()
    ext = Path(original).suffix.lower().lstrip(".")

    # 브라우저/캡처 프로그램이 확장자를 누락하거나 특이하게 전달하는 경우
    # MIME 타입으로 한 번 더 JPEG/PNG/WEBP/GIF 여부를 확인합니다.
    mime_ext_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/pjpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        ext = mime_ext_map.get((file.mimetype or "").lower(), "")

    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify(
            ok=False,
            message="PNG, JPG, JPEG, JFIF, WEBP, GIF 이미지만 업로드할 수 있습니다.",
        ), 400

    # JPEG 계열은 서버 저장 확장자를 jpg로 통일합니다.
    if ext in {"jpeg", "jpe", "jfif"}:
        ext = "jpg"

    # DB에는 사용자가 올린 원래 한글 파일명을 보존합니다.
    original = original[:255]

    # Read once to enforce module-level size cap.
    data = file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify(ok=False, message="이미지는 8MB 이하만 업로드할 수 있습니다."), 400

    filename = f"{uuid.uuid4().hex}.{ext}"
    folder = _manual_root() / str(manual_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(data)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO manual_images(manual_id, filename, original_name, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (manual_id, filename, original, _now()),
        )

    return jsonify(
        ok=True,
        filename=filename,
        url=url_for("manual.media", manual_id=manual_id, filename=filename),
    )


@manual_bp.post("/api/delete-image")
@manual_write_required
def delete_image():
    payload = request.get_json(silent=True) or {}
    try:
        manual_id = int(payload.get("manual_id", 0))
    except (TypeError, ValueError):
        manual_id = 0
    filename = secure_filename(str(payload.get("filename") or ""))

    _get_manual_or_404(manual_id)
    if not filename:
        return jsonify(ok=False, message="삭제할 이미지가 없습니다."), 400

    with _connect() as conn:
        exists = conn.execute(
            "SELECT id FROM manual_images WHERE manual_id=? AND filename=?",
            (manual_id, filename),
        ).fetchone()
        if not exists:
            return jsonify(ok=False, message="등록된 이미지를 찾을 수 없습니다."), 404
        conn.execute(
            "DELETE FROM manual_images WHERE manual_id=? AND filename=?",
            (manual_id, filename),
        )

    path = _manual_root() / str(manual_id) / filename
    if path.exists():
        path.unlink()

    return jsonify(ok=True)



@manual_bp.post("/api/upload-thumbnail")
@manual_write_required
def upload_thumbnail():
    try:
        manual_id = int(request.form.get("manual_id", "0"))
    except ValueError:
        manual_id = 0

    _get_manual_or_404(manual_id)

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, message="썸네일 이미지 파일을 선택해 주세요."), 400

    original = str(file.filename).replace("\\\\", "/").rsplit("/", 1)[-1].strip()
    ext = Path(original).suffix.lower().lstrip(".")

    mime_ext_map = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/pjpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        ext = mime_ext_map.get((file.mimetype or "").lower(), "")

    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return jsonify(
            ok=False,
            message="PNG, JPG, JPEG, JFIF, WEBP, GIF 이미지만 업로드할 수 있습니다.",
        ), 400

    if ext in {"jpeg", "jpe", "jfif"}:
        ext = "jpg"

    data = file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        return jsonify(ok=False, message="썸네일 이미지는 8MB 이하만 업로드할 수 있습니다."), 400

    filename = f"thumb_{uuid.uuid4().hex}.{ext}"
    folder = _manual_root() / str(manual_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / filename).write_bytes(data)

    old_filename = ""
    url = url_for("manual.media", manual_id=manual_id, filename=filename)
    with _connect() as conn:
        row = conn.execute(
            "SELECT thumbnail_filename FROM manuals WHERE id=?",
            (manual_id,),
        ).fetchone()
        if row:
            old_filename = str(row["thumbnail_filename"] or "")
        conn.execute(
            """
            UPDATE manuals
            SET thumbnail=?, thumbnail_source='uploaded',
                thumbnail_filename=?, updated_at=?
            WHERE id=?
            """,
            (url, filename, _now(), manual_id),
        )

    if old_filename and old_filename != filename:
        old_path = folder / secure_filename(old_filename)
        if old_path.exists():
            old_path.unlink()

    return jsonify(ok=True, url=url, filename=filename)


@manual_bp.post("/api/delete-thumbnail")
@manual_write_required
def delete_thumbnail():
    payload = request.get_json(silent=True) or {}
    try:
        manual_id = int(payload.get("manual_id", 0))
    except (TypeError, ValueError):
        manual_id = 0

    _get_manual_or_404(manual_id)

    old_filename = ""
    with _connect() as conn:
        row = conn.execute(
            "SELECT thumbnail_filename FROM manuals WHERE id=?",
            (manual_id,),
        ).fetchone()
        if row:
            old_filename = str(row["thumbnail_filename"] or "")
        conn.execute(
            """
            UPDATE manuals
            SET thumbnail='', thumbnail_source='', thumbnail_filename='', updated_at=?
            WHERE id=?
            """,
            (_now(), manual_id),
        )

    if old_filename:
        path = _manual_root() / str(manual_id) / secure_filename(old_filename)
        if path.exists():
            path.unlink()

    return jsonify(ok=True, message="썸네일을 삭제했습니다.")


@manual_bp.get("/media/<int:manual_id>/<path:filename>")
def media(manual_id, filename):
    safe = secure_filename(filename)
    if safe != filename:
        abort(404)
    folder = _manual_root() / str(manual_id)
    return send_from_directory(folder, safe)


# ---------------------------------------------------------------------
# TXT import
# ---------------------------------------------------------------------
@manual_bp.post("/api/upload-txt")
@manual_write_required
def upload_txt():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, message="TXT 파일을 선택해 주세요."), 400

    if not file.filename.lower().endswith(".txt"):
        return jsonify(ok=False, message=".txt 파일만 불러올 수 있습니다."), 400

    data = file.read(MAX_TXT_BYTES + 1)
    if len(data) > MAX_TXT_BYTES:
        return jsonify(ok=False, message="TXT 파일은 2MB 이하만 불러올 수 있습니다."), 400

    text = None
    for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            pass

    if text is None:
        return jsonify(ok=False, message="TXT 파일의 문자 인코딩을 읽을 수 없습니다."), 400

    parsed = _parse_txt(text)
    return jsonify(ok=True, **parsed)
