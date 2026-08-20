"""스마트명세서 설정 세 종류의 재시작 후 유지 회귀 테스트."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path


class PayrollPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.test_directory = tempfile.TemporaryDirectory(
            prefix="saedam-payroll-persistence-"
        )
        self.database_path = Path(self.test_directory.name) / "saedam.db"

    def tearDown(self):
        self.test_directory.cleanup()

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_payroll_settings_survive_database_reopen(self):
        connection = self._connect()
        connection.executescript("""
            CREATE TABLE payroll_workgroups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_emp_no TEXT NOT NULL,
                name TEXT NOT NULL,
                form_type TEXT NOT NULL DEFAULT 'form_basic',
                subject TEXT NOT NULL,
                body_html TEXT NOT NULL DEFAULT '',
                banner1_asset_id INTEGER,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner_emp_no, name)
            );
            CREATE TABLE payroll_image_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_emp_no TEXT NOT NULL,
                asset_kind TEXT NOT NULL,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner_emp_no, asset_kind, name)
            );
            CREATE TABLE payroll_mail_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_emp_no TEXT NOT NULL,
                template_key TEXT,
                name TEXT NOT NULL,
                match_keywords TEXT,
                body_html TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(owner_emp_no, name)
            );
        """)
        asset_id = connection.execute("""
            INSERT INTO payroll_image_assets (
                owner_emp_no, asset_kind, name, source_type, source_value
            ) VALUES (?, ?, ?, ?, ?)
        """, ("tester", "banner", "재배포 확인 광고", "file-encrypted", "encrypted-image-data")).lastrowid
        connection.execute("""
            INSERT INTO payroll_workgroups (
                owner_emp_no, name, form_type, subject, body_html, banner1_asset_id
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, ("tester", "재배포 확인 그룹", "form_basic", "급여명세서", "<p>본문</p>", asset_id))
        template_id = connection.execute("""
            INSERT INTO payroll_mail_templates (
                owner_emp_no, template_key, name, match_keywords, body_html
            ) VALUES (?, ?, ?, ?, ?)
        """, ("tester", "form_basic", "근로자 폼", "기본 문구", "<p>명세서</p>")).lastrowid
        connection.execute("""
            UPDATE payroll_mail_templates
            SET match_keywords=?
            WHERE id=? AND owner_emp_no=?
        """, ("직원근로자, 센터장근로자, 임직원", template_id, "tester"))
        connection.commit()
        connection.close()

        # 재배포된 앱이 같은 Persistent Disk의 DB에 새 연결을 여는 상황을 모사한다.
        reopened = self._connect()
        saved_workspace = reopened.execute("""
            SELECT g.name AS group_name, a.name AS asset_name, a.source_value
            FROM payroll_workgroups g
            JOIN payroll_image_assets a ON a.id=g.banner1_asset_id
            WHERE g.owner_emp_no=?
        """, ("tester",)).fetchone()
        saved_keywords = reopened.execute("""
            SELECT match_keywords
            FROM payroll_mail_templates
            WHERE owner_emp_no=? AND template_key=?
        """, ("tester", "form_basic")).fetchone()
        reopened.close()

        self.assertIsNotNone(saved_workspace)
        self.assertEqual(saved_workspace["group_name"], "재배포 확인 그룹")
        self.assertEqual(saved_workspace["asset_name"], "재배포 확인 광고")
        self.assertEqual(saved_workspace["source_value"], "encrypted-image-data")
        self.assertIsNotNone(saved_keywords)
        self.assertEqual(
            saved_keywords["match_keywords"],
            "직원근로자, 센터장근로자, 임직원",
        )


if __name__ == "__main__":
    unittest.main()
