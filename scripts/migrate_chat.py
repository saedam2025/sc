import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from routes.chat import _ensure_chat_tables
from routes.database import get_db


def main():
    conn = get_db()
    try:
        before = int(
            conn.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
        )
        _ensure_chat_tables(conn)
        missing_uid = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM messages
                WHERE message_uid IS NULL OR message_uid=''
                """
            ).fetchone()["count"]
        )
        tables = [
            row["name"]
            for row in conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name LIKE 'chat_%'
                ORDER BY name
                """
            ).fetchall()
        ]
        possible_legacy_collisions = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM (
                    SELECT message_uid
                    FROM messages
                    WHERE room_id IS NOT NULL
                    GROUP BY message_uid
                    HAVING COUNT(*) > COUNT(DISTINCT receiver)
                )
                """
            ).fetchone()["count"]
        )
        print(f"messages={before} missing_uid={missing_uid}")
        print(f"possible_legacy_collisions={possible_legacy_collisions}")
        print("chat_tables=" + ",".join(tables))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
