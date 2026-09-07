"""사진을 책처럼 넘겨 보는 [통합관리] > Webtoon > 웹전자책 기능.

- e리플렛(ebook)의 이미지 페이지 구성 방식을 그대로 따르되, 전자책 한 권마다
  열람 비밀번호를 걸 수 있고 서재에서 등록·수정·삭제로 관리한다.
- 사진뿐 아니라 PDF도 올릴 수 있다. PDF는 올리는 순간 쪽마다 이미지로 바뀌어
  저장되므로 뷰어·순서 변경·표지 지정이 사진과 똑같이 동작한다.
- 뷰어는 넓은 화면에서 왼쪽 한 장 / 오른쪽 한 장의 펼침면을 보여주고,
  모바일에서는 한 장씩 실제 책장을 넘기듯 3D로 넘긴다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from io import BytesIO
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from PIL import Image, UnidentifiedImageError

try:  # PDF를 쪽마다 이미지로 바꿔 주는 라이브러리
    import pypdfium2 as pdfium
except ImportError:  # 설치되지 않은 서버에서는 PDF 업로드만 막고 나머지는 그대로 쓴다.
    pdfium = None

from .database import get_db
from .secure_files import (
    encrypt_bytes,
    encrypted_response,
    encrypted_storage_name,
    encrypt_upload,
    original_filename,
)
from .security import hash_password, is_admin_session, verify_password
from .storage import PHOTOBOOK_UPLOADS

photobook_bp = Blueprint("photobook", __name__)

PHOTOBOOK_ROOT = Path(PHOTOBOOK_UPLOADS)
COVER_ROOT = PHOTOBOOK_ROOT / "covers"
PAGE_ROOT = PHOTOBOOK_ROOT / "pages"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
PDF_EXTENSIONS = {".pdf"}
# 페이지로 올릴 수 있는 파일. PDF는 쪽마다 이미지 한 장으로 펼쳐서 저장한다.
PAGE_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS
UPLOAD_FORMAT_MESSAGE = "JPG, PNG, WEBP, GIF 이미지나 PDF 파일만 올릴 수 있습니다."
COVER_FORMAT_MESSAGE = "표지는 JPG, PNG, WEBP, GIF 이미지나 PDF 파일만 사용할 수 있습니다."
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_COVER_BYTES = 12 * 1024 * 1024
MAX_PAGES = 300
# PDF 한 쪽을 이미지로 만들 때의 해상도(dpi)와 긴 변 최대 픽셀
PDF_RENDER_DPI = 150
PDF_MAX_SIDE = 2400
PDF_JPEG_QUALITY = 88
# 폴더를 한꺼번에 끌어다 놓을 때 한 번에 만들 수 있는 전자책 권수
MAX_BATCH_BOOKS = 30
UNLOCK_SESSION_KEY = "photobook_unlocked"

# 메뉴 이름을 '웹화보집'에서 '웹전자책'으로 바꾸면서 예전 이름으로 쌓인
# 이용통계를 새 이름 쪽으로 합쳐 준다.
LEGACY_MENU_NAME = "웹화보집"
MENU_NAME = "웹전자책"

THEME_CHOICES = (
    "행사", "여행", "인물", "풍경", "일상",
    "교육활동", "홍보", "기록", "기타",
)

# 서재 정렬. 기본값(recent)은 예전 서재 정렬과 같은 순서를 유지한다.
DEFAULT_SORT = "recent"
SORT_ORDERS = {
    "recent": "b.updated_at DESC, b.id DESC",
    "oldest": "b.updated_at ASC, b.id ASC",
    "title": "b.title COLLATE NOCASE ASC, b.id ASC",
    "title_desc": "b.title COLLATE NOCASE DESC, b.id DESC",
}
SORT_CHOICES = (
    ("recent", "최신순"),
    ("oldest", "오래된순"),
    ("title", "이름순 (ㄱ→ㅎ)"),
    ("title_desc", "이름 역순 (ㅎ→ㄱ)"),
)


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def merge_legacy_menu_stats(conn) -> int:
    """예전 메뉴 이름('웹화보집')으로 쌓인 이용통계를 '웹전자책'으로 합친다.

    같은 회원이 두 이름을 모두 갖고 있으면 조회수는 더하고, 처음/마지막
    이용 시각은 각각 가장 이르고 늦은 값으로 남긴 뒤 예전 행을 지운다.
    한 번 합쳐지면 예전 이름 행이 없어져 다음 실행부터는 그냥 지나간다.
    """
    moved = 0
    if _table_exists(conn, "usage_user_menu_totals"):
        legacy = conn.execute(
            """SELECT emp_no, user_name, access_count, first_used, last_used
               FROM usage_user_menu_totals WHERE menu_name=?""",
            (LEGACY_MENU_NAME,),
        ).fetchall()
        for row in legacy:
            current = conn.execute(
                """SELECT access_count, first_used, last_used
                   FROM usage_user_menu_totals WHERE emp_no=? AND menu_name=?""",
                (row["emp_no"], MENU_NAME),
            ).fetchone()
            if current is None:
                conn.execute(
                    "UPDATE usage_user_menu_totals SET menu_name=? WHERE emp_no=? AND menu_name=?",
                    (MENU_NAME, row["emp_no"], LEGACY_MENU_NAME),
                )
            else:
                # 시각은 'YYYY-MM-DD HH:MM:SS' 문자열이라 사전순 비교가 곧 시간순이다.
                firsts = [v for v in (current["first_used"], row["first_used"]) if v]
                lasts = [v for v in (current["last_used"], row["last_used"]) if v]
                conn.execute(
                    """UPDATE usage_user_menu_totals
                       SET access_count=?, first_used=?, last_used=?
                       WHERE emp_no=? AND menu_name=?""",
                    (
                        int(current["access_count"] or 0) + int(row["access_count"] or 0),
                        min(firsts) if firsts else None,
                        max(lasts) if lasts else None,
                        row["emp_no"], MENU_NAME,
                    ),
                )
                conn.execute(
                    "DELETE FROM usage_user_menu_totals WHERE emp_no=? AND menu_name=?",
                    (row["emp_no"], LEGACY_MENU_NAME),
                )
            moved += 1
    if _table_exists(conn, "usage_logs"):
        conn.execute(
            "UPDATE usage_logs SET menu_name=? WHERE menu_name=?",
            (MENU_NAME, LEGACY_MENU_NAME),
        )
    return moved


def init_photobook_schema():
    """전자책 테이블과 업로드 폴더를 준비한다(기존 데이터는 보존)."""
    for directory in (PHOTOBOOK_ROOT, COVER_ROOT, PAGE_ROOT):
        directory.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS photobooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                theme TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                cover_filename TEXT,
                cover_path TEXT,
                password_hash TEXT,
                view_count INTEGER NOT NULL DEFAULT 0,
                created_by TEXT NOT NULL DEFAULT '',
                created_by_emp_no TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS photobook_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                photobook_id INTEGER NOT NULL,
                page_no INTEGER NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                image_filename TEXT NOT NULL DEFAULT '',
                image_path TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(photobook_id, page_no)
            );
            CREATE INDEX IF NOT EXISTS idx_photobook_pages_book
                ON photobook_pages(photobook_id, page_no);
        """)
        merge_legacy_menu_stats(conn)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 공통 도우미
# ---------------------------------------------------------------------------


def _require_staff() -> None:
    if not session.get("emp_no"):
        abort(401)


def _current_name() -> str:
    return str(session.get("user_name") or session.get("emp_no") or "").strip()


def _clean_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _get_book(conn, photobook_id: int):
    book = conn.execute(
        "SELECT * FROM photobooks WHERE id=?", (photobook_id,)
    ).fetchone()
    if not book:
        abort(404)
    return book


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


def _mark_unlocked(photobook_id: int) -> None:
    unlocked = _unlocked_ids()
    unlocked.add(int(photobook_id))
    session[UNLOCK_SESSION_KEY] = sorted(unlocked)
    session.modified = True


def _is_locked(book) -> bool:
    """비밀번호가 걸린 전자책인지, 그리고 아직 안 풀렸는지 반환한다.

    [통합관리] 안의 메뉴라 열람자 대부분이 관리자 레벨이다. 관리자라는 이유로
    잠금을 통과시키면 비밀번호가 무의미해지므로 등록자 본인과 최고관리자
    계정만 예외로 둔다.
    """
    if not str(book["password_hash"] or "").strip():
        return False
    if _is_owner(book) or _is_master_admin():
        return False
    return int(book["id"]) not in _unlocked_ids()


def _require_unlocked(book):
    if _is_locked(book):
        return redirect(url_for("photobook.unlock", photobook_id=book["id"]))
    return None


def _natural_key(name: str):
    """`사진_10.jpg`가 `사진_9.jpg` 뒤에 오도록 숫자를 숫자로 비교한다."""
    base = Path(str(name or "").replace("\\", "/")).name.casefold()
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", base)
    ]


def _natural_path_key(path: str):
    """폴더가 섞인 상대경로도 폴더 → 파일 순으로 자연스럽게 정렬한다."""
    parts = [part for part in str(path or "").replace("\\", "/").split("/") if part]
    return [_natural_key(part) for part in parts]


def _open_pdf(upload, raw_name: str):
    """올라온 PDF를 열어 (문서, 쪽수)를 돌려준다."""
    if pdfium is None:
        raise ValueError(
            "서버에 PDF 변환 기능(pypdfium2)이 준비되지 않아 PDF를 올릴 수 없습니다. "
            "관리자에게 문의해 주세요."
        )
    upload.stream.seek(0, os.SEEK_END)
    size = upload.stream.tell()
    upload.stream.seek(0)
    if size > MAX_PDF_BYTES:
        raise ValueError(
            f"‘{raw_name}’ PDF가 {MAX_PDF_BYTES // (1024 * 1024)}MB를 초과합니다."
        )
    try:
        document = pdfium.PdfDocument(upload.stream)
        count = len(document)
    except Exception as exc:
        raise ValueError(
            f"‘{raw_name}’ PDF를 열지 못했습니다. "
            "암호가 걸려 있거나 손상된 파일인지 확인해 주세요."
        ) from exc
    if count <= 0:
        raise ValueError(f"‘{raw_name}’ PDF에 페이지가 없습니다.")
    return document, count


class _PdfPageSource:
    """PDF 한 쪽. 저장하는 순간에만 이미지로 바꿔 메모리를 아낀다."""

    __slots__ = ("document", "index", "filename")

    def __init__(self, document, index: int, filename: str):
        self.document = document
        self.index = index
        self.filename = filename

    def render(self):
        """(JPEG 바이트, 가로, 세로)를 돌려준다."""
        page = self.document.get_page(self.index)
        try:
            width_pt, height_pt = page.get_size()
            scale = PDF_RENDER_DPI / 72
            longest = max(float(width_pt or 0), float(height_pt or 0)) * scale
            if longest > PDF_MAX_SIDE:
                scale *= PDF_MAX_SIDE / longest
            image = page.render(scale=max(scale, 0.1)).to_pil()
            width, height = image.size
            buffer = BytesIO()
            image.convert("RGB").save(
                buffer, "JPEG", quality=PDF_JPEG_QUALITY, optimize=True
            )
        finally:
            page.close()
        return buffer.getvalue(), int(width), int(height)


def _release_sources(sources) -> None:
    """PDF 문서 핸들을 닫아 잡아 두었던 메모리를 곧바로 돌려준다."""
    documents = {}
    for source in sources or ():
        if isinstance(source, _PdfPageSource):
            documents.setdefault(id(source.document), source.document)
    for document in documents.values():
        try:
            document.close()
        except Exception:
            pass


def _expand_sources(uploads, remaining: int):
    """사진·PDF 업로드를 전자책 페이지 원본 목록으로 펼친다.

    PDF는 쪽수만큼 장수가 늘어나므로 장수 제한은 펼친 뒤에 따진다.
    """
    sources = []
    try:
        for upload in uploads:
            raw_name = original_filename(upload.filename, "photo")
            if Path(upload.filename).suffix.lower() in PDF_EXTENSIONS:
                document, count = _open_pdf(upload, raw_name)
                stem = Path(raw_name).stem or "pdf"
                sources.extend(
                    _PdfPageSource(document, index, f"{stem}_{index + 1:03d}.jpg")
                    for index in range(count)
                )
            else:
                sources.append(upload)
            if len(sources) > remaining:
                raise ValueError(
                    f"한 전자책에는 최대 {MAX_PAGES}장까지 담을 수 있습니다. "
                    f"지금은 {remaining}장까지 더 올릴 수 있습니다. "
                    "(PDF는 쪽수만큼 장수가 늘어납니다.)"
                )
    except Exception:
        _release_sources(sources)
        raise
    return sources


def _sorted_uploads(files, remaining: int = MAX_PAGES):
    uploads = [upload for upload in files if upload and upload.filename]
    if not uploads:
        raise ValueError("사진이나 PDF 파일을 하나 이상 선택해 주세요.")
    for upload in uploads:
        if Path(upload.filename).suffix.lower() not in PAGE_EXTENSIONS:
            raise ValueError(UPLOAD_FORMAT_MESSAGE)
    ordered = sorted(uploads, key=lambda upload: _natural_key(upload.filename))
    return _expand_sources(ordered, remaining)


def _parse_folder_batch(raw, uploads):
    """폴더 드롭으로 넘어온 묶음을 `[(제목, [페이지 원본…])]`로 정리한다.

    브라우저가 `folder_batch`(폴더별 제목·상대경로 JSON)와 파일을 같은 순서로
    보내오므로, 그 개수만큼 잘라 폴더별로 나눈 뒤 경로 기준으로 다시 정렬한다.
    폴더 안에 PDF가 있으면 쪽마다 한 장씩으로 펼쳐 둔다.
    """
    try:
        groups = json.loads(str(raw or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("폴더 정보를 읽지 못했습니다. 폴더를 다시 끌어다 놓아 주세요.") from exc
    if not isinstance(groups, list) or not groups:
        raise ValueError("사진이 들어 있는 폴더를 한 개 이상 끌어다 놓아 주세요.")
    if len(groups) > MAX_BATCH_BOOKS:
        raise ValueError(
            f"한 번에 최대 {MAX_BATCH_BOOKS}개 폴더까지 등록할 수 있습니다. "
            f"지금은 {len(groups)}개입니다."
        )

    images = [upload for upload in uploads if upload and upload.filename]
    parsed = []
    cursor = 0
    try:
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError("폴더 정보가 올바르지 않습니다. 다시 시도해 주세요.")
            title = _clean_text(group.get("title"), 120) or "제목 없는 폴더"
            paths = group.get("paths")
            if not isinstance(paths, list) or not paths:
                raise ValueError(f"‘{title}’ 폴더에 올릴 파일이 없습니다.")
            if len(paths) > MAX_PAGES:
                raise ValueError(
                    f"‘{title}’ 폴더에 파일이 {len(paths)}개 있습니다. "
                    f"한 전자책에는 최대 {MAX_PAGES}장까지 담을 수 있습니다."
                )
            chunk = images[cursor:cursor + len(paths)]
            cursor += len(paths)
            if len(chunk) != len(paths):
                raise ValueError("파일 개수가 폴더 정보와 맞지 않습니다. 다시 끌어다 놓아 주세요.")
            for upload in chunk:
                if Path(upload.filename).suffix.lower() not in PAGE_EXTENSIONS:
                    raise ValueError(UPLOAD_FORMAT_MESSAGE)
            ordered = sorted(
                zip(chunk, [str(path or "") for path in paths]),
                key=lambda pair: _natural_path_key(pair[1]),
            )
            try:
                sources = _expand_sources(
                    [upload for upload, _path in ordered], MAX_PAGES
                )
            except ValueError as exc:
                raise ValueError(f"‘{title}’ 폴더 : {exc}") from exc
            parsed.append((title, sources))
        if cursor != len(images):
            raise ValueError("파일 개수가 폴더 정보와 맞지 않습니다. 다시 끌어다 놓아 주세요.")
    except Exception:
        for _title, sources in parsed:
            _release_sources(sources)
        raise
    return parsed


def _inspect_image(upload, max_bytes: int):
    """용량·형식을 검사하고 (원본이름, 가로, 세로)를 돌려준다."""
    raw_name = original_filename(upload.filename, "photo")
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


def _store_pdf_page(source: _PdfPageSource, folder: Path):
    """PDF 한 쪽을 이미지로 만들어 사진과 같은 자리에 저장한다."""
    data, width, height = source.render()
    stored_name = encrypted_storage_name(source.filename)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored_name
    encrypt_bytes(data, path)
    return source.filename, str(path), width, height


def _store_page(source, folder: Path, max_bytes: int = MAX_IMAGE_BYTES):
    """사진 업로드든 PDF 한 쪽이든 같은 방식으로 한 장을 저장한다."""
    if isinstance(source, _PdfPageSource):
        return _store_pdf_page(source, folder)
    return _store_image(source, folder, max_bytes)


def _store_cover(upload):
    """표지 파일을 저장한다. PDF를 고르면 첫 쪽이 표지가 된다."""
    if Path(upload.filename).suffix.lower() in PDF_EXTENSIONS:
        raw_name = original_filename(upload.filename, "cover")
        document, _count = _open_pdf(upload, raw_name)
        try:
            stem = Path(raw_name).stem or "cover"
            return _store_pdf_page(
                _PdfPageSource(document, 0, f"{stem}.jpg"), COVER_ROOT
            )
        finally:
            document.close()
    return _store_image(upload, COVER_ROOT, MAX_COVER_BYTES)


def _book_dir(photobook_id: int) -> Path:
    return PAGE_ROOT / str(photobook_id)


def _pages(conn, photobook_id: int):
    return conn.execute(
        """SELECT id, page_no, caption, image_filename, width, height
           FROM photobook_pages WHERE photobook_id=? ORDER BY page_no, id""",
        (photobook_id,),
    ).fetchall()


def _page_count(conn, photobook_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM photobook_pages WHERE photobook_id=?",
        (photobook_id,),
    ).fetchone()
    return int(row["total"] or 0)


def _renumber_pages(conn, photobook_id: int) -> None:
    """삭제·순서 변경 뒤 1번부터 빈틈없이 다시 매긴다."""
    rows = conn.execute(
        "SELECT id FROM photobook_pages WHERE photobook_id=? ORDER BY page_no, id",
        (photobook_id,),
    ).fetchall()
    # UNIQUE(photobook_id, page_no) 충돌을 피하려고 음수로 잠시 옮긴 뒤 되돌린다.
    for index, row in enumerate(rows, 1):
        conn.execute(
            "UPDATE photobook_pages SET page_no=? WHERE id=?", (-index, row["id"])
        )
    for index, row in enumerate(rows, 1):
        conn.execute(
            "UPDATE photobook_pages SET page_no=? WHERE id=?", (index, row["id"])
        )


def _sync_cover(conn, photobook_id: int) -> None:
    """표지를 따로 올리지 않았으면 첫 장을 표지로 쓴다."""
    book = conn.execute(
        "SELECT cover_path FROM photobooks WHERE id=?", (photobook_id,)
    ).fetchone()
    if book and str(book["cover_path"] or "").strip():
        if os.path.isfile(book["cover_path"]):
            return
    first = conn.execute(
        """SELECT image_filename, image_path FROM photobook_pages
           WHERE photobook_id=? ORDER BY page_no, id LIMIT 1""",
        (photobook_id,),
    ).fetchone()
    conn.execute(
        "UPDATE photobooks SET cover_filename=?, cover_path=? WHERE id=?",
        (
            first["image_filename"] if first else "",
            first["image_path"] if first else "",
            photobook_id,
        ),
    )


def _remove_file(path) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _page_image_paths(conn, photobook_id: int):
    return [
        str(row["image_path"] or "")
        for row in conn.execute(
            "SELECT image_path FROM photobook_pages WHERE photobook_id=?",
            (photobook_id,),
        ).fetchall()
    ]


def _purge_book_files(photobook_id: int, cover_path, image_paths) -> None:
    """전자책의 사진 폴더·페이지 이미지·전용 표지 파일을 디스크에서 모두 지운다."""
    directory = _book_dir(photobook_id)
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
    # 폴더 밖에 저장된 이미지가 있어도 남지 않도록 경로별로 한 번 더 확인한다.
    for path in image_paths:
        _remove_file(path)
    # 페이지 사진을 그대로 표지로 쓰기도 하므로 따로 올린 표지 파일만 지운다.
    if str(cover_path or "").startswith(str(COVER_ROOT)):
        _remove_file(cover_path)


def _natural_title_key(title: str):
    """‘10화’가 ‘9화’ 뒤에 오도록 제목 안의 숫자를 숫자로 비교한다."""
    text = str(title or "").strip().casefold()
    return [
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in re.split(r"(\d+)", text)
    ]


def _sort_rows(rows, sort: str):
    """이름순만 파이썬에서 자연 정렬한다(SQL은 ‘10화 < 1화’로 뒤집힌다)."""
    if sort not in ("title", "title_desc"):
        return rows
    return sorted(
        rows,
        key=lambda row: _natural_title_key(row["title"]),
        reverse=(sort == "title_desc"),
    )


def _sort_key(value) -> str:
    key = str(value or "").strip()
    return key if key in SORT_ORDERS else DEFAULT_SORT


def _library_filter(query: str, theme: str):
    """서재 검색·주제 조건을 WHERE 절과 파라미터로 만든다."""
    where, params = [], []
    if query:
        where.append("(b.title LIKE ? OR b.author LIKE ? OR b.description LIKE ?)")
        params.extend((f"%{query}%", f"%{query}%", f"%{query}%"))
    if theme:
        where.append("b.theme = ?")
        params.append(theme)
    return (f"WHERE {' AND '.join(where)}" if where else ""), params


def _browse_args(query: str = "", theme: str = "", sort: str = DEFAULT_SORT) -> dict:
    """서재에서 보던 검색·정렬 조건을 링크에 실어 뷰어까지 이어 준다."""
    args = {}
    if query:
        args["q"] = query
    if theme:
        args["theme"] = theme
    if sort and sort != DEFAULT_SORT:
        args["sort"] = sort
    return args


def _neighbour_books(conn, photobook_id: int, query: str, theme: str, sort: str):
    """서재에서 보던 순서 그대로 앞뒤 전자책을 찾아 준다."""
    def _ordered(clause, params):
        return _sort_rows(
            conn.execute(
                f"SELECT b.id, b.title FROM photobooks b {clause} "
                f"ORDER BY {SORT_ORDERS[sort]}",
                params,
            ).fetchall(),
            sort,
        )

    clause, params = _library_filter(query, theme)
    rows = _ordered(clause, params)
    ids = [int(row["id"]) for row in rows]
    if photobook_id not in ids:
        # 검색 결과 밖의 전자책을 주소로 바로 열었다면 전체 목록을 기준으로 삼는다.
        rows = _ordered("", [])
        ids = [int(row["id"]) for row in rows]
        if photobook_id not in ids:
            return None, None
    position = ids.index(photobook_id)
    return (
        rows[position - 1] if position > 0 else None,
        rows[position + 1] if position + 1 < len(rows) else None,
    )


def _form_context(book=None, form=None):
    return {"book": book, "form": form or {}, "themes": THEME_CHOICES}


# ---------------------------------------------------------------------------
# 전자책 서재
# ---------------------------------------------------------------------------


@photobook_bp.route("/")
def library():
    _require_staff()
    query = _clean_text(request.args.get("q"), 100)
    theme = _clean_text(request.args.get("theme"), 40)
    sort = _sort_key(request.args.get("sort"))
    clause, params = _library_filter(query, theme)

    conn = get_db()
    try:
        books = conn.execute(
            f"""SELECT b.*, COUNT(p.id) AS page_count
                FROM photobooks b
                LEFT JOIN photobook_pages p ON p.photobook_id = b.id
                {clause}
                GROUP BY b.id
                ORDER BY {SORT_ORDERS[sort]}""",
            params,
        ).fetchall()
    finally:
        conn.close()

    books = _sort_rows(books, sort)
    can_manage_map = {book["id"]: _can_manage(book) for book in books}
    return render_template(
        "photobook/library.html",
        books=books,
        query=query,
        theme=theme,
        themes=THEME_CHOICES,
        sort=sort,
        sorts=SORT_CHOICES,
        browse_args=_browse_args(query, theme, sort),
        locked_map={book["id"]: _is_locked(book) for book in books},
        can_manage_map=can_manage_map,
        can_bulk_delete=any(can_manage_map.values()),
    )


@photobook_bp.route("/new", methods=["GET", "POST"])
def create_book():
    _require_staff()
    if request.method == "GET":
        return render_template("photobook/form.html", **_form_context())

    form = {
        "title": _clean_text(request.form.get("title"), 120),
        "author": _clean_text(request.form.get("author"), 60),
        "theme": _clean_text(request.form.get("theme"), 40),
        "description": _clean_text(request.form.get("description"), 2000),
    }
    password = str(request.form.get("password") or "").strip()
    cover = request.files.get("cover")

    if not form["title"]:
        flash("전자책 제목을 입력해 주세요.", "error")
        return render_template("photobook/form.html", **_form_context(form=form)), 400
    if password and len(password) < 2:
        flash("열람 비밀번호는 2자 이상으로 정해 주세요.", "error")
        return render_template("photobook/form.html", **_form_context(form=form)), 400
    if cover and cover.filename and Path(cover.filename).suffix.lower() not in PAGE_EXTENSIONS:
        flash(COVER_FORMAT_MESSAGE, "error")
        return render_template("photobook/form.html", **_form_context(form=form)), 400

    try:
        images = _sorted_uploads(request.files.getlist("photos"))
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template("photobook/form.html", **_form_context(form=form)), 400

    cover_name, cover_path = "", ""
    if cover and cover.filename:
        try:
            cover_name, cover_path, _w, _h = _store_cover(cover)
        except ValueError as exc:
            _release_sources(images)
            flash(str(exc), "error")
            return render_template("photobook/form.html", **_form_context(form=form)), 400

    conn = get_db()
    page_dir = None
    try:
        cursor = conn.execute(
            """INSERT INTO photobooks
               (title, author, theme, description, cover_filename, cover_path,
                password_hash, created_by, created_by_emp_no)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                form["title"], form["author"], form["theme"], form["description"],
                cover_name, cover_path,
                hash_password(password) if password else None,
                _current_name(), str(session.get("emp_no") or ""),
            ),
        )
        photobook_id = cursor.lastrowid
        page_dir = _book_dir(photobook_id)
        rows = []
        for page_no, source in enumerate(images, 1):
            name, path, width, height = _store_page(source, page_dir)
            rows.append((photobook_id, page_no, "", name, path, width, height))
        conn.executemany(
            """INSERT INTO photobook_pages
               (photobook_id, page_no, caption, image_filename, image_path, width, height)
               VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
        _sync_cover(conn, photobook_id)
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        if page_dir and page_dir.is_dir():
            shutil.rmtree(page_dir, ignore_errors=True)
        flash(str(exc), "error")
        return render_template("photobook/form.html", **_form_context(form=form)), 400
    except Exception:
        conn.rollback()
        if page_dir and page_dir.is_dir():
            shutil.rmtree(page_dir, ignore_errors=True)
        raise
    finally:
        conn.close()
        _release_sources(images)

    if password:
        _mark_unlocked(photobook_id)
    flash(f"‘{form['title']}’ 전자책을 {len(images)}장으로 만들었습니다.", "success")
    return redirect(url_for("photobook.read_book", photobook_id=photobook_id))


@photobook_bp.route("/new/batch", methods=["POST"])
def create_books_batch():
    """폴더 여러 개를 한꺼번에 끌어다 놓았을 때 폴더마다 전자책 한 권씩 만든다.

    제목은 폴더 이름을 그대로 쓰고, 폴더 안에서 이름순으로 가장 앞선 사진이
    1페이지이자 표지가 된다(표지는 `_sync_cover`가 자동으로 맞춘다).
    촬영자·주제·소개·열람 비밀번호는 만들어지는 모든 권에 똑같이 적용한다.
    """
    _require_staff()

    form = {
        "title": _clean_text(request.form.get("title"), 120),
        "author": _clean_text(request.form.get("author"), 60),
        "theme": _clean_text(request.form.get("theme"), 40),
        "description": _clean_text(request.form.get("description"), 2000),
    }
    password = str(request.form.get("password") or "").strip()

    def _fail(message: str):
        flash(message, "error")
        return render_template("photobook/form.html", **_form_context(form=form)), 400

    if password and len(password) < 2:
        return _fail("열람 비밀번호는 2자 이상으로 정해 주세요.")

    try:
        groups = _parse_folder_batch(
            request.form.get("folder_batch"), request.files.getlist("photos")
        )
    except ValueError as exc:
        return _fail(str(exc))

    password_hash = hash_password(password) if password else None
    author = _current_name()
    emp_no = str(session.get("emp_no") or "")

    conn = get_db()
    created = []
    page_dirs = []
    try:
        for title, images in groups:
            cursor = conn.execute(
                """INSERT INTO photobooks
                   (title, author, theme, description, cover_filename, cover_path,
                    password_hash, created_by, created_by_emp_no)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    title, form["author"], form["theme"], form["description"],
                    "", "", password_hash, author, emp_no,
                ),
            )
            photobook_id = cursor.lastrowid
            page_dir = _book_dir(photobook_id)
            page_dirs.append(page_dir)
            rows = []
            for page_no, source in enumerate(images, 1):
                name, path, width, height = _store_page(source, page_dir)
                rows.append((photobook_id, page_no, "", name, path, width, height))
            conn.executemany(
                """INSERT INTO photobook_pages
                   (photobook_id, page_no, caption, image_filename, image_path, width, height)
                   VALUES (?,?,?,?,?,?,?)""",
                rows,
            )
            _sync_cover(conn, photobook_id)
            created.append((photobook_id, title, len(images)))
        conn.commit()
    except ValueError as exc:
        conn.rollback()
        for directory in page_dirs:
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
        return _fail(str(exc))
    except Exception:
        conn.rollback()
        for directory in page_dirs:
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        conn.close()
        for _title, sources in groups:
            _release_sources(sources)

    if password_hash:
        for photobook_id, _title, _count in created:
            _mark_unlocked(photobook_id)

    total = sum(count for _id, _title, count in created)
    flash(
        f"폴더 {len(created)}개를 전자책 {len(created)}권(총 {total}장)으로 등록했습니다. "
        f"각 폴더의 첫 장이 표지가 되었습니다.",
        "success",
    )
    if len(created) == 1:
        return redirect(url_for("photobook.read_book", photobook_id=created[0][0]))
    return redirect(url_for("photobook.library"))


@photobook_bp.route("/<int:photobook_id>/edit", methods=["GET", "POST"])
def edit_book(photobook_id):
    _require_staff()
    conn = get_db()
    old_cover = None
    try:
        book = _get_book(conn, photobook_id)
        if not _can_manage(book):
            abort(403)
        if request.method == "GET":
            return render_template("photobook/form.html", **_form_context(book=book))

        form = {
            "title": _clean_text(request.form.get("title"), 120),
            "author": _clean_text(request.form.get("author"), 60),
            "theme": _clean_text(request.form.get("theme"), 40),
            "description": _clean_text(request.form.get("description"), 2000),
        }
        if not form["title"]:
            flash("전자책 제목을 입력해 주세요.", "error")
            return render_template(
                "photobook/form.html", **_form_context(book=book, form=form)
            ), 400

        cover_name, cover_path = book["cover_filename"], book["cover_path"]
        cover = request.files.get("cover")
        if cover and cover.filename:
            if Path(cover.filename).suffix.lower() not in PAGE_EXTENSIONS:
                flash(COVER_FORMAT_MESSAGE, "error")
                return render_template(
                    "photobook/form.html", **_form_context(book=book, form=form)
                ), 400
            try:
                cover_name, cover_path, _w, _h = _store_cover(cover)
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template(
                    "photobook/form.html", **_form_context(book=book, form=form)
                ), 400
            # 표지로 쓰던 파일이 전자책 페이지라면 지우면 안 된다.
            if str(book["cover_path"] or "").startswith(str(COVER_ROOT)):
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
                    "photobook/form.html", **_form_context(book=book, form=form)
                ), 400
            password_hash = hash_password(password)

        conn.execute(
            """UPDATE photobooks
               SET title=?, author=?, theme=?, description=?,
                   cover_filename=?, cover_path=?, password_hash=?,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (
                form["title"], form["author"], form["theme"], form["description"],
                cover_name, cover_path, password_hash, photobook_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if old_cover and os.path.isfile(old_cover):
        try:
            os.remove(old_cover)
        except OSError:
            pass
    _mark_unlocked(photobook_id)
    flash("전자책 정보를 수정했습니다.", "success")
    return redirect(url_for("photobook.manage_pages", photobook_id=photobook_id))


@photobook_bp.route("/<int:photobook_id>/delete", methods=["POST"])
def delete_book(photobook_id):
    _require_staff()
    conn = get_db()
    cover_path = ""
    image_paths = []
    try:
        book = _get_book(conn, photobook_id)
        if not _can_manage(book):
            abort(403)
        cover_path = str(book["cover_path"] or "")
        image_paths = _page_image_paths(conn, photobook_id)
        conn.execute("DELETE FROM photobook_pages WHERE photobook_id=?", (photobook_id,))
        conn.execute("DELETE FROM photobooks WHERE id=?", (photobook_id,))
        conn.commit()
    finally:
        conn.close()

    _purge_book_files(photobook_id, cover_path, image_paths)
    flash("전자책을 삭제했습니다.", "success")
    return redirect(url_for("photobook.library"))


@photobook_bp.route("/delete-selected", methods=["POST"])
def delete_books():
    """서재에서 체크한 전자책을 한 번에 지운다.

    사진 파일과 따로 올린 표지 파일까지 디스크에서 함께 지우고, 등록자 본인
    또는 관리자가 아닌 전자책은 건너뛴 뒤 몇 권을 못 지웠는지 알려 준다.
    """
    _require_staff()
    ids = sorted({
        int(value) for value in request.form.getlist("photobook_ids")
        if str(value).strip().isdigit()
    })
    if not ids:
        flash("삭제할 전자책을 한 권 이상 선택해 주세요.", "error")
        return redirect(url_for("photobook.library"))

    conn = get_db()
    targets = []          # (id, 표지경로, [페이지 이미지 경로…])
    blocked = []
    try:
        for photobook_id in ids:
            book = conn.execute(
                "SELECT * FROM photobooks WHERE id=?", (photobook_id,)
            ).fetchone()
            if not book:
                continue
            if not _can_manage(book):
                blocked.append(str(book["title"] or ""))
                continue
            targets.append((
                photobook_id,
                str(book["cover_path"] or ""),
                _page_image_paths(conn, photobook_id),
            ))
            conn.execute(
                "DELETE FROM photobook_pages WHERE photobook_id=?", (photobook_id,)
            )
            conn.execute("DELETE FROM photobooks WHERE id=?", (photobook_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # DB에서 지운 뒤에 파일을 지워야 중간에 실패해도 고아 레코드가 남지 않는다.
    for photobook_id, cover_path, image_paths in targets:
        _purge_book_files(photobook_id, cover_path, image_paths)

    if targets:
        photos = sum(len(paths) for _id, _cover, paths in targets)
        flash(
            f"전자책 {len(targets)}권(사진 {photos}장)을 사진·표지 파일까지 삭제했습니다.",
            "success",
        )
    if blocked:
        flash(
            f"등록자 본인이나 관리자만 지울 수 있어 {len(blocked)}권은 건너뛰었습니다 : "
            f"{', '.join(blocked[:5])}{' 외' if len(blocked) > 5 else ''}",
            "error",
        )
    return redirect(url_for("photobook.library"))


@photobook_bp.route("/<int:photobook_id>/unlock", methods=["GET", "POST"])
def unlock(photobook_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_book(conn, photobook_id)
    finally:
        conn.close()

    if not _is_locked(book):
        return redirect(url_for("photobook.read_book", photobook_id=photobook_id))
    if request.method == "GET":
        return render_template("photobook/unlock.html", book=book)

    supplied = str(request.form.get("password") or "").strip()
    if not verify_password(book["password_hash"], supplied):
        flash("비밀번호가 올바르지 않습니다.", "error")
        return render_template("photobook/unlock.html", book=book), 403
    _mark_unlocked(photobook_id)
    return redirect(url_for("photobook.read_book", photobook_id=photobook_id))


# ---------------------------------------------------------------------------
# 사진 관리
# ---------------------------------------------------------------------------


@photobook_bp.route("/<int:photobook_id>/pages", methods=["GET", "POST"])
def manage_pages(photobook_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_book(conn, photobook_id)
        if not _can_manage(book):
            abort(403)

        if request.method == "POST":
            remaining = MAX_PAGES - _page_count(conn, photobook_id)
            if remaining <= 0:
                flash(f"한 전자책에는 최대 {MAX_PAGES}장까지 담을 수 있습니다.", "error")
                return redirect(url_for("photobook.manage_pages", photobook_id=photobook_id))
            images = []
            try:
                images = _sorted_uploads(request.files.getlist("photos"), remaining)
                next_no = _page_count(conn, photobook_id) + 1
                rows = []
                for offset, source in enumerate(images):
                    name, path, width, height = _store_page(
                        source, _book_dir(photobook_id)
                    )
                    rows.append(
                        (photobook_id, next_no + offset, "", name, path, width, height)
                    )
                conn.executemany(
                    """INSERT INTO photobook_pages
                       (photobook_id, page_no, caption, image_filename, image_path, width, height)
                       VALUES (?,?,?,?,?,?,?)""",
                    rows,
                )
                _renumber_pages(conn, photobook_id)
                _sync_cover(conn, photobook_id)
                conn.execute(
                    "UPDATE photobooks SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (photobook_id,),
                )
                conn.commit()
            except ValueError as exc:
                conn.rollback()
                flash(str(exc), "error")
                return redirect(url_for("photobook.manage_pages", photobook_id=photobook_id))
            finally:
                _release_sources(images)
            flash(f"{len(images)}장을 추가했습니다.", "success")
            return redirect(url_for("photobook.manage_pages", photobook_id=photobook_id))

        pages = _pages(conn, photobook_id)
    finally:
        conn.close()

    return render_template(
        "photobook/pages.html", book=book, pages=pages, max_pages=MAX_PAGES
    )


@photobook_bp.route("/<int:photobook_id>/pages/<int:page_id>/delete", methods=["POST"])
def delete_page(photobook_id, page_id):
    _require_staff()
    conn = get_db()
    image_path = None
    try:
        book = _get_book(conn, photobook_id)
        if not _can_manage(book):
            abort(403)
        row = conn.execute(
            "SELECT image_path FROM photobook_pages WHERE id=? AND photobook_id=?",
            (page_id, photobook_id),
        ).fetchone()
        if not row:
            abort(404)
        image_path = row["image_path"]
        if str(book["cover_path"] or "") == image_path:
            # 표지로 쓰이던 장을 지우면 표지를 다시 고르게 비워 둔다.
            conn.execute(
                "UPDATE photobooks SET cover_filename='', cover_path='' WHERE id=?",
                (photobook_id,),
            )
        conn.execute(
            "DELETE FROM photobook_pages WHERE id=? AND photobook_id=?",
            (page_id, photobook_id),
        )
        _renumber_pages(conn, photobook_id)
        _sync_cover(conn, photobook_id)
        conn.execute(
            "UPDATE photobooks SET updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (photobook_id,),
        )
        conn.commit()
    finally:
        conn.close()

    if image_path and os.path.isfile(image_path):
        try:
            os.remove(image_path)
        except OSError:
            pass
    flash("사진을 삭제했습니다.", "success")
    return redirect(url_for("photobook.manage_pages", photobook_id=photobook_id))


@photobook_bp.route("/<int:photobook_id>/pages/<int:page_id>/move", methods=["POST"])
def move_page(photobook_id, page_id):
    _require_staff()
    direction = -1 if str(request.form.get("direction") or "") == "up" else 1
    conn = get_db()
    try:
        book = _get_book(conn, photobook_id)
        if not _can_manage(book):
            abort(403)
        current = conn.execute(
            "SELECT id, page_no FROM photobook_pages WHERE id=? AND photobook_id=?",
            (page_id, photobook_id),
        ).fetchone()
        if not current:
            abort(404)
        neighbour = conn.execute(
            """SELECT id, page_no FROM photobook_pages
               WHERE photobook_id=? AND page_no = ?""",
            (photobook_id, int(current["page_no"]) + direction),
        ).fetchone()
        if neighbour:
            # UNIQUE 제약을 피해 임시 번호를 거쳐 자리를 맞바꾼다.
            conn.execute("UPDATE photobook_pages SET page_no=-1 WHERE id=?", (current["id"],))
            conn.execute(
                "UPDATE photobook_pages SET page_no=? WHERE id=?",
                (current["page_no"], neighbour["id"]),
            )
            conn.execute(
                "UPDATE photobook_pages SET page_no=? WHERE id=?",
                (neighbour["page_no"], current["id"]),
            )
            _sync_cover(conn, photobook_id)
            conn.commit()
    finally:
        conn.close()
    return redirect(url_for("photobook.manage_pages", photobook_id=photobook_id))


# ---------------------------------------------------------------------------
# 뷰어 · 이미지 제공
# ---------------------------------------------------------------------------


@photobook_bp.route("/<int:photobook_id>")
def read_book(photobook_id):
    _require_staff()
    query = _clean_text(request.args.get("q"), 100)
    theme = _clean_text(request.args.get("theme"), 40)
    sort = _sort_key(request.args.get("sort"))

    conn = get_db()
    try:
        book = _get_book(conn, photobook_id)
        locked = _require_unlocked(book)
        if locked:
            return locked
        pages = _pages(conn, photobook_id)
        # 서재에서 보던 순서 그대로 앞뒤 권을 이어 볼 수 있게 한다.
        prev_book, next_book = _neighbour_books(conn, photobook_id, query, theme, sort)
        conn.execute(
            "UPDATE photobooks SET view_count = view_count + 1 WHERE id=?",
            (photobook_id,),
        )
        conn.commit()
    finally:
        conn.close()

    return render_template(
        "photobook/viewer.html",
        book=book,
        pages=pages,
        can_manage=_can_manage(book),
        prev_book=prev_book,
        next_book=next_book,
        browse_args=_browse_args(query, theme, sort),
    )


@photobook_bp.route("/<int:photobook_id>/cover")
def serve_cover(photobook_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_book(conn, photobook_id)
        cover_path = str(book["cover_path"] or "")
        cover_name = book["cover_filename"] or "cover.jpg"
        if not cover_path or not os.path.isfile(cover_path):
            first = conn.execute(
                """SELECT image_filename, image_path FROM photobook_pages
                   WHERE photobook_id=? ORDER BY page_no, id LIMIT 1""",
                (photobook_id,),
            ).fetchone()
            cover_path = str(first["image_path"]) if first else ""
            cover_name = (first["image_filename"] if first else "") or "cover.jpg"
    finally:
        conn.close()

    if not cover_path or not os.path.isfile(cover_path):
        abort(404)
    return encrypted_response(cover_path, cover_name, as_attachment=False)


@photobook_bp.route("/<int:photobook_id>/pages/<int:page_id>/image")
def serve_page_image(photobook_id, page_id):
    _require_staff()
    conn = get_db()
    try:
        book = _get_book(conn, photobook_id)
        if _is_locked(book):
            abort(403)
        row = conn.execute(
            """SELECT image_path, image_filename FROM photobook_pages
               WHERE id=? AND photobook_id=?""",
            (page_id, photobook_id),
        ).fetchone()
    finally:
        conn.close()

    if not row or not row["image_path"] or not os.path.isfile(row["image_path"]):
        abort(404)
    return encrypted_response(
        row["image_path"], row["image_filename"] or "photo.jpg", as_attachment=False
    )
