"""전자계약 데이터를 saedam.db 안에서 안전하게 관리하는 저장 계층."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from collections.abc import Iterable, Mapping

from .storage import LEGACY_ARCHIVE_ROOT, LEGACY_CONTRACT_DB_FILE


CONTRACT_COLUMNS = (
    "계약구분",
    "수탁학교명",
    "부서명",
    "성명",
    "주민번호",
    "수수료",
    "보조금",
    "경력수당",
    "직책수당",
    "기타",
    "근무시간",
    "계약기간",
    "비고1",
    "비고2",
    "비고3",
    "비고4",
    "email",
    "연락처",
    "거주지",
    "계약완료일시",
    "연도",
    "파일명",
    "IP",
)


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _create_contract_table(conn: sqlite3.Connection) -> None:
    column_sql = ",\n            ".join(
        f"{_quoted(column)} TEXT NOT NULL DEFAULT ''"
        for column in CONTRACT_COLUMNS
    )
    conn.execute(f'''
        CREATE TABLE IF NOT EXISTS contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {column_sql},
            legacy_rowid INTEGER UNIQUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_contracts_name_ssn '
        'ON contracts("성명", "주민번호")'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_contracts_completed '
        'ON contracts("계약완료일시")'
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info({_quoted(table)})').fetchall()
    ]


def _insert_records(
    conn: sqlite3.Connection,
    records: Iterable[Mapping[str, object]],
    *,
    include_legacy_rowid: bool = False,
) -> int:
    records = list(records)
    if not records:
        return 0
    columns = list(CONTRACT_COLUMNS)
    if include_legacy_rowid:
        columns.append("legacy_rowid")
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(_quoted(column) for column in columns)
    values = [
        tuple("" if record.get(column) is None else str(record.get(column, "")) for column in columns)
        for record in records
    ]
    conn.executemany(
        f"INSERT OR IGNORE INTO contracts ({column_sql}) VALUES ({placeholders})",
        values,
    )
    return len(records)


def _upgrade_main_contract_table(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contracts'"
    ).fetchone()
    if not existing:
        _create_contract_table(conn)
        return
    if "id" in _table_columns(conn, "contracts"):
        _create_contract_table(conn)
        return

    conn.execute("ALTER TABLE contracts RENAME TO contracts_legacy_in_main")
    _create_contract_table(conn)
    old_columns = _table_columns(conn, "contracts_legacy_in_main")
    selected = [column for column in CONTRACT_COLUMNS if column in old_columns]
    if selected:
        select_sql = ", ".join(_quoted(column) for column in selected)
        rows = conn.execute(
            f"SELECT rowid, {select_sql} FROM contracts_legacy_in_main ORDER BY rowid"
        ).fetchall()
        records = []
        for row in rows:
            record = {column: row[index + 1] for index, column in enumerate(selected)}
            record["legacy_rowid"] = row[0]
            records.append(record)
        _insert_records(conn, records, include_legacy_rowid=True)


def ensure_contract_schema_and_migrate(conn: sqlite3.Connection) -> int:
    """계약 테이블을 만들고 기존 contracts.db를 최초 1회 병합한다."""
    conn.execute('''
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    _upgrade_main_contract_table(conn)

    migration_key = "contracts_db_merge_v1"
    migrated = conn.execute(
        "SELECT value FROM admin_settings WHERE key=?",
        (migration_key,),
    ).fetchone()
    if migrated:
        return int(conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])

    existing_count = int(conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])
    if existing_count:
        result = f"existing:{existing_count}"
    elif LEGACY_CONTRACT_DB_FILE.is_file():
        legacy_conn = sqlite3.connect(str(LEGACY_CONTRACT_DB_FILE))
        legacy_conn.row_factory = sqlite3.Row
        try:
            legacy_tables = legacy_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contracts'"
            ).fetchone()
            if not legacy_tables:
                return 0
            legacy_columns = _table_columns(legacy_conn, "contracts")
            selected = [column for column in CONTRACT_COLUMNS if column in legacy_columns]
            select_sql = ", ".join(_quoted(column) for column in selected)
            rows = legacy_conn.execute(
                f"SELECT rowid, {select_sql} FROM contracts ORDER BY rowid"
            ).fetchall()
            records = []
            for row in rows:
                record = {column: row[column] for column in selected}
                record["legacy_rowid"] = row["rowid"]
                records.append(record)
            _insert_records(conn, records, include_legacy_rowid=True)
        finally:
            legacy_conn.close()
        imported = int(conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])
        result = f"imported:{imported}"
    else:
        # 나중에 기존 DB가 복원될 수 있으므로 파일이 없을 때는 완료 처리하지 않는다.
        return 0

    conn.execute('''
        INSERT INTO admin_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=CURRENT_TIMESTAMP
    ''', (migration_key, result))
    return int(conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0])


def insert_contract_records(
    conn: sqlite3.Connection,
    records: Iterable[Mapping[str, object]],
) -> int:
    return _insert_records(conn, records)


def update_contract_record(
    conn: sqlite3.Connection,
    contract_id: int,
    values: Mapping[str, object],
) -> int:
    allowed = {
        column: "" if value is None else str(value)
        for column, value in values.items()
        if column in CONTRACT_COLUMNS
    }
    if not allowed:
        return 0
    assignments = ", ".join(f"{_quoted(column)}=?" for column in allowed)
    params = list(allowed.values()) + [int(contract_id)]
    cursor = conn.execute(
        f"UPDATE contracts SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        params,
    )
    return int(cursor.rowcount)


def delete_contract_records(conn: sqlite3.Connection, contract_ids: Iterable[int]) -> int:
    ids = sorted({int(contract_id) for contract_id in contract_ids})
    if not ids:
        return 0
    placeholders = ", ".join("?" for _ in ids)
    cursor = conn.execute(
        f"DELETE FROM contracts WHERE id IN ({placeholders})",
        ids,
    )
    return int(cursor.rowcount)


def archive_legacy_contract_database():
    """병합 완료 후 예전 DB를 삭제하지 않고 복구용 폴더로 이동한다."""
    if not LEGACY_CONTRACT_DB_FILE.is_file():
        return None
    legacy_conn = sqlite3.connect(str(LEGACY_CONTRACT_DB_FILE))
    try:
        integrity = legacy_conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        legacy_conn.close()
    if integrity != "ok":
        return None

    LEGACY_ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    target = LEGACY_ARCHIVE_ROOT / "contracts_merged_backup.db"
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = LEGACY_ARCHIVE_ROOT / f"contracts_merged_backup_{stamp}.db"
    LEGACY_CONTRACT_DB_FILE.replace(target)
    return target
