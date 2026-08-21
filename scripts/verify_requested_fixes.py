"""요청된 메뉴/다운로드/갤러리/FileLink/로그인 수정의 비파괴 점검."""

from pathlib import Path
from io import BytesIO
import json
import os
import re
import sqlite3
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import app
from routes.database import get_db
from routes.main import UPLOAD_FOLDER, _weblink_file_path
from routes import gall2 as gall2_module
from PIL import Image
from werkzeug.datastructures import FileStorage


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"PASS: {message}")


client = app.test_client()

response = client.post(
    "/login",
    json={"emp_no": "x" * 16, "password": "y" * 16},
)
check(response.status_code == 400, "로그인 15자 초과 입력을 서버에서도 거부")

with client.session_transaction() as login_session:
    login_session["emp_no"] = "admin"
    login_session["user_name"] = "admin"
    login_session["user_level"] = 1
    login_session["position"] = "최고관리자"
    login_session["department"] = "본부"

response = client.get("/expense/template")
check(response.status_code == 200, "지출결의서 엑셀 양식 다운로드")
check(response.data[:2] == b"PK", "다운로드 파일이 유효한 XLSX 컨테이너")
check("attachment" in response.headers.get("Content-Disposition", ""), "엑셀 양식 첨부 헤더")

response = client.get("/gall2?post_id=1")
gallery_html = response.get_data(as_text=True)
check(response.status_code == 200, "사내 갤러리 렌더링")
check("requestedPostId" in gallery_html, "post_id로 갤러리 게시물 자동 열기")
check("게시물 사진 관리" in gallery_html, "갤러리 수정 사진 추가·삭제 UI")
check("사진을 끌어다 놓거나 클릭해서 선택" in gallery_html, "갤러리 수정 사진 드래그앤드롭 업로드 UI")
check("추가 예정 사진" in gallery_html and "edit-selected-preview" in gallery_html, "갤러리 추가 사진 업로드 전 미리보기")
check('id="btnAddPhotos"' not in gallery_html and "저장 버튼을 누르면 함께 업로드됩니다" in gallery_html, "별도 업로드 버튼 없이 저장 시 사진 업로드")
check("formatEditPhotoSize(file.size)" in gallery_html, "저장 전 추가 사진 용량 표시")
check("buildLightweightEditPhotoPreviews" in gallery_html, "대용량 사진 경량 썸네일 생성")
check("editPhotoFiles = editPhotoFiles.concat" in gallery_html, "수정창에서 여러 차례 선택한 사진 누적")
check("/images/add" in gallery_html and "/image/__image_id__/delete" in gallery_html, "갤러리 사진 관리 API 연결")

response = client.get("/")
main_html = response.get_data(as_text=True)
check(response.status_code == 200, "메인 화면 렌더링")
check('href="/manual/"' in main_html, "업무공간의 업무메뉴얼이 새 메뉴로 연결")
check('class="my-weekly-tooltip-item"' in main_html, "주간업무 검은색 풍선 도움말 연결")
holiday_match = re.search(r"const globalHolidays = (\{.*?\});", main_html)
rendered_holidays = json.loads(holiday_match.group(1)) if holiday_match else {}
check(rendered_holidays.get("2026-08-17") == "광복절 대체 휴일", "주간자원관리 공휴일 한국어 명칭")

portable_path = Path(_weblink_file_path(r"C:\\old-server\\uploads\\sample.sdf"))
check(portable_path == Path(UPLOAD_FOLDER) / "sample.sdf", "FileLink 레거시 절대 경로를 현재 저장소로 변환")

with tempfile.TemporaryDirectory(prefix="saedam-gallery-check-") as temporary_root:
    previous_upload_folder = gall2_module.UPLOAD_FOLDER
    previous_thumb_folder = gall2_module.THUMB_FOLDER
    gall2_module.UPLOAD_FOLDER = os.path.join(temporary_root, "uploads")
    gall2_module.THUMB_FOLDER = os.path.join(temporary_root, "thumbnails")
    os.makedirs(gall2_module.UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(gall2_module.THUMB_FOLDER, exist_ok=True)
    try:
        temporary_db = sqlite3.connect(":memory:")
        temporary_db.row_factory = sqlite3.Row
        temporary_db.executescript(
            """
            CREATE TABLE gall2_posts (
                id INTEGER PRIMARY KEY, school_id INTEGER, updated_at TEXT
            );
            CREATE TABLE gall2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT, filename TEXT, thumb_name TEXT,
                file_type TEXT, tab_id INTEGER, post_id INTEGER
            );
            INSERT INTO gall2_posts (id, school_id) VALUES (1, NULL);
            """
        )
        image_stream = BytesIO()
        Image.new("RGB", (40, 30), (30, 120, 210)).save(image_stream, format="PNG")
        image_stream.seek(0)
        upload = FileStorage(stream=image_stream, filename="추가사진.png", content_type="image/png")
        added = gall2_module.append_gallery_files(temporary_db, 1, [upload])
        temporary_db.commit()
        image_row = temporary_db.execute("SELECT * FROM gall2 WHERE post_id=1").fetchone()
        check(added == 1 and image_row is not None, "갤러리 기존 게시물에 사진 추가")
        check(
            os.path.isfile(os.path.join(gall2_module.UPLOAD_FOLDER, image_row["filename"]))
            and os.path.isfile(os.path.join(gall2_module.THUMB_FOLDER, image_row["thumb_name"])),
            "추가 사진과 썸네일 암호화 저장",
        )
        deleted = gall2_module.delete_gallery_image_for_scope(
            temporary_db, 1, image_row["id"], None
        )
        check(deleted and temporary_db.execute("SELECT COUNT(*) FROM gall2").fetchone()[0] == 0, "갤러리 사진 개별 삭제")
        temporary_db.close()
    finally:
        gall2_module.UPLOAD_FOLDER = previous_upload_folder
        gall2_module.THUMB_FOLDER = previous_thumb_folder

with app.app_context():
    conn = get_db()
    stored_link = conn.execute(
        "SELECT id, filepath FROM weblinks WHERE type='file' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    school = conn.execute(
        "SELECT access_key FROM schools WHERE access_key IS NOT NULL AND TRIM(access_key)<>'' LIMIT 1"
    ).fetchone()
    conn.close()

if stored_link and os.path.isfile(_weblink_file_path(stored_link["filepath"])):
    print(f"INFO: FileLink id={stored_link['id']} path={stored_link['filepath']} resolved={_weblink_file_path(stored_link['filepath'])}")
    response = client.get(f"/weblink-file/{stored_link['id']}")
    check(response.status_code == 200, "기존 FileLink 파일 다운로드")
    check("attachment" in response.headers.get("Content-Disposition", ""), "FileLink 다운로드 첨부 헤더")

if school:
    response = client.get(f"/school/{school['access_key']}/gallery?post_id=1")
    school_gallery_html = response.get_data(as_text=True)
    check(response.status_code == 200, "학교 갤러리 렌더링")
    check("requestedPostId" in school_gallery_html, "학교 갤러리 게시물 자동 열기")
    check("/images/add" in school_gallery_html, "학교 갤러리 사진 추가 연결")

print("ALL REQUESTED FIX CHECKS PASSED")
