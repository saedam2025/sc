"""웹툰 회차 이미지를 세로로 이어 보는 [통합관리] > Webtoon 기능.

- 웹툰 1편 = 제목/저자/장르/표지 + (선택) 열람 비밀번호
- 웹툰 1편 안에 1화~N화의 회차를 등록하고, 회차마다 이미지 여러 장을
  올리면 업로드 순서대로 위에서 아래로 이어지는 한 장처럼 보여준다.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from PIL import Image, UnidentifiedImageError

from .database import get_db
from .secure_files import (
    encrypted_response,
    encrypted_storage_name,
    encrypt_upload,
    original_filename,
)
from .security import hash_password, is_admin_session, verify_password
from .storage import WEBTOON_UPLOADS


webtoon_bp = Blueprint("webtoon", __name__)

WEBTOON_ROOT = Path(WEBTOON_UPLOADS)
COVER_ROOT = WEBTOON_ROOT / "covers"
EPISODE_ROOT = WEBTOON_ROOT / "episodes"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_COVER_BYTES = 12 * 1024 * 1024
MAX_PAGES_PER_EPISODE = 300
UNLOCK_SESSION_KEY = "webtoon_unlocked"

GENRE_CHOICES = (
    "일상", "개그", "드라마", "액션", "판타지", "로맨스",
    "스릴러", "학습", "사내소식", "기타",
)


def init_webtoon_schema():
    """웹툰 테이블과 업로드 폴더를 준비한다(기존 데이터는 보존)."""
    for directory in (WEBTOON_ROOT, COVER_ROOT, EPISODE_ROOT):
        directory.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS webtoons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                genre TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                cover_filename TEXT,
                cover_path TEXT,
                password_hash TEXT,
                created_by TEXT NOT NULL DEFAULT '',
                created_by_emp_no TEXT NOT NULL DEFAULT '',
                view_count INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS webtoon_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                webtoon_id INTEGER NOT NULL,
                episode_no INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                page_count INTEGER NOT NULL DEFAULT 0,
                view_count INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '',
                created_by_emp_no TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(webtoon_id, episode_no)
            );
            CREATE TABLE IF NOT EXISTS webtoon_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                page_no INTEGER NOT NULL,
                image_filename TEXT NOT NULL DEFAULT '',
                image_path TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                UNIQUE(episode_id, page_no)
            );
            CREATE TABLE IF NOT EXISTS webtoon_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                webtoon_id INTEGER NOT NULL,
                episode_id INTEGER NOT NULL,
                user_key TEXT NOT NULL,
                scroll_ratio REAL NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(episode_id, user_key)
            );
            CREATE INDEX IF NOT EXISTS idx_webtoon_episodes_book
                ON webtoon_episodes(webtoon_id, episode_no);
            CREATE INDEX IF NOT EXISTS idx_webtoon_pages_episode
                ON webtoon_pages(episode_id, page_no);
            CREATE INDEX IF NOT EXISTS idx_webtoon_progress_user
                ON webtoon_progress(user_key, webtoon_id);
        """)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------


def _require_staff() -> None:
    if not session.get("emp_no"):
        abort(401)


def _user_key() -> str:
    return str(session.get("emp_no") or session.get("user_name") or "").strip()


def _current_name() -> str:
    return str(session.get("user_name") or session.get("emp_no") or "").strip()


def _get_webtoon(conn, webtoon_id: int):
    row = conn.execute("SELECT * FROM webtoons WHERE id=?", (webtoon_id,)).fetchone()
    if not row:
        abort(404)
    return row


def _get_episode(conn, webtoon_id: int, episode_id: int):
    row = conn.execute(
        "SELECT * FROM webtoon_episodes WHERE id=? AND webtoon_id=?",
        (episode_id, webtoon_id),
    ).fetchone()
    if not row:
        abort(404)
    return row


def _is_owner(row) -> bool:
    owner = str(row["created_by_emp_no"] or "").strip()
    return bool(owner) and owner == str(session.get("emp_no") or "").strip()


def _is_master_admin() -> bool:
    return str(session.get("emp_no") or "").strip().lower() == "admin"


def _can_manage(row) -> bool:
    """등록자 본인 또는 관리자만 수정·삭제할 수 있다."""
    return _is_owner(row) or is_admin_session()


def _unlocked_ids() -> set:
    values = session.get(UNLOCK_SESSION_KEY) or []
    return {int(value) for value in values if str(value).isdigit()}


def _mark_unlocked(webtoon_id: int) -> None:
    unlocked = _unlocked_ids()
    unlocked.add(int(webtoon_id))
    session[UNLOCK_SESSION_KEY] = sorted(unlocked)
    session.modified = True


def _is_locked(book) -> bool:
    """비밀번호가 걸린 웹툰인지, 그리고 아직 안 풀렸는지 반환한다.

    이 메뉴는 [통합관리] 안에 있어 열람자 대부분이 관리자 레벨이다. 관리자라는
    이유로 잠금을 통과시키면 비밀번호가 무의미해지므로, 등록자 본인과 최고관리자
    계정만 예외로 둔다. (비밀번호를 잊었을 때는 관리자가 정보 수정 화면에서
    다시 설정하거나 해제할 수 있다.)
    """
    if not str(book["password_hash"] or "").strip():
        return False
    if _is_owner(book) or _is_master_admin():
        return False
    return int(book["id"]) not in _unlocked_ids()


def _require_unlocked(book):
    if _is_locked(book):
        return redirect(url_for("webtoon.unlock", webtoon_id=book["id"]))
    return None


def _natural_key(name: str):
    """`2화_10.png`가 `2화_9.png` 뒤에 오도록 숫자를 숫자로 비교한다."""
    base = Path(str(name or "").replace("\\", "/")).name.casefold()
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", base)
    ]


def _sorted_uploads(files):
    images = [upload for upload in files if upload and upload.filename]
    if not images:
        raise ValueError("웹툰 이미지 파일을 한 장 이상 선택해 주세요.")
    if len(images) > MAX_PAGES_PER_EPISODE:
        raise ValueError(
            f"한 회차에는 최대 {MAX_PAGES_PER_EPISODE}장까지 올릴 수 있습니다."
        )
    for upload in images:
        if Path(upload.filename).suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError("JPG, PNG, WEBP, GIF 이미지만 올릴 수 있습니다.")
    return sorted(images, key=lambda upload: _natural_key(upload.filename))


def _inspect_image(upload, max_bytes: int):
    """용량·형식을 검사하고 (원본이름, 가로, 세로)를 돌려준다."""
    raw_name = original_filename(upload.filename, "page")
    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size > max_bytes:
        raise ValueError(
            f"‘{raw_name}’ 파일이 {max_bytes // (1024 * 1024)}MB를 초과합니다."
        )
    try:
        with Image.open(upload.stream) as image:
            image.verify()
        upload.stream.seek(0)
        with Image.open(upload.stream) as image:
            image_format = image.format
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"‘{raw_name}’은(는) 정상적인 이미지 파일이 아닙니다.") from exc
    finally:
        upload.stream.seek(0)
    if image_format not in IMAGE_FORMATS:
        raise ValueError("JPG, PNG, WEBP, GIF 이미지만 올릴 수 있습니다.")
    return raw_name, int(width or 0), int(height or 0)


def _store_image(upload, folder: Path, max_bytes: int):
    raw_name, width, height = _inspect_image(upload, max_bytes)
    stored_name = encrypted_storage_name(raw_name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored_name
    encrypt_upload(upload, path)
    return raw_name, str(path), width, height


def _episode_dir(webtoon_id: int, episode_id: int) -> Path:
    return EPISODE_ROOT / str(webtoon_id) / str(episode_id)


def _clean_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _next_episode_no(conn, webtoon_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(episode_no), 0) AS last_no FROM webtoon_episodes WHERE webtoon_id=?",
        (webtoon_id,),
    ).fetchone()
    return int(row["last_no"] or 0) + 1


def _form_context(book=None, form=None):
    return {
        "book": book,
        "form": form or {},
        "genres": GENRE_CHOICES,
    }


# ---------------------------------------------------------------------------
# 웹툰 서재
# ---------------------------------------------------------------------------


@webtoon_bp.route("/")
def library():
    _require_staff()
    query = _clean_text(request.args.get("q"), 100)
    genre = _clean_text(request.args.get("genre"), 40)
    where = []
    params = []
    if query:
        where.append("(w.title LIKE ? OR w.author LIKE ? OR w.description LIKE ?)")
        params.extend((f"%{query}%", f"%{query}%", f"%{query}%"))
    if genre:
        where.append("w.genre = ?")
        params.append(genre)
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    conn = get_db()
    try:
        books = conn.execute(
            f"""
            SELECT w.*,
                   COUNT(e.id) AS episode_count,
                   MAX(e.updated_at) AS last_episode_at
            FROM webtoons w
            LEFT JOIN webtoon_episodes e ON e.webtoon_id = w.id
            {clause}
            GROUP BY w.id
            ORDER BY COALESCE(MAX(e.updated_at), w.updated_at) DESC, w.id DESC
            """,
            params,
        ).fetchall()
        genre_rows = conn.execute(
            "SELECT DISTINCT genre FROM webtoons WHERE TRIM(genre) <> '' ORDER BY genre"
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        "webtoon/library.html",
        books=books,
        query=query,
        genre=genre,
        genres=[row["genre"] for row in genre_rows],
        locked_map={int(row["id"]): _is_locked(row) for row in books},
        can_manage_map={int(row["id"]): _can_manage(row) for row in books},
    )


@webtoon_bp.route("/new", methods=["GET", "POST"])
def create_webtoon():
    _require_staff()
    if request.method == "GET":
        return render_template("webtoon/form.html", **_form_context())

    form = {
        "title": _clean_text(request.form.get("title"), 120),
        "author": _clean_text(request.form.get("author"), 60),
        "genre": _clean_text(request.form.get("genre"), 40),
        "description": _clean_text(request.form.get("description"), 2000),
    }
    password = str(request.form.get("password") or "").strip()
    cover = request.files.get("cover")

    if not form["title"]:
        flash("웹툰 제목을 입력해 주세요.", "error")
        return render_template("webtoon/form.html", **_form_context(form=form)), 400
    if not cover or not cover.filename:
        flash("표지 이미지를 선택해 주세요.", "error")
        return render_template("webtoon/form.html", **_form_context(form=form)), 400
    if Path(cover.filename).suffix.lower() not in IMAGE_EXTENSIONS:
        flash("표지는 JPG, PNG, WEBP, GIF 이미지만 사용할 수 있습니다.", "error")
        return render_template("webtoon/form.html", **_form_context(form=form)), 400
    if password and len(password) < 2:
        flash("열람 비밀번호는 2자 이상으로 정해 주세요.", "error")
        return render_template("webtoon/form.html", **_form_context(form=form)), 400

    try:
        cover_name, cover_path, _w, _h = _store_image(cover, COVER_ROOT, MAX_COVER_BYTES)
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("webtoon/form.html", **_form_context(form=form)), 400

    conn = get_db()
    try:
        cursor = conn.execute(
            """INSERT INTO webtoons
               (title, author, genre, description, cover_filename, cover_path,
                password_hash, created_by, created_by_emp_no)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                form["title"], form["author"], form["genre"], form["description"],
                cover_name, cover_path,
                hash_password(password) if password else None,
                _current_name(), str(session.get("emp_no") or ""),
            ),
        )
        webtoon_id = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()

    if password:
        _mark_unlocked(webtoon_id)
    flash(f"‘{form['title']}’ 웹툰을 등록했습니다. 이제 회차를 올려 보세요.", "success")
    return redirect(url_for("webtoon.episodes", webtoon_id=webtoon_id))


@webtoon_bp.route("/<int:webtoon_id>/edit", methods=["GET", "POST"])
def edit_webtoon(webtoon_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        if not _can_manage(book):
            abort(403)
        if request.method == "GET":
            return render_template("webtoon/form.html", **_form_context(book=book))

        form = {
            "title": _clean_text(request.form.get("title"), 120),
            "author": _clean_text(request.form.get("author"), 60),
            "genre": _clean_text(request.form.get("genre"), 40),
            "description": _clean_text(request.form.get("description"), 2000),
        }
        if not form["title"]:
            flash("웹툰 제목을 입력해 주세요.", "error")
            return render_template(
                "webtoon/form.html", **_form_context(book=book, form=form)
            ), 400

        cover_name = book["cover_filename"]
        cover_path = book["cover_path"]
        old_cover = None
        cover = request.files.get("cover")
        if cover and cover.filename:
            if Path(cover.filename).suffix.lower() not in IMAGE_EXTENSIONS:
                flash("표지는 JPG, PNG, WEBP, GIF 이미지만 사용할 수 있습니다.", "error")
                return render_template(
                    "webtoon/form.html", **_form_context(book=book, form=form)
                ), 400
            try:
                cover_name, cover_path, _w, _h = _store_image(
                    cover, COVER_ROOT, MAX_COVER_BYTES
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template(
                    "webtoon/form.html", **_form_context(book=book, form=form)
                ), 400
            old_cover = book["cover_path"]

        password_hash = book["password_hash"]
        password_mode = str(request.form.get("password_mode") or "keep").strip()
        if password_mode == "clear":
            password_hash = None
        elif password_mode == "set":
            password = str(request.form.get("password") or "").strip()
            if len(password) < 2:
                flash("열람 비밀번호는 2자 이상으로 정해 주세요.", "error")
                return render_template(
                    "webtoon/form.html", **_form_context(book=book, form=form)
                ), 400
            password_hash = hash_password(password)

        conn.execute(
            """UPDATE webtoons
               SET title=?, author=?, genre=?, description=?,
                   cover_filename=?, cover_path=?, password_hash=?,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (
                form["title"], form["author"], form["genre"], form["description"],
                cover_name, cover_path, password_hash, webtoon_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if old_cover and old_cover != cover_path and os.path.isfile(old_cover):
        try:
            os.remove(old_cover)
        except OSError:
            pass
    _mark_unlocked(webtoon_id)
    flash("웹툰 정보를 수정했습니다.", "success")
    return redirect(url_for("webtoon.episodes", webtoon_id=webtoon_id))


@webtoon_bp.route("/<int:webtoon_id>/delete", methods=["POST"])
def delete_webtoon(webtoon_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        if not _can_manage(book):
            abort(403)
        episode_ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM webtoon_episodes WHERE webtoon_id=?", (webtoon_id,)
            ).fetchall()
        ]
        if episode_ids:
            placeholders = ",".join("?" for _ in episode_ids)
            conn.execute(
                f"DELETE FROM webtoon_pages WHERE episode_id IN ({placeholders})",
                episode_ids,
            )
        conn.execute("DELETE FROM webtoon_progress WHERE webtoon_id=?", (webtoon_id,))
        conn.execute("DELETE FROM webtoon_episodes WHERE webtoon_id=?", (webtoon_id,))
        conn.execute("DELETE FROM webtoons WHERE id=?", (webtoon_id,))
        conn.commit()
        cover_path = book["cover_path"]
        title = book["title"]
    finally:
        conn.close()

    directory = EPISODE_ROOT / str(webtoon_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
    if cover_path and os.path.isfile(cover_path):
        try:
            os.remove(cover_path)
        except OSError:
            pass
    flash(f"‘{title}’ 웹툰을 삭제했습니다.", "success")
    return redirect(url_for("webtoon.library"))


# ---------------------------------------------------------------------------
# 비밀번호 잠금
# ---------------------------------------------------------------------------


@webtoon_bp.route("/<int:webtoon_id>/unlock", methods=["GET", "POST"])
def unlock(webtoon_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
    finally:
        conn.close()

    if not _is_locked(book):
        return redirect(url_for("webtoon.episodes", webtoon_id=webtoon_id))
    if request.method == "GET":
        return render_template("webtoon/unlock.html", book=book)

    supplied = str(request.form.get("password") or "").strip()
    if not verify_password(book["password_hash"], supplied):
        flash("비밀번호가 올바르지 않습니다.", "error")
        return render_template("webtoon/unlock.html", book=book), 403
    _mark_unlocked(webtoon_id)
    return redirect(url_for("webtoon.episodes", webtoon_id=webtoon_id))


# ---------------------------------------------------------------------------
# 회차 목록 · 등록
# ---------------------------------------------------------------------------


@webtoon_bp.route("/<int:webtoon_id>")
def episodes(webtoon_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        gate = _require_unlocked(book)
        if gate is not None:
            return gate
        rows = conn.execute(
            """SELECT e.*,
                      (SELECT p.id FROM webtoon_pages p
                        WHERE p.episode_id = e.id ORDER BY p.page_no LIMIT 1) AS thumb_page_id
               FROM webtoon_episodes e
               WHERE e.webtoon_id=?
               ORDER BY e.episode_no ASC""",
            (webtoon_id,),
        ).fetchall()
        progress_rows = conn.execute(
            """SELECT episode_id, scroll_ratio FROM webtoon_progress
               WHERE webtoon_id=? AND user_key=?""",
            (webtoon_id, _user_key()),
        ).fetchall()
        conn.execute(
            "UPDATE webtoons SET view_count = view_count + 1 WHERE id=?", (webtoon_id,)
        )
        conn.commit()
    finally:
        conn.close()

    progress = {
        int(row["episode_id"]): round(float(row["scroll_ratio"] or 0) * 100)
        for row in progress_rows
    }
    resume_id = None
    if progress:
        resume_id = max(progress, key=lambda key: progress[key])
    return render_template(
        "webtoon/episodes.html",
        book=book,
        episodes=rows,
        progress=progress,
        resume_id=resume_id,
        can_manage=_can_manage(book),
    )


@webtoon_bp.route("/<int:webtoon_id>/episodes/new", methods=["GET", "POST"])
def create_episode(webtoon_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        gate = _require_unlocked(book)
        if gate is not None:
            return gate
        next_no = _next_episode_no(conn, webtoon_id)
        if request.method == "GET":
            return render_template(
                "webtoon/episode_form.html",
                book=book, episode=None, next_no=next_no, form={},
            )

        form = {
            "title": _clean_text(request.form.get("title"), 120),
            "episode_no": _clean_text(request.form.get("episode_no"), 10),
        }
        try:
            episode_no = int(form["episode_no"] or next_no)
        except ValueError:
            episode_no = next_no
        if episode_no < 1 or episode_no > 99999:
            flash("회차 번호는 1~99999 사이로 입력해 주세요.", "error")
            return render_template(
                "webtoon/episode_form.html",
                book=book, episode=None, next_no=next_no, form=form,
            ), 400

        try:
            uploads = _sorted_uploads(request.files.getlist("page_images"))
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template(
                "webtoon/episode_form.html",
                book=book, episode=None, next_no=next_no, form=form,
            ), 400

        duplicate = conn.execute(
            "SELECT 1 FROM webtoon_episodes WHERE webtoon_id=? AND episode_no=?",
            (webtoon_id, episode_no),
        ).fetchone()
        if duplicate:
            flash(f"{episode_no}화는 이미 등록되어 있습니다.", "error")
            return render_template(
                "webtoon/episode_form.html",
                book=book, episode=None, next_no=next_no, form=form,
            ), 400

        folder = None
        try:
            cursor = conn.execute(
                """INSERT INTO webtoon_episodes
                   (webtoon_id, episode_no, title, page_count, created_by, created_by_emp_no)
                   VALUES (?,?,?,?,?,?)""",
                (
                    webtoon_id, episode_no, form["title"], 0,
                    _current_name(), str(session.get("emp_no") or ""),
                ),
            )
            episode_id = cursor.lastrowid
            folder = _episode_dir(webtoon_id, episode_id)
            rows = []
            for page_no, upload in enumerate(uploads, 1):
                name, path, width, height = _store_image(
                    upload, folder, MAX_IMAGE_BYTES
                )
                rows.append((episode_id, page_no, name, path, width, height))
            conn.executemany(
                """INSERT INTO webtoon_pages
                   (episode_id, page_no, image_filename, image_path, width, height)
                   VALUES (?,?,?,?,?,?)""",
                rows,
            )
            conn.execute(
                "UPDATE webtoon_episodes SET page_count=? WHERE id=?",
                (len(rows), episode_id),
            )
            conn.execute(
                "UPDATE webtoons SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (webtoon_id,),
            )
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            if folder and folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
            flash(str(exc), "error")
            return render_template(
                "webtoon/episode_form.html",
                book=book, episode=None, next_no=next_no, form=form,
            ), 400
        except Exception:
            conn.rollback()
            if folder and folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
            raise
    finally:
        conn.close()

    flash(f"{episode_no}화를 {len(uploads)}장으로 등록했습니다.", "success")
    return redirect(url_for("webtoon.viewer", webtoon_id=webtoon_id, episode_id=episode_id))


@webtoon_bp.route("/<int:webtoon_id>/episodes/<int:episode_id>/edit", methods=["GET", "POST"])
def edit_episode(webtoon_id, episode_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        gate = _require_unlocked(book)
        if gate is not None:
            return gate
        episode = _get_episode(conn, webtoon_id, episode_id)
        if not (_can_manage(book) or _can_manage(episode)):
            abort(403)
        next_no = episode["episode_no"]
        if request.method == "GET":
            return render_template(
                "webtoon/episode_form.html",
                book=book, episode=episode, next_no=next_no, form={},
            )

        form = {
            "title": _clean_text(request.form.get("title"), 120),
            "episode_no": _clean_text(request.form.get("episode_no"), 10),
        }
        try:
            episode_no = int(form["episode_no"] or episode["episode_no"])
        except ValueError:
            episode_no = int(episode["episode_no"])
        duplicate = conn.execute(
            "SELECT 1 FROM webtoon_episodes WHERE webtoon_id=? AND episode_no=? AND id<>?",
            (webtoon_id, episode_no, episode_id),
        ).fetchone()
        if duplicate:
            flash(f"{episode_no}화는 이미 등록되어 있습니다.", "error")
            return render_template(
                "webtoon/episode_form.html",
                book=book, episode=episode, next_no=next_no, form=form,
            ), 400

        uploads = [
            upload for upload in request.files.getlist("page_images")
            if upload and upload.filename
        ]
        replace_images = bool(uploads)
        old_paths = []
        if replace_images:
            try:
                uploads = _sorted_uploads(uploads)
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template(
                    "webtoon/episode_form.html",
                    book=book, episode=episode, next_no=next_no, form=form,
                ), 400

        try:
            if replace_images:
                old_paths = [
                    row["image_path"]
                    for row in conn.execute(
                        "SELECT image_path FROM webtoon_pages WHERE episode_id=?",
                        (episode_id,),
                    ).fetchall()
                ]
                conn.execute("DELETE FROM webtoon_pages WHERE episode_id=?", (episode_id,))
                folder = _episode_dir(webtoon_id, episode_id)
                rows = []
                for page_no, upload in enumerate(uploads, 1):
                    name, path, width, height = _store_image(
                        upload, folder, MAX_IMAGE_BYTES
                    )
                    rows.append((episode_id, page_no, name, path, width, height))
                conn.executemany(
                    """INSERT INTO webtoon_pages
                       (episode_id, page_no, image_filename, image_path, width, height)
                       VALUES (?,?,?,?,?,?)""",
                    rows,
                )
                conn.execute(
                    "UPDATE webtoon_episodes SET page_count=? WHERE id=?",
                    (len(rows), episode_id),
                )
            conn.execute(
                """UPDATE webtoon_episodes
                   SET episode_no=?, title=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (episode_no, form["title"], episode_id),
            )
            conn.execute(
                "UPDATE webtoons SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (webtoon_id,),
            )
            conn.commit()
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), "error")
            return render_template(
                "webtoon/episode_form.html",
                book=book, episode=episode, next_no=next_no, form=form,
            ), 400
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    for path in old_paths:
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    flash(f"{episode_no}화를 수정했습니다.", "success")
    return redirect(url_for("webtoon.episodes", webtoon_id=webtoon_id))


@webtoon_bp.route("/<int:webtoon_id>/episodes/<int:episode_id>/delete", methods=["POST"])
def delete_episode(webtoon_id, episode_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        gate = _require_unlocked(book)
        if gate is not None:
            return gate
        episode = _get_episode(conn, webtoon_id, episode_id)
        if not (_can_manage(book) or _can_manage(episode)):
            abort(403)
        conn.execute("DELETE FROM webtoon_pages WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM webtoon_progress WHERE episode_id=?", (episode_id,))
        conn.execute("DELETE FROM webtoon_episodes WHERE id=?", (episode_id,))
        conn.commit()
        episode_no = episode["episode_no"]
    finally:
        conn.close()

    folder = _episode_dir(webtoon_id, episode_id)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    flash(f"{episode_no}화를 삭제했습니다.", "success")
    return redirect(url_for("webtoon.episodes", webtoon_id=webtoon_id))


# ---------------------------------------------------------------------------
# 컷 순서 편집
# 파일명 숫자와 실제 이야기 순서가 다를 때(9번 컷이 7번 컷 앞에 와야 하는 등)
# 썸네일을 끌어다 놓아 순서를 고치고 그 결과를 page_no로 저장한다.
# ---------------------------------------------------------------------------


@webtoon_bp.route("/<int:webtoon_id>/episodes/<int:episode_id>/pages")
def edit_pages(webtoon_id, episode_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        gate = _require_unlocked(book)
        if gate is not None:
            return gate
        episode = _get_episode(conn, webtoon_id, episode_id)
        if not (_can_manage(book) or _can_manage(episode)):
            abort(403)
        pages = conn.execute(
            """SELECT id, page_no, image_filename, width, height
               FROM webtoon_pages WHERE episode_id=? ORDER BY page_no""",
            (episode_id,),
        ).fetchall()
    finally:
        conn.close()
    return render_template(
        "webtoon/page_editor.html", book=book, episode=episode, pages=pages
    )


@webtoon_bp.route(
    "/<int:webtoon_id>/episodes/<int:episode_id>/pages/order", methods=["POST"]
)
def save_page_order(webtoon_id, episode_id):
    """컷 순서와 삭제할 컷을 한 번에 반영한다.

    화면에서는 남길 컷(order)과 지울 컷(remove)을 모두 보내고, 둘을 합친
    목록이 서버가 가진 컷 전체와 일치할 때만 저장한다. 이렇게 하면 다른
    창에서 이미지를 교체·삭제한 뒤 저장해 엉뚱한 컷이 지워지는 일이 없다.
    """
    _require_staff()
    payload = request.get_json(silent=True) or {}
    raw_order = payload.get("order")
    raw_remove = payload.get("remove") or []
    if not isinstance(raw_order, list) or not isinstance(raw_remove, list):
        return jsonify({"status": "error", "message": "컷 정보가 올바르지 않습니다."}), 400
    if not raw_order and not raw_remove:
        return jsonify({"status": "error", "message": "정렬할 컷 정보가 없습니다."}), 400
    try:
        order = [int(value) for value in raw_order]
        remove = [int(value) for value in raw_remove]
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "컷 정보가 올바르지 않습니다."}), 400

    conn = get_db()
    removed_paths = []
    try:
        book = _get_webtoon(conn, webtoon_id)
        if _is_locked(book):
            return jsonify({"status": "error", "message": "잠긴 웹툰입니다."}), 403
        episode = _get_episode(conn, webtoon_id, episode_id)
        if not (_can_manage(book) or _can_manage(episode)):
            return jsonify(
                {"status": "error", "message": "이 회차를 편집할 권한이 없습니다."}
            ), 403

        rows = conn.execute(
            "SELECT id, image_path FROM webtoon_pages WHERE episode_id=? ORDER BY page_no",
            (episode_id,),
        ).fetchall()
        current = [int(row["id"]) for row in rows]
        # 남길 컷 + 지울 컷이 이 회차의 컷 전체와 정확히 일치해야 한다.
        # (중복이나 다른 회차의 컷 번호도 여기서 함께 걸러진다.)
        if sorted(order + remove) != sorted(current):
            return jsonify({
                "status": "error",
                "message": "그 사이 회차 구성이 바뀌었습니다. 새로고침 후 다시 시도해 주세요.",
            }), 409
        if not remove and order == current:
            return jsonify(
                {"status": "ok", "changed": False, "count": len(order), "removed": 0}
            )

        if remove:
            paths = {int(row["id"]): row["image_path"] for row in rows}
            removed_paths = [paths.get(page_id) for page_id in remove]
            placeholders = ",".join("?" for _ in remove)
            conn.execute(
                f"DELETE FROM webtoon_pages WHERE episode_id=? AND id IN ({placeholders})",
                (episode_id, *remove),
            )

        # page_no에 UNIQUE(episode_id, page_no) 제약이 있어 곧바로 덮어쓰면
        # 중간에 번호가 겹친다. 음수로 한 번 옮겼다가 부호만 되돌린다.
        if order:
            conn.executemany(
                "UPDATE webtoon_pages SET page_no=? WHERE id=? AND episode_id=?",
                [(-index, page_id, episode_id) for index, page_id in enumerate(order, 1)],
            )
            conn.execute(
                "UPDATE webtoon_pages SET page_no=-page_no WHERE episode_id=? AND page_no<0",
                (episode_id,),
            )
        conn.execute(
            """UPDATE webtoon_episodes
               SET page_count=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (len(order), episode_id),
        )
        conn.execute(
            "UPDATE webtoons SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (webtoon_id,)
        )
        conn.commit()
    finally:
        conn.close()

    # 이미지 파일은 DB 반영이 끝난 뒤에 지운다.
    for path in removed_paths:
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    return jsonify(
        {"status": "ok", "changed": True, "count": len(order), "removed": len(remove)}
    )


# ---------------------------------------------------------------------------
# 뷰어
# ---------------------------------------------------------------------------


@webtoon_bp.route("/<int:webtoon_id>/episodes/<int:episode_id>")
def viewer(webtoon_id, episode_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        gate = _require_unlocked(book)
        if gate is not None:
            return gate
        episode = _get_episode(conn, webtoon_id, episode_id)
        pages = conn.execute(
            """SELECT id, page_no, width, height FROM webtoon_pages
               WHERE episode_id=? ORDER BY page_no""",
            (episode_id,),
        ).fetchall()
        siblings = conn.execute(
            """SELECT id, episode_no, title FROM webtoon_episodes
               WHERE webtoon_id=? ORDER BY episode_no""",
            (webtoon_id,),
        ).fetchall()
        progress_row = conn.execute(
            "SELECT scroll_ratio FROM webtoon_progress WHERE episode_id=? AND user_key=?",
            (episode_id, _user_key()),
        ).fetchone()
        conn.execute(
            "UPDATE webtoon_episodes SET view_count = view_count + 1 WHERE id=?",
            (episode_id,),
        )
        conn.commit()
    finally:
        conn.close()

    order = [int(row["id"]) for row in siblings]
    index = order.index(episode_id) if episode_id in order else -1
    prev_episode = siblings[index - 1] if index > 0 else None
    next_episode = siblings[index + 1] if 0 <= index < len(siblings) - 1 else None
    return render_template(
        "webtoon/viewer.html",
        book=book,
        episode=episode,
        pages=pages,
        siblings=siblings,
        prev_episode=prev_episode,
        next_episode=next_episode,
        resume_ratio=float(progress_row["scroll_ratio"]) if progress_row else 0.0,
        can_manage=(_can_manage(book) or _can_manage(episode)),
    )


@webtoon_bp.route("/<int:webtoon_id>/episodes/<int:episode_id>/progress", methods=["POST"])
def save_progress(webtoon_id, episode_id):
    _require_staff()
    payload = request.get_json(silent=True) or {}
    try:
        ratio = float(payload.get("ratio", 0))
    except (TypeError, ValueError):
        ratio = 0.0
    ratio = min(max(ratio, 0.0), 1.0)
    user_key = _user_key()
    if not user_key:
        return jsonify({"status": "error", "message": "세션이 만료되었습니다."}), 401

    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        if _is_locked(book):
            return jsonify({"status": "error", "message": "잠긴 웹툰입니다."}), 403
        _get_episode(conn, webtoon_id, episode_id)
        conn.execute(
            """INSERT INTO webtoon_progress
                   (webtoon_id, episode_id, user_key, scroll_ratio, updated_at)
               VALUES (?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(episode_id, user_key) DO UPDATE SET
                   scroll_ratio=excluded.scroll_ratio,
                   updated_at=CURRENT_TIMESTAMP""",
            (webtoon_id, episode_id, user_key, ratio),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"status": "ok", "ratio": ratio})


# ---------------------------------------------------------------------------
# 이미지 서빙
# ---------------------------------------------------------------------------


@webtoon_bp.route("/<int:webtoon_id>/cover")
def serve_cover(webtoon_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
    finally:
        conn.close()
    path = book["cover_path"]
    if not path or not os.path.isfile(path):
        abort(404)
    return encrypted_response(
        path, book["cover_filename"] or "cover.jpg", as_attachment=False
    )


@webtoon_bp.route("/<int:webtoon_id>/episodes/<int:episode_id>/pages/<int:page_id>")
def serve_page(webtoon_id, episode_id, page_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_webtoon(conn, webtoon_id)
        if _is_locked(book):
            abort(403)
        row = conn.execute(
            """SELECT p.image_path, p.image_filename
               FROM webtoon_pages p
               JOIN webtoon_episodes e ON e.id = p.episode_id
               WHERE p.id=? AND p.episode_id=? AND e.webtoon_id=?""",
            (page_id, episode_id, webtoon_id),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["image_path"] or not os.path.isfile(row["image_path"]):
        abort(404)
    return encrypted_response(
        row["image_path"], row["image_filename"] or "page.jpg", as_attachment=False
    )
