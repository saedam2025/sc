"""개인화이트보드 회원별 30MB 용량 제한 회귀 검사."""

import io
import sqlite3
import sys
import tempfile
from pathlib import Path

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routes import memo as memo_routes


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main():
    with tempfile.TemporaryDirectory(prefix="saedam-memo-quota-") as directory:
        root = Path(directory)
        db_path = root / "memo.db"
        upload_dir = root / "uploads"
        upload_dir.mkdir()

        app = Flask(__name__)
        app.secret_key = "memo-quota-test"
        app.register_blueprint(memo_routes.memo_bp, url_prefix="/memo")

        original_get_db = memo_routes.get_db
        original_upload_dir = memo_routes._upload_dir
        original_storage_root = memo_routes._storage_root
        original_encrypt_upload = memo_routes.encrypt_upload
        original_plaintext_size = memo_routes.plaintext_size

        memo_routes.get_db = lambda: connect(db_path)
        memo_routes._upload_dir = lambda: upload_dir
        memo_routes._storage_root = lambda: root

        def fake_encrypt(uploaded, destination):
            data = uploaded.stream.read()
            Path(destination).write_bytes(data)
            return len(data)

        memo_routes.encrypt_upload = fake_encrypt
        memo_routes.plaintext_size = lambda path: Path(path).stat().st_size

        try:
            client = app.test_client()
            with client.session_transaction() as flask_session:
                flask_session["emp_no"] = "emp-1"
                flask_session["user_name"] = "회원1"

            allowed = client.post(
                "/memo/upload_file",
                data={"file": (io.BytesIO(b"a" * 1024), "allowed.txt")},
                content_type="multipart/form-data",
            )
            assert allowed.status_code == 200, allowed.get_data(as_text=True)

            conn = connect(db_path)
            memo_routes.ensure_memo_schema(conn)
            conn.execute(
                "INSERT INTO memos (owner_key, type, content) VALUES (?, 'postit', ?)",
                ("other-user", "x" * memo_routes.MEMO_USER_QUOTA_BYTES),
            )
            conn.execute(
                "INSERT INTO memos (owner_key, type, content) VALUES (?, 'postit', ?)",
                ("emp-1", "x" * (memo_routes.MEMO_USER_QUOTA_BYTES - 100 * 1024)),
            )
            conn.commit()
            conn.close()

            blocked = client.post(
                "/memo/upload_file",
                data={"file": (io.BytesIO(b"b" * 200 * 1024), "blocked.txt")},
                content_type="multipart/form-data",
            )
            assert blocked.status_code == 413, blocked.get_data(as_text=True)
            assert "30MB" in blocked.get_json()["message"]

            assert memo_routes._format_megabytes(int(6.2 * 1024 * 1024)) == "6.2"

            template = (
                Path(__file__).resolve().parents[1] / "templates" / "memo.html"
            ).read_text(encoding="utf-8")
            assert "사용가능용량 {{ memo_quota_mb }}MB 중 {{ memo_usage_mb }}MB사용" in template
            assert ".toolbar-subtitle" in template
            assert "font-weight: 400" in template
        finally:
            memo_routes.get_db = original_get_db
            memo_routes._upload_dir = original_upload_dir
            memo_routes._storage_root = original_storage_root
            memo_routes.encrypt_upload = original_encrypt_upload
            memo_routes.plaintext_size = original_plaintext_size

    print("Memo quota test: PASS")


if __name__ == "__main__":
    main()
