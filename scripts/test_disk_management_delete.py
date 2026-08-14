"""디스크 관리 삭제 파일이 레거시 마이그레이션으로 복원되지 않는지 검증한다."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from routes import storage


class LegacyBootstrapTest(unittest.TestCase):
    def test_delete_storage_target_removes_file_from_disk(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            target = Path(temporary_directory) / "disk-delete-test.txt"
            target.write_text("delete me", encoding="utf-8")

            storage.delete_storage_target(target)

            self.assertFalse(target.exists())

    def test_deleted_file_is_not_restored_after_initial_bootstrap(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            app_root = temporary_root / "app"
            data_root = temporary_root / "data"
            legacy_chat = app_root / "chat_uploads"
            chat_uploads = data_root / "chat_uploads"
            security_root = data_root / "security"
            marker = security_root / ".legacy_files_bootstrapped"

            legacy_chat.mkdir(parents=True)
            (legacy_chat / "restore-test.txt").write_text("legacy", encoding="utf-8")

            directories = (data_root, chat_uploads, security_root)
            patches = (
                patch.object(storage, "APP_ROOT", app_root),
                patch.object(storage, "DATA_ROOT", data_root),
                patch.object(storage, "CHAT_UPLOADS", chat_uploads),
                patch.object(storage, "MEMO_UPLOADS", data_root / "memo_uploads"),
                patch.object(storage, "AI_MAIL_UPLOADS", data_root / "ai_mail_uploads"),
                patch.object(storage, "PROFILE_ROOT", data_root / "id"),
                patch.object(storage, "SCHOOL_UPLOADS", data_root / "school_uploads"),
                patch.object(storage, "DEPOSIT_UPLOADS", data_root / "uploads_deposit"),
                patch.object(storage, "SECURITY_ROOT", security_root),
                patch.object(storage, "PERSISTENT_DIRECTORIES", directories),
                patch.object(storage, "LEGACY_BOOTSTRAP_MARKER", marker),
                patch.object(storage, "WINDOWS_LEGACY_RENDER_ROOT", temporary_root / "missing"),
            )

            for active_patch in patches:
                active_patch.start()
                self.addCleanup(active_patch.stop)

            self.assertEqual(storage.bootstrap_legacy_files(), 1)
            migrated_file = chat_uploads / "restore-test.txt"
            self.assertTrue(migrated_file.is_file())
            self.assertTrue(marker.is_file())

            migrated_file.unlink()
            self.assertEqual(storage.bootstrap_legacy_files(), 0)
            self.assertFalse(migrated_file.exists())


if __name__ == "__main__":
    unittest.main()
