"""Regression checks for the restored TXT eBook workflow."""

import io
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module
from routes import ebook


def _connection(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    with tempfile.TemporaryDirectory(prefix="saedam-ebook-test-") as directory:
        database = Path(directory) / "ebook-test.db"
        ebook.get_db = lambda: _connection(database)
        app_module.record_page_usage = lambda *_args, **_kwargs: False
        ebook.init_ebook_schema()

        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["emp_no"] = "admin"
            sess["user_name"] = "admin"
            sess["user_level"] = 0

        response = client.post(
            "/ebook/books/new",
            data={
                "title": "복구 테스트 도서",
                "author": "테스트 저자",
                "description": "TXT eBook 복구 검증",
                "page_length": "500",
                "text_file": (
                    io.BytesIO((("첫 문단입니다. " * 80) + "\n\n둘째 문단입니다.").encode("utf-8")),
                    "restore-test.txt",
                ),
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 302, response.get_data(as_text=True)

        conn = _connection(database)
        book = conn.execute("SELECT * FROM ebooks WHERE kind='text'").fetchone()
        page_count = conn.execute(
            "SELECT COUNT(*) FROM ebook_pages WHERE ebook_id=?", (book["id"],)
        ).fetchone()[0]
        conn.close()
        assert book["source_filename"] == "restore-test.txt"
        assert page_count >= 2

        library = client.get("/ebook/books")
        reader = client.get(f"/ebook/books/{book['id']}")
        editor = client.get(f"/ebook/books/{book['id']}/edit")
        assert library.status_code == 200 and "복구 테스트 도서" in library.get_data(as_text=True)
        assert reader.status_code == 200 and "복구 테스트 도서" in reader.get_data(as_text=True)
        assert editor.status_code == 200 and "eBook 관리" in editor.get_data(as_text=True)

        bookmark = client.post(
            f"/ebook/books/{book['id']}/bookmark",
            json={"slot": 2, "page_no": 2},
        )
        assert bookmark.status_code == 200 and bookmark.get_json()["page_no"] == 2

        review = client.post(
            f"/ebook/books/{book['id']}/reviews",
            data={"content": "복구된 독후감 기능 테스트"},
        )
        assert review.status_code == 302

        print(f"TXT eBook restore test: PASS ({page_count} pages)")


if __name__ == "__main__":
    main()
