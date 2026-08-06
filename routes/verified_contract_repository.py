"""인증전자계약 데이터와 감사기록 저장 계층."""

from __future__ import annotations

import json
from collections.abc import Mapping


VERIFIED_CONTRACT_FIELDS = (
    "contract_type",
    "school_name",
    "department",
    "signer_name",
    "signer_email",
    "signer_phone",
    "signer_address",
    "signer_rrn_encrypted",
    "signer_bank_encrypted",
    "signer_account_encrypted",
    "contract_data_json",
    "status",
    "version",
    "title_snapshot",
    "terms1_snapshot",
    "terms2_snapshot",
    "company_snapshot_json",
    "agreement_snapshot_json",
    "invitation_token_hash",
    "invitation_expires_at",
    "invitation_sent_at",
    "opened_at",
    "otp_hash",
    "otp_expires_at",
    "otp_attempts",
    "otp_sent_at",
    "verified_at",
    "confirmed_name",
    "signature_filename",
    "signed_at",
    "ip_address",
    "user_agent",
    "pdf_filename",
    "pdf_sha256",
    "invite_mail_status",
    "invite_mail_error",
    "completion_mail_status",
    "completion_mail_error",
    "created_by",
)


def ensure_verified_contract_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verified_contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_type TEXT NOT NULL,
            school_name TEXT NOT NULL DEFAULT '',
            department TEXT NOT NULL DEFAULT '',
            signer_name TEXT NOT NULL,
            signer_email TEXT NOT NULL,
            signer_phone TEXT NOT NULL DEFAULT '',
            signer_address TEXT NOT NULL DEFAULT '',
            signer_rrn_encrypted TEXT NOT NULL DEFAULT '',
            signer_bank_encrypted TEXT NOT NULL DEFAULT '',
            signer_account_encrypted TEXT NOT NULL DEFAULT '',
            contract_data_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            version INTEGER NOT NULL DEFAULT 1,
            title_snapshot TEXT NOT NULL DEFAULT '',
            terms1_snapshot TEXT NOT NULL DEFAULT '',
            terms2_snapshot TEXT NOT NULL DEFAULT '',
            company_snapshot_json TEXT NOT NULL DEFAULT '{}',
            agreement_snapshot_json TEXT NOT NULL DEFAULT '[]',
            invitation_token_hash TEXT NOT NULL UNIQUE,
            invitation_expires_at TEXT NOT NULL,
            invitation_sent_at TEXT,
            opened_at TEXT,
            otp_hash TEXT,
            otp_expires_at TEXT,
            otp_attempts INTEGER NOT NULL DEFAULT 0,
            otp_sent_at TEXT,
            verified_at TEXT,
            confirmed_name TEXT,
            signature_filename TEXT,
            signed_at TEXT,
            ip_address TEXT,
            user_agent TEXT,
            pdf_filename TEXT,
            pdf_sha256 TEXT,
            invite_mail_status TEXT NOT NULL DEFAULT 'waiting',
            invite_mail_error TEXT,
            completion_mail_status TEXT,
            completion_mail_error TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    existing_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(verified_contracts)").fetchall()
    }
    for column in (
        "signer_rrn_encrypted",
        "signer_bank_encrypted",
        "signer_account_encrypted",
    ):
        if column not in existing_columns:
            conn.execute(
                f"ALTER TABLE verified_contracts "
                f"ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verified_contract_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            details_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY(contract_id) REFERENCES verified_contracts(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verified_contract_status "
        "ON verified_contracts(status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verified_contract_email "
        "ON verified_contracts(signer_email)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_verified_contract_event "
        "ON verified_contract_events(contract_id, event_at)"
    )


def insert_verified_contract(conn, values: Mapping[str, object]) -> int:
    allowed = {
        key: value
        for key, value in values.items()
        if key in VERIFIED_CONTRACT_FIELDS
    }
    columns = list(allowed)
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    cursor = conn.execute(
        f"INSERT INTO verified_contracts ({column_sql}) VALUES ({placeholders})",
        [allowed[column] for column in columns],
    )
    return int(cursor.lastrowid)


def update_verified_contract(conn, contract_id: int, values: Mapping[str, object]) -> int:
    allowed = {
        key: value
        for key, value in values.items()
        if key in VERIFIED_CONTRACT_FIELDS
    }
    if not allowed:
        return 0
    assignments = ", ".join(f"{key}=?" for key in allowed)
    cursor = conn.execute(
        f"""
        UPDATE verified_contracts
        SET {assignments}, updated_at=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        [*allowed.values(), int(contract_id)],
    )
    return int(cursor.rowcount)


def add_verified_contract_event(
    conn,
    contract_id: int,
    event_type: str,
    event_at: str,
    *,
    ip_address: str = "",
    user_agent: str = "",
    details: Mapping[str, object] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO verified_contract_events (
            contract_id, event_type, event_at, ip_address, user_agent, details_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(contract_id),
            str(event_type),
            str(event_at),
            str(ip_address or ""),
            str(user_agent or "")[:500],
            json.dumps(dict(details or {}), ensure_ascii=False),
        ),
    )
