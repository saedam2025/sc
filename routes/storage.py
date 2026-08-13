"""새담 인트라넷의 영구 저장 위치를 한 곳에서 관리한다.

Render에서는 Persistent Disk의 표준 마운트인 ``/mnt/data``를 사용하고,
그 밖의 환경에서는 프로젝트의 ``data`` 폴더를 사용한다. Render에서는
영구 디스크가 빠진 채 임시 파일시스템으로 실행되는 것을 의도적으로 막는다.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent.parent
RENDER_DATA_ROOT = Path("/mnt/data")
WINDOWS_LEGACY_RENDER_ROOT = Path(APP_ROOT.anchor) / "mnt" / "data"


def _truthy_environment(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_render_runtime() -> bool:
    return _truthy_environment("RENDER") or bool(
        os.environ.get("RENDER_SERVICE_ID", "").strip()
    )


def _has_render_persistent_mount(path: Path) -> bool:
    """DATA_DIR가 실제 /mnt/data Persistent Disk 아래인지 확인한다."""
    try:
        path.resolve().relative_to(RENDER_DATA_ROOT)
    except ValueError:
        return False
    return RENDER_DATA_ROOT.is_dir() and os.path.ismount(str(RENDER_DATA_ROOT))


def _detect_data_root() -> Path:
    configured = os.environ.get("DATA_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    elif os.name != "nt" and RENDER_DATA_ROOT.is_dir():
        root = RENDER_DATA_ROOT
    else:
        root = APP_ROOT / "data"

    if (
        os.name != "nt"
        and _is_render_runtime()
        and not _truthy_environment("ALLOW_EPHEMERAL_DATA_ON_RENDER")
        and not _has_render_persistent_mount(root)
    ):
        raise RuntimeError(
            "Render Persistent Disk가 연결되지 않았습니다. /mnt/data에 디스크를 "
            "마운트하고 DATA_DIR=/mnt/data로 설정해야 합니다. 임시 저장소로는 "
            "데이터 보호를 위해 실행하지 않습니다."
        )
    return root


DATA_ROOT = _detect_data_root()
MAIN_DB_FILE = DATA_ROOT / "saedam.db"
LEGACY_CONTRACT_DB_FILE = DATA_ROOT / "contracts.db"

UPLOADS_ROOT = DATA_ROOT / "uploads"
BOARD_UPLOADS = DATA_ROOT / "board_uploads"
CHAT_UPLOADS = DATA_ROOT / "chat_uploads"
MEMO_UPLOADS = DATA_ROOT / "memo_uploads"
AI_MAIL_UPLOADS = DATA_ROOT / "ai_mail_uploads"
PROFILE_ROOT = DATA_ROOT / "id"
SCHOOL_UPLOADS = DATA_ROOT / "school_uploads"
DEPOSIT_UPLOADS = DATA_ROOT / "uploads_deposit"
GALLERY_ROOT = DATA_ROOT / "gallery"
GALLERY_UPLOADS = GALLERY_ROOT / "uploads"
GALLERY_THUMBS = GALLERY_ROOT / "thumbnails"
GALL2_ROOT = DATA_ROOT / "gall2"
CONTRACTS_ROOT = DATA_ROOT / "contracts"
VERIFIED_CONTRACT_ROOT = DATA_ROOT / "verified_contract"
VERIFIED_CONTRACTS_ROOT = VERIFIED_CONTRACT_ROOT / "completed"
VERIFIED_TERMS_ROOT = VERIFIED_CONTRACT_ROOT / "terms"
VERIFIED_STAMP_ROOT = VERIFIED_CONTRACT_ROOT / "stamps"
VERIFIED_LOGO_ROOT = VERIFIED_CONTRACT_ROOT / "logos"
VERIFIED_SIGNATURE_ROOT = VERIFIED_CONTRACT_ROOT / "signatures"
VERIFIED_PDF_FONT_ROOT = VERIFIED_CONTRACT_ROOT / "pdf_fonts"
TERMS_ROOT = DATA_ROOT / "terms"
COMPANY_STAMP_ROOT = DATA_ROOT / "company_stamps"
PDF_FONT_ROOT = DATA_ROOT / "pdf_fonts"
SECURITY_ROOT = DATA_ROOT / "security"
LEGACY_ARCHIVE_ROOT = DATA_ROOT / "legacy_archive"


PERSISTENT_DIRECTORIES = (
    DATA_ROOT,
    UPLOADS_ROOT,
    BOARD_UPLOADS,
    CHAT_UPLOADS,
    MEMO_UPLOADS,
    AI_MAIL_UPLOADS,
    PROFILE_ROOT,
    SCHOOL_UPLOADS,
    DEPOSIT_UPLOADS,
    GALLERY_UPLOADS,
    GALLERY_THUMBS,
    GALL2_ROOT,
    CONTRACTS_ROOT,
    VERIFIED_CONTRACT_ROOT,
    VERIFIED_CONTRACTS_ROOT,
    VERIFIED_TERMS_ROOT,
    VERIFIED_STAMP_ROOT,
    VERIFIED_LOGO_ROOT,
    VERIFIED_SIGNATURE_ROOT,
    VERIFIED_PDF_FONT_ROOT,
    TERMS_ROOT,
    COMPANY_STAMP_ROOT,
    PDF_FONT_ROOT,
    SECURITY_ROOT,
    LEGACY_ARCHIVE_ROOT,
)


def ensure_storage_directories() -> None:
    for directory in PERSISTENT_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)


def _copy_missing_tree(source: Path, target: Path) -> int:
    """기존 로컬 파일을 덮어쓰지 않고 통합 저장소로 복사한다."""
    if not source.is_dir() or source.resolve() == target.resolve():
        return 0
    copied = 0
    for source_file in source.rglob("*"):
        if not source_file.is_file():
            continue
        target_file = target / source_file.relative_to(source)
        if target_file.exists():
            continue
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        copied += 1
    return copied


def bootstrap_legacy_files() -> int:
    """예전 프로젝트 폴더의 영구 파일을 최초 실행 때 자동 통합한다."""
    ensure_storage_directories()
    if DATA_ROOT.resolve() == APP_ROOT.resolve():
        return 0

    mappings = []
    # 과거 Windows에서 '/mnt/data'를 드라이브 루트로 오인해 저장한 자료가
    # 있으면 프로젝트 data 폴더로 한 번만, 덮어쓰기 없이 복사한다.
    if os.name == "nt" and WINDOWS_LEGACY_RENDER_ROOT.is_dir():
        mappings.append((WINDOWS_LEGACY_RENDER_ROOT, DATA_ROOT))
    mappings.extend((
        (APP_ROOT / "chat_uploads", CHAT_UPLOADS),
        (APP_ROOT / "memo_uploads", MEMO_UPLOADS),
        (APP_ROOT / "ai_mail_uploads", AI_MAIL_UPLOADS),
        (APP_ROOT / "id", PROFILE_ROOT),
        (APP_ROOT / "school_uploads", SCHOOL_UPLOADS),
        (APP_ROOT / "static" / "school_uploads", SCHOOL_UPLOADS),
        (APP_ROOT / "uploads_deposit", DEPOSIT_UPLOADS),
        (APP_ROOT / "instance", SECURITY_ROOT),
    ))
    return sum(_copy_missing_tree(source, target) for source, target in mappings)


ensure_storage_directories()
