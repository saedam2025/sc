import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routes.database import ensure_certificate_schema


class CertificateWorkgroupSchemaTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()

    def test_existing_certificate_table_is_extended(self):
        self.conn.execute("""
            CREATE TABLE certificate_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                certificate_type TEXT NOT NULL,
                applicant_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '대기'
            )
        """)

        ensure_certificate_schema(self.conn)

        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(certificate_requests)")
        }
        self.assertTrue(
            {"workgroup_id", "company_id", "workgroup_name", "company_name"}
            <= columns
        )
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("certificate_companies", tables)
        self.assertIn("certificate_workgroups", tables)
        company_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(certificate_companies)")
        }
        self.assertTrue({"logo_filename", "logo_path"} <= company_columns)

    def test_company_and_workgroup_can_be_registered(self):
        ensure_certificate_schema(self.conn)
        company_id = self.conn.execute("""
            INSERT INTO certificate_companies (company_name, representative_name)
            VALUES ('테스트회사', '홍길동')
        """).lastrowid
        workgroup_id = self.conn.execute("""
            INSERT INTO certificate_workgroups (
                name, company_id, access_token, allow_instructor, allow_employee
            ) VALUES ('테스트그룹', ?, 'test-token', 1, 1)
        """, (company_id,)).lastrowid

        row = self.conn.execute("""
            SELECT w.name, c.company_name, c.representative_name
            FROM certificate_workgroups w
            JOIN certificate_companies c ON c.id=w.company_id
            WHERE w.id=?
        """, (workgroup_id,)).fetchone()
        self.assertEqual(row["name"], "테스트그룹")
        self.assertEqual(row["company_name"], "테스트회사")
        self.assertEqual(row["representative_name"], "홍길동")


if __name__ == "__main__":
    unittest.main()
