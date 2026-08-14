"""공통 인증·권한·비밀키 도우미."""

from __future__ import annotations

import hmac
import os
import secrets
from functools import wraps
from pathlib import Path

from flask import abort, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .storage import DATA_ROOT, SECURITY_ROOT, bootstrap_legacy_files

PASSWORD_HASH_PREFIXES = ("scrypt:", "pbkdf2:", "argon2:", "$2a$", "$2b$")
ADMIN_MAX_LEVEL = 2


def is_password_hash(value: object) -> bool:
    text = str(value or "")
    return "$" in text and text.startswith(PASSWORD_HASH_PREFIXES)


def hash_password(password: object) -> str:
    value = str(password or "")
    if not value:
        raise ValueError("비밀번호를 입력해주세요.")
    return generate_password_hash(value, method="scrypt")


def verify_password(stored_password: object, supplied_password: object) -> bool:
    stored = str(stored_password or "")
    supplied = str(supplied_password or "")
    if not stored or not supplied:
        return False
    if is_password_hash(stored):
        try:
            return check_password_hash(stored, supplied)
        except (ValueError, TypeError):
            return False
    # 기존 평문 계정은 마이그레이션 기간에만 호환한다.
    return hmac.compare_digest(stored, supplied)


def upgrade_legacy_password(conn, user_id: int, stored_password: object, supplied_password: object) -> bool:
    if is_password_hash(stored_password):
        return False
    if not verify_password(stored_password, supplied_password):
        return False
    conn.execute(
        "UPDATE users SET password=? WHERE id=?",
        (hash_password(supplied_password), int(user_id)),
    )
    conn.commit()
    return True


def migrate_plaintext_passwords(conn) -> int:
    """기존 평문 사용자 비밀번호를 로그인 값 변경 없이 일괄 해시한다."""
    rows = conn.execute(
        "SELECT id, password FROM users WHERE password IS NOT NULL AND TRIM(password) != ''"
    ).fetchall()
    migrated = 0
    for row in rows:
        if is_password_hash(row["password"]):
            continue
        conn.execute(
            "UPDATE users SET password=? WHERE id=?",
            (hash_password(row["password"]), int(row["id"])),
        )
        migrated += 1
    if migrated:
        conn.commit()
    return migrated


def _session_level(default: int = 99) -> int:
    try:
        return int(session.get("user_level", default))
    except (TypeError, ValueError):
        return default


def is_admin_session(max_level: int = ADMIN_MAX_LEVEL) -> bool:
    emp_no = str(session.get("emp_no") or "").strip().lower()
    user_name = str(session.get("user_name") or "").strip().lower()
    return (
        bool(emp_no)
        and (
            emp_no == "admin"
            or user_name == "admin"
            or _session_level() <= int(max_level)
        )
    )


def has_menu_permission(menu_key: str) -> bool:
    """로그인 세션이 현재 저장된 메뉴 접근권한을 만족하는지 반환한다."""
    if not session.get("emp_no"):
        return False

    # menu_access는 공통 보안 도우미를 import하지 않지만, 향후 의존성 변경에도
    # 순환 import가 생기지 않도록 실제 검사 시점에 불러온다.
    from .menu_access import menu_is_allowed

    return menu_is_allowed(menu_key)


def _permission_denied(status_code: int):
    wants_json = (
        request.is_json
        or request.path.startswith("/api/")
        or "/api/" in request.path
        or request.accept_mimetypes.best == "application/json"
    )
    message = "로그인이 필요합니다." if status_code == 401 else "관리자 권한이 필요합니다."
    if wants_json:
        return jsonify({"status": "error", "message": message}), status_code
    abort(status_code)


def menu_permission_required(menu_key: str):
    """하드코딩된 관리자 레벨 대신 메뉴 권한관리 설정으로 접근을 제한한다."""
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            if not session.get("emp_no"):
                return _permission_denied(401)
            if not has_menu_permission(menu_key):
                return _permission_denied(403)
            return func(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view=None, *, max_level: int = ADMIN_MAX_LEVEL):
    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            if not session.get("emp_no"):
                return _permission_denied(401)
            if not is_admin_session(max_level=max_level):
                return _permission_denied(403)
            return func(*args, **kwargs)

        return wrapped

    if view is None:
        return decorator
    return decorator(view)


def _security_directory() -> Path:
    bootstrap_legacy_files()
    configured = (
        os.environ.get("SECURITY_DATA_DIR", "").strip()
    )
    directory = (
        Path(configured).expanduser().resolve()
        if configured
        else SECURITY_ROOT.resolve()
    )
    render_runtime = os.environ.get("RENDER", "").strip().lower() in {
        "1", "true", "yes", "on"
    } or bool(os.environ.get("RENDER_SERVICE_ID", "").strip())
    allow_ephemeral = os.environ.get(
        "ALLOW_EPHEMERAL_DATA_ON_RENDER", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if render_runtime and not allow_ephemeral:
        try:
            directory.relative_to(DATA_ROOT.resolve())
        except ValueError as exc:
            raise RuntimeError(
                "SECURITY_DATA_DIR는 Render 영구 DATA_DIR 안에 있어야 합니다. "
                "암호화 키가 재배포 때 사라지지 않도록 설정을 확인해 주세요."
            ) from exc
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _read_or_create_secret(env_name: str, filename: str) -> str:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        if len(configured) < 32:
            raise RuntimeError(f"{env_name}는 32자 이상으로 설정해야 합니다.")
        return configured

    path = _security_directory() / filename
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing

    value = secrets.token_urlsafe(64)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if not existing:
            raise RuntimeError(f"비밀키 파일이 비어 있습니다: {path}")
        return existing

    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def load_session_secret() -> str:
    return _read_or_create_secret("SECRET_KEY", "session.key")


def load_credential_secret() -> str:
    return _read_or_create_secret("CREDENTIAL_ENCRYPTION_KEY", "credentials.key")


def load_file_secret() -> str:
    """첨부파일 전용 키를 영속 저장소에서 불러온다."""
    return _read_or_create_secret("FILE_ENCRYPTION_KEY", "files.key")
