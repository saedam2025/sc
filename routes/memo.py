"""새담 인트라넷 개인 화이트보드 라우트.

기존 기능(포스트잇, 파일/이미지, 타이머, 이동/크기 저장)을 유지하면서
다음 기능을 추가한다.
1. 포스트잇/파일/이미지 기한 및 사전 알림
2. 항목별 비밀번호 잠금(비밀번호 해시 저장)
3. 잠금 해제 전 메모 내용/파일 경로를 서버에서 차단

app.py에서는 기존처럼 아래 형태로 등록하면 된다.
    app.register_blueprint(memo_bp, url_prefix='/memo')
"""

from __future__ import annotations

import mimetypes
import os
import platform
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    render_template,
    request,
    send_file,
    session,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from .database import get_db


memo_bp = Blueprint("memo", __name__)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

POSTIT_SHAPES = {
    "square",
    "rounded",
    "heart",
    "star",
    "moon",
    "rabbit",
    "circle",
    "cloud",
    "speech",
    "leaf",
    "hexagon",
}


def _storage_root() -> Path:
    if platform.system() != "Windows" and os.path.exists("/mnt/data"):
        return Path("/mnt/data")
    return Path(current_app.root_path)


def _upload_dir() -> Path:
    path = _storage_root() / "memo_uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _owner_aliases() -> list[str]:
    """현재 로그인 사용자를 식별할 수 있는 값을 모두 반환한다.

    신규 자료는 emp_no를 기준으로 저장한다. 기존 memo.py가 사용자 이름으로
    저장했을 가능성도 있으므로 user_name도 조회 별칭으로 함께 사용한다.
    """
    aliases: list[str] = []
    for value in (session.get("emp_no"), session.get("user_name")):
        text = str(value or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    return aliases


def _owner_key() -> str:
    aliases = _owner_aliases()
    if not aliases:
        abort(401)
    return aliases[0]


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(memos)").fetchall()}


def _add_column_if_missing(
    conn: sqlite3.Connection,
    columns: set[str],
    column_name: str,
    definition: str,
) -> None:
    if column_name not in columns:
        conn.execute(f"ALTER TABLE memos ADD COLUMN {column_name} {definition}")
        columns.add(column_name)


def ensure_memo_schema(conn: sqlite3.Connection) -> None:
    """기존 DB를 지우지 않고 필요한 컬럼만 자동 추가한다."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_key TEXT,
            type TEXT NOT NULL DEFAULT 'postit',
            content TEXT,
            color TEXT DEFAULT '#fff9b1',
            shape TEXT NOT NULL DEFAULT 'square',
            filepath TEXT,
            pos_x INTEGER DEFAULT 30,
            pos_y INTEGER DEFAULT 30,
            z_index INTEGER DEFAULT 1,
            width INTEGER,
            height INTEGER,
            due_at TEXT,
            reminder_minutes INTEGER DEFAULT 60,
            password_hash TEXT,
            is_locked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
        """
    )

    columns = _table_columns(conn)
    required_columns = {
        "owner_key": "TEXT",
        "type": "TEXT NOT NULL DEFAULT 'postit'",
        "content": "TEXT",
        "color": "TEXT DEFAULT '#fff9b1'",
        "shape": "TEXT NOT NULL DEFAULT 'square'",
        "filepath": "TEXT",
        "pos_x": "INTEGER DEFAULT 30",
        "pos_y": "INTEGER DEFAULT 30",
        "z_index": "INTEGER DEFAULT 1",
        "width": "INTEGER",
        "height": "INTEGER",
        "due_at": "TEXT",
        "reminder_minutes": "INTEGER DEFAULT 60",
        "password_hash": "TEXT",
        "is_locked": "INTEGER DEFAULT 0",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }
    for name, definition in required_columns.items():
        _add_column_if_missing(conn, columns, name, definition)

    # 기존 구현에서 사용했을 법한 사용자 컬럼을 owner_key로 안전하게 이관한다.
    for legacy_owner_column in ("emp_no", "user_id", "username", "user_name", "owner"):
        if legacy_owner_column in columns:
            conn.execute(
                f"""
                UPDATE memos
                   SET owner_key = CAST({legacy_owner_column} AS TEXT)
                 WHERE (owner_key IS NULL OR TRIM(owner_key) = '')
                   AND {legacy_owner_column} IS NOT NULL
                   AND TRIM(CAST({legacy_owner_column} AS TEXT)) != ''
                """
            )
            break

    conn.execute(
        """
        UPDATE memos
           SET shape='square'
         WHERE shape IS NULL
            OR TRIM(shape)=''
            OR LOWER(TRIM(shape)) NOT IN (
                'square','rounded','heart','star','moon','rabbit',
                'circle','cloud','speech','leaf','hexagon'
            )
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_memos_owner_key ON memos(owner_key)")
    conn.commit()


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _owned_where(prefix: str = "") -> tuple[str, list[str]]:
    aliases = _owner_aliases()
    if not aliases:
        abort(401)
    column = f"{prefix}owner_key" if prefix else "owner_key"
    return f"{column} IN ({_placeholders(len(aliases))})", aliases


def _claim_legacy_unowned_rows(conn: sqlite3.Connection) -> None:
    """소유자 컬럼이 전혀 없던 구버전 DB만 현재 사용자에게 1회 귀속한다.

    기존 테이블에 owner/user_name/emp_no 등이 있으면 ensure_memo_schema에서 이미
    이관되므로 이 로직은 실행되지 않는다.
    """
    columns = _table_columns(conn)
    legacy_columns = {"emp_no", "user_id", "username", "user_name", "owner"}
    if columns.isdisjoint(legacy_columns):
        conn.execute(
            "UPDATE memos SET owner_key=? WHERE owner_key IS NULL OR TRIM(owner_key)=''",
            (_owner_key(),),
        )
        conn.commit()


def _session_unlocked_ids() -> set[int]:
    raw = session.get("memo_unlocked_ids", [])
    result: set[int] = set()
    if isinstance(raw, list):
        for value in raw:
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                continue
    return result


def _remember_unlocked(memo_id: int) -> None:
    unlocked = _session_unlocked_ids()
    unlocked.add(int(memo_id))
    session["memo_unlocked_ids"] = sorted(unlocked)[-200:]
    session.modified = True


def _forget_unlocked(memo_id: int) -> None:
    unlocked = _session_unlocked_ids()
    unlocked.discard(int(memo_id))
    session["memo_unlocked_ids"] = sorted(unlocked)
    session.modified = True


def _is_unlocked(memo: sqlite3.Row | dict[str, Any]) -> bool:
    locked = bool(memo["is_locked"])
    return not locked or int(memo["id"]) in _session_unlocked_ids()


def _get_owned_memo(conn: sqlite3.Connection, memo_id: int) -> sqlite3.Row | None:
    where, params = _owned_where()
    return conn.execute(
        f"SELECT * FROM memos WHERE id=? AND {where}",
        [int(memo_id), *params],
    ).fetchone()

def _next_z_index(conn: sqlite3.Connection) -> int:
    """현재 사용자의 모든 항목 중 가장 높은 레이어보다 1 큰 값을 반환한다."""
    where, params = _owned_where()
    row = conn.execute(
        f"SELECT COALESCE(MAX(z_index), 0) AS max_z FROM memos WHERE {where}",
        params,
    ).fetchone()
    try:
        max_z = int(row["max_z"] or 0) if row else 0
    except (TypeError, ValueError, KeyError, IndexError):
        max_z = 0

    if max_z >= 999_000:
        rows = conn.execute(
            f"SELECT id FROM memos WHERE {where} ORDER BY z_index ASC, id ASC",
            params,
        ).fetchall()
        for new_z, memo_row in enumerate(rows, start=1):
            conn.execute("UPDATE memos SET z_index=? WHERE id=?", (new_z, int(memo_row["id"])))
        conn.commit()
        max_z = len(rows)
    return max_z + 1



def _serialize_memo(row: sqlite3.Row) -> dict[str, Any]:
    memo = dict(row)
    memo["is_locked"] = bool(memo.get("is_locked"))
    memo["is_unlocked"] = _is_unlocked(row)
    memo["reminder_minutes"] = int(memo.get("reminder_minutes") or 60)
    memo["shape"] = _safe_shape(memo.get("shape"))

    # 잠금 상태에서는 템플릿으로 비밀 내용/서버 파일명을 절대 전달하지 않는다.
    if memo["is_locked"] and not memo["is_unlocked"]:
        memo["content"] = ""
        memo["filepath"] = ""
    return memo


def _json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "message": message}), status


def _parse_due_at(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("기한 날짜와 시간을 올바르게 입력해주세요.") from exc
    return parsed.strftime("%Y-%m-%dT%H:%M")


def _parse_reminder_minutes(value: Any) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = 60
    return max(1, min(minutes, 60 * 24 * 30))


def _safe_color(value: Any) -> str:
    text = str(value or "#fff9b1").strip()
    allowed = {"#fff9b1", "#ffd6e0", "#bbf0f3", "#d4f0c0"}
    return text if text in allowed else "#fff9b1"


def _safe_shape(value: Any) -> str:
    shape = str(value or "square").strip().lower()
    return shape if shape in POSTIT_SHAPES else "square"


def _requested_postit_shape(data: dict[str, Any] | None = None) -> str:
    payload = data or {}
    for candidate in (
        payload.get("shape"),
        payload.get("postit_shape"),
        request.args.get("shape"),
        request.args.get("postit_shape"),
        request.form.get("shape"),
        request.form.get("postit_shape"),
    ):
        text = str(candidate or "").strip()
        if text:
            return _safe_shape(text)
    return "square"


def _resolve_file_path(filepath: str) -> Path | None:
    """DB에 저장된 경로를 허용된 저장 영역 안에서만 해석한다."""
    raw = str(filepath or "").strip()
    if not raw:
        return None

    basename = os.path.basename(raw.replace("\\", "/"))
    candidates: list[Path] = [
        _upload_dir() / basename,
        _storage_root() / raw.lstrip("/"),
        Path(current_app.root_path) / raw.lstrip("/"),
        Path(current_app.root_path) / "static" / raw.lstrip("/"),
    ]
    if os.path.isabs(raw):
        candidates.insert(0, Path(raw))

    allowed_roots = [
        _storage_root().resolve(),
        Path(current_app.root_path).resolve(),
    ]

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        if any(resolved == root or root in resolved.parents for root in allowed_roots):
            return resolved
    return None


def _delete_physical_file(filepath: str) -> None:
    path = _resolve_file_path(filepath)
    if path and path.exists():
        try:
            path.unlink()
        except OSError:
            pass


@memo_bp.route("/")
def memo_board():
    conn = get_db()
    try:
        ensure_memo_schema(conn)
        _claim_legacy_unowned_rows(conn)
        where, params = _owned_where()
        rows = conn.execute(
            f"SELECT * FROM memos WHERE {where} ORDER BY z_index ASC, id ASC",
            params,
        ).fetchall()
        memos = [_serialize_memo(row) for row in rows]
        response = make_response(render_template("memo.html", memos=memos))
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    finally:
        conn.close()


@memo_bp.route("/add_postit", methods=["POST"])
def add_postit():
    data = request.get_json(silent=True) or {}
    requested_shape = _requested_postit_shape(data)
    requested_color = _safe_color(data.get("color") or request.args.get("color"))

    conn = get_db()
    try:
        ensure_memo_schema(conn)
        next_z = _next_z_index(conn)
        cursor = conn.execute(
            """
            INSERT INTO memos (
                owner_key, type, content, color, shape, pos_x, pos_y, z_index,
                width, height, due_at, reminder_minutes, is_locked, updated_at
            ) VALUES (?, 'postit', '', ?, ?, 35, 35, ?, 220, 220, NULL, 60, 0, datetime('now','localtime'))
            """,
            (_owner_key(), requested_color, requested_shape, next_z),
        )
        memo_id = int(cursor.lastrowid)
        conn.commit()

        saved = conn.execute("SELECT shape FROM memos WHERE id=?", (memo_id,)).fetchone()
        saved_shape = _safe_shape(saved["shape"] if saved else requested_shape)
        if saved_shape != requested_shape:
            conn.execute(
                "UPDATE memos SET shape=?, updated_at=datetime('now','localtime') WHERE id=?",
                (requested_shape, memo_id),
            )
            conn.commit()
            saved_shape = requested_shape

        return jsonify({"ok": True, "id": memo_id, "shape": saved_shape, "z_index": next_z})
    finally:
        conn.close()


@memo_bp.route("/upload_file", methods=["POST"])
def upload_file():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return _json_error("업로드할 파일을 선택해주세요.")

    # Content-Length가 없거나 부정확할 수 있으므로 스트림 저장 전후 모두 확인한다.
    uploaded.stream.seek(0, os.SEEK_END)
    size = uploaded.stream.tell()
    uploaded.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return _json_error("5MB 이하의 파일만 보드에 붙일 수 있습니다.")

    original_name = os.path.basename(uploaded.filename.replace("\\", "/"))
    clean_name = secure_filename(original_name)
    extension = Path(original_name).suffix.lower()
    stored_name = f"{uuid.uuid4().hex}{extension}"
    save_path = _upload_dir() / stored_name

    password = str(request.form.get("password") or "")
    password_confirm = str(request.form.get("password_confirm") or "")
    if password:
        if len(password) < 4:
            return _json_error("비밀번호는 4자 이상 입력해주세요.")
        if password != password_confirm:
            return _json_error("비밀번호 확인이 일치하지 않습니다.")

    try:
        due_at = _parse_due_at(request.form.get("due_at"))
    except ValueError as exc:
        return _json_error(str(exc))
    reminder_minutes = _parse_reminder_minutes(request.form.get("reminder_minutes"))

    uploaded.save(save_path)
    if save_path.stat().st_size > MAX_UPLOAD_BYTES:
        save_path.unlink(missing_ok=True)
        return _json_error("5MB 이하의 파일만 보드에 붙일 수 있습니다.")

    memo_type = "image" if extension in IMAGE_EXTENSIONS else "file"
    password_hash = generate_password_hash(password) if password else None
    is_locked = 1 if password_hash else 0

    conn = get_db()
    try:
        ensure_memo_schema(conn)
        next_z = _next_z_index(conn)
        cursor = conn.execute(
            """
            INSERT INTO memos (
                owner_key, type, content, color, filepath,
                pos_x, pos_y, z_index, width, height,
                due_at, reminder_minutes, password_hash, is_locked, updated_at
            ) VALUES (?, ?, ?, NULL, ?, 45, 45, ?, ?, NULL, ?, ?, ?, ?, datetime('now','localtime'))
            """,
            (
                _owner_key(),
                memo_type,
                original_name or clean_name or "첨부파일",
                stored_name,
                next_z,
                250 if memo_type == "image" else 140,
                due_at,
                reminder_minutes,
                password_hash,
                is_locked,
            ),
        )
        conn.commit()
        return jsonify({"ok": True, "id": cursor.lastrowid})
    except Exception:
        save_path.unlink(missing_ok=True)
        raise
    finally:
        conn.close()


@memo_bp.route("/add_timer", methods=["POST"])
def add_timer():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "타이머").strip()[:80]
    try:
        minutes = max(0, int(data.get("minutes") or 0))
        seconds = max(0, int(data.get("seconds") or 0))
    except (TypeError, ValueError):
        return _json_error("타이머 시간을 올바르게 입력해주세요.")
    total_seconds = minutes * 60 + seconds
    if total_seconds <= 0:
        return _json_error("타이머 시간을 설정해주세요.")
    if total_seconds > 60 * 60 * 24 * 30:
        return _json_error("타이머는 최대 30일까지 설정할 수 있습니다.")

    end_ms = int(datetime.now().timestamp() * 1000) + total_seconds * 1000
    content = f"{title}|{end_ms}"

    conn = get_db()
    try:
        ensure_memo_schema(conn)
        next_z = _next_z_index(conn)
        cursor = conn.execute(
            """
            INSERT INTO memos (
                owner_key, type, content, pos_x, pos_y, z_index,
                width, height, is_locked, updated_at
            ) VALUES (?, 'timer', ?, 55, 55, ?, 140, 190, 0, datetime('now','localtime'))
            """,
            (_owner_key(), content, next_z),
        )
        conn.commit()
        return jsonify({"ok": True, "id": cursor.lastrowid})
    finally:
        conn.close()


@memo_bp.route("/update", methods=["POST"])
def update_memo():
    data = request.get_json(silent=True) or {}
    try:
        memo_id = int(data.get("id"))
    except (TypeError, ValueError):
        return _json_error("잘못된 메모 번호입니다.")

    conn = get_db()
    try:
        ensure_memo_schema(conn)
        memo = _get_owned_memo(conn, memo_id)
        if not memo:
            return _json_error("메모를 찾을 수 없습니다.", 404)

        allowed_fields = {
            "content": str,
            "shape": str,
            "pos_x": int,
            "pos_y": int,
            "z_index": int,
            "width": int,
            "height": int,
        }
        assignments: list[str] = []
        values: list[Any] = []

        for field, converter in allowed_fields.items():
            if field not in data:
                continue
            if field == "content" and bool(memo["is_locked"]) and not _is_unlocked(memo):
                return _json_error("비밀번호를 먼저 입력해 잠금을 해제해주세요.", 423)
            try:
                value = converter(data[field])
            except (TypeError, ValueError):
                continue
            if field == "shape":
                value = _safe_shape(value)
            elif field in {"pos_x", "pos_y"}:
                value = max(0, min(value, 20000))
            elif field == "z_index":
                value = max(1, min(value, 1000000))
            elif field in {"width", "height"}:
                value = max(80, min(value, 5000))
            elif field == "content":
                value = value[:20000]
            assignments.append(f"{field}=?")
            values.append(value)

        if not assignments:
            return jsonify({"ok": True})

        assignments.append("updated_at=datetime('now','localtime')")
        values.append(memo_id)
        conn.execute(
            f"UPDATE memos SET {', '.join(assignments)} WHERE id=?",
            values,
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@memo_bp.route("/deadline/<int:memo_id>", methods=["POST"])
def update_deadline(memo_id: int):
    data = request.get_json(silent=True) or {}
    try:
        due_at = _parse_due_at(data.get("due_at"))
    except ValueError as exc:
        return _json_error(str(exc))
    reminder_minutes = _parse_reminder_minutes(data.get("reminder_minutes"))

    conn = get_db()
    try:
        ensure_memo_schema(conn)
        memo = _get_owned_memo(conn, memo_id)
        if not memo:
            return _json_error("항목을 찾을 수 없습니다.", 404)
        if memo["type"] == "timer":
            return _json_error("타이머에는 별도의 기한을 설정하지 않습니다.")
        conn.execute(
            """
            UPDATE memos
               SET due_at=?, reminder_minutes=?, updated_at=datetime('now','localtime')
             WHERE id=?
            """,
            (due_at, reminder_minutes, memo_id),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@memo_bp.route("/password/<int:memo_id>", methods=["POST"])
def set_password(memo_id: int):
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")
    confirm = str(data.get("password_confirm") or "")
    if len(password) < 4:
        return _json_error("비밀번호는 4자 이상 입력해주세요.")
    if password != confirm:
        return _json_error("비밀번호 확인이 일치하지 않습니다.")

    conn = get_db()
    try:
        ensure_memo_schema(conn)
        memo = _get_owned_memo(conn, memo_id)
        if not memo:
            return _json_error("항목을 찾을 수 없습니다.", 404)
        if memo["type"] == "timer":
            return _json_error("타이머에는 비밀번호를 설정할 수 없습니다.")
        if bool(memo["is_locked"]) and not _is_unlocked(memo):
            return _json_error("기존 비밀번호로 먼저 잠금을 해제해주세요.", 423)

        conn.execute(
            """
            UPDATE memos
               SET password_hash=?, is_locked=1, updated_at=datetime('now','localtime')
             WHERE id=?
            """,
            (generate_password_hash(password), memo_id),
        )
        conn.commit()
        _forget_unlocked(memo_id)
        return jsonify({"ok": True})
    finally:
        conn.close()


@memo_bp.route("/password/<int:memo_id>", methods=["DELETE"])
def remove_password(memo_id: int):
    conn = get_db()
    try:
        ensure_memo_schema(conn)
        memo = _get_owned_memo(conn, memo_id)
        if not memo:
            return _json_error("항목을 찾을 수 없습니다.", 404)
        if bool(memo["is_locked"]) and not _is_unlocked(memo):
            return _json_error("비밀번호를 먼저 입력해 잠금을 해제해주세요.", 423)
        conn.execute(
            """
            UPDATE memos
               SET password_hash=NULL, is_locked=0, updated_at=datetime('now','localtime')
             WHERE id=?
            """,
            (memo_id,),
        )
        conn.commit()
        _forget_unlocked(memo_id)
        return jsonify({"ok": True})
    finally:
        conn.close()


@memo_bp.route("/unlock/<int:memo_id>", methods=["POST"])
def unlock_memo(memo_id: int):
    data = request.get_json(silent=True) or {}
    password = str(data.get("password") or "")

    conn = get_db()
    try:
        ensure_memo_schema(conn)
        memo = _get_owned_memo(conn, memo_id)
        if not memo:
            return _json_error("항목을 찾을 수 없습니다.", 404)
        if not bool(memo["is_locked"]):
            _remember_unlocked(memo_id)
            return jsonify({"ok": True})
        password_hash = str(memo["password_hash"] or "")
        if not password_hash or not check_password_hash(password_hash, password):
            return _json_error("비밀번호가 올바르지 않습니다.", 401)
        _remember_unlocked(memo_id)
        return jsonify({"ok": True})
    finally:
        conn.close()


@memo_bp.route("/lock/<int:memo_id>", methods=["POST"])
def lock_memo(memo_id: int):
    conn = get_db()
    try:
        ensure_memo_schema(conn)
        memo = _get_owned_memo(conn, memo_id)
        if not memo:
            return _json_error("항목을 찾을 수 없습니다.", 404)
        if not bool(memo["is_locked"]):
            return _json_error("비밀번호가 설정되지 않은 항목입니다.")
        _forget_unlocked(memo_id)
        return jsonify({"ok": True})
    finally:
        conn.close()


@memo_bp.route("/file/<int:memo_id>")
def memo_file(memo_id: int):
    conn = get_db()
    try:
        ensure_memo_schema(conn)
        memo = _get_owned_memo(conn, memo_id)
        if not memo or memo["type"] not in {"image", "file"}:
            abort(404)
        if bool(memo["is_locked"]) and not _is_unlocked(memo):
            abort(403)
        path = _resolve_file_path(str(memo["filepath"] or ""))
        if not path:
            abort(404)

        original_name = os.path.basename(str(memo["content"] or path.name))
        inline = request.args.get("inline") == "1" and memo["type"] == "image"
        guessed_type, _ = mimetypes.guess_type(original_name)
        return send_file(
            path,
            mimetype=guessed_type,
            as_attachment=not inline,
            download_name=original_name,
            conditional=True,
            max_age=0,
        )
    finally:
        conn.close()


@memo_bp.route("/delete/<int:memo_id>", methods=["DELETE"])
def delete_memo(memo_id: int):
    conn = get_db()
    try:
        ensure_memo_schema(conn)
        memo = _get_owned_memo(conn, memo_id)
        if not memo:
            return _json_error("항목을 찾을 수 없습니다.", 404)
        filepath = str(memo["filepath"] or "")
        conn.execute("DELETE FROM memos WHERE id=?", (memo_id,))
        conn.commit()
        _forget_unlocked(memo_id)
        if memo["type"] in {"image", "file"} and filepath:
            _delete_physical_file(filepath)
        return jsonify({"ok": True})
    finally:
        conn.close()
