"""사용자 활동 포인트 적립, 오늘 순위, 포인트 선물 기능."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing

from flask import Blueprint, jsonify, request, session

from .database import get_db


points_bp = Blueprint('points', __name__)

ACTIVITY_POINT_RULES = {
    ('POST', 'login'): ('login', 2, '로그인'),
    ('POST', 'board.board_write'): ('post', 5, '게시물 등록'),
    ('POST', 'school.add_post'): ('post', 5, '학교 게시물 등록'),
    ('POST', 'gall2.upload'): ('post', 5, '갤러리 게시물 등록'),
    ('POST', 'gallery.upload'): ('post', 5, '갤러리 등록'),
    ('POST', 'main.save_board'): ('post', 5, '게시물 등록'),
}


def ensure_point_schema(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS point_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            user_name TEXT NOT NULL,
            points_delta INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            activity_type TEXT NOT NULL,
            ranking_points INTEGER NOT NULL DEFAULT 0,
            counterparty_name TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_point_transactions_user_date
        ON point_transactions(user_name, created_at)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_point_transactions_ranking_date
        ON point_transactions(ranking_points, created_at)
    ''')


def _balance(conn: sqlite3.Connection, user_name: str) -> int:
    row = conn.execute('''
        SELECT COALESCE(SUM(points_delta), 0) AS balance
        FROM point_transactions
        WHERE user_name=?
    ''', (user_name,)).fetchone()
    return int(row['balance'] or 0)


def get_point_balance(user_name: str, conn: sqlite3.Connection | None = None) -> int:
    if not user_name:
        return 0
    owns_connection = conn is None
    db = conn or get_db()
    try:
        ensure_point_schema(db)
        if owns_connection:
            db.commit()
        return _balance(db, user_name)
    finally:
        if owns_connection:
            db.close()


def get_point_balances(conn: sqlite3.Connection, user_names: list[str]) -> dict[str, int]:
    names = list(dict.fromkeys(name for name in user_names if name))
    if not names:
        return {}
    ensure_point_schema(conn)
    placeholders = ','.join('?' for _ in names)
    rows = conn.execute(f'''
        SELECT user_name, COALESCE(SUM(points_delta), 0) AS balance
        FROM point_transactions
        WHERE user_name IN ({placeholders})
        GROUP BY user_name
    ''', names).fetchall()
    balances = {name: 0 for name in names}
    balances.update({row['user_name']: int(row['balance'] or 0) for row in rows})
    return balances


def award_activity_points(
    user_name: str,
    points: int,
    activity_type: str,
    note: str,
    *,
    event_key: str | None = None,
) -> int:
    """활동 포인트를 원장에 기록하고 변경 후 잔액을 반환한다."""
    if not user_name or points <= 0:
        return 0
    conn = get_db()
    try:
        ensure_point_schema(conn)
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        current_balance = _balance(conn, user_name)
        new_balance = current_balance + int(points)
        conn.execute('''
            INSERT INTO point_transactions
                (event_key, user_name, points_delta, balance_after, activity_type,
                 ranking_points, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            event_key or uuid.uuid4().hex,
            user_name,
            int(points),
            new_balance,
            activity_type,
            int(points),
            note,
        ))
        conn.commit()
        return new_balance
    except sqlite3.IntegrityError:
        conn.rollback()
        return _balance(conn, user_name)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def award_response_activity(method: str, endpoint: str | None, user_name: str) -> int | None:
    rule = ACTIVITY_POINT_RULES.get((method.upper(), endpoint or ''))
    if not rule or not user_name:
        return None
    activity_type, points, note = rule
    return award_activity_points(user_name, points, activity_type, note)


def _today_points(conn: sqlite3.Connection, user_name: str) -> int:
    row = conn.execute('''
        SELECT COALESCE(SUM(
            CASE
                WHEN activity_type IN ('gift_sent', 'gift_received') THEN points_delta
                ELSE ranking_points
            END
        ), 0) AS points
        FROM point_transactions
        WHERE user_name=?
          AND substr(created_at, 1, 10)=date('now', 'localtime')
    ''', (user_name,)).fetchone()
    return int(row['points'] or 0)


def get_today_rankings(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    ensure_point_schema(conn)
    rows = conn.execute('''
        SELECT
            u.name,
            COALESCE(u.profile_icon, '👤') AS icon,
            COALESCE(today.points, 0) AS today_points,
            COALESCE(total.balance, 0) AS balance
        FROM users u
        LEFT JOIN (
            SELECT user_name, SUM(
                CASE
                    WHEN activity_type IN ('gift_sent', 'gift_received') THEN points_delta
                    ELSE ranking_points
                END
            ) AS points
            FROM point_transactions
            WHERE substr(created_at, 1, 10)=date('now', 'localtime')
            GROUP BY user_name
        ) today ON today.user_name=u.name
        LEFT JOIN (
            SELECT user_name, SUM(points_delta) AS balance
            FROM point_transactions
            GROUP BY user_name
        ) total ON total.user_name=u.name
        WHERE u.status='승인'
          AND lower(COALESCE(u.emp_no, '')) <> 'admin'
          AND lower(COALESCE(u.name, '')) <> 'admin'
        ORDER BY COALESCE(today.points, 0) DESC,
                 COALESCE(total.balance, 0) DESC,
                 u.level ASC,
                 u.name ASC
        LIMIT ?
    ''', (max(1, min(int(limit), 10)),)).fetchall()
    return [
        {
            'rank': index,
            'name': row['name'] or '',
            'icon': row['icon'] or '👤',
            'today_points': int(row['today_points'] or 0),
            'balance': int(row['balance'] or 0),
        }
        for index, row in enumerate(rows, start=1)
    ]


@points_bp.route('/api/points/me')
def my_points():
    user_name = str(session.get('user_name') or '').strip()
    if not user_name:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    with closing(get_db()) as conn:
        ensure_point_schema(conn)
        return jsonify({
            'status': 'success',
            'balance': _balance(conn, user_name),
            'today_points': _today_points(conn, user_name),
        })


@points_bp.route('/api/points/rankings')
def today_rankings():
    user_name = str(session.get('user_name') or '').strip()
    if not user_name:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401
    with closing(get_db()) as conn:
        ensure_point_schema(conn)
        return jsonify({
            'status': 'success',
            'balance': _balance(conn, user_name),
            'today_points': _today_points(conn, user_name),
            'rankings': get_today_rankings(conn, 10),
        })


@points_bp.route('/api/points/gift', methods=['POST'])
def gift_points():
    sender = str(session.get('user_name') or '').strip()
    if not sender:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    recipient = str(data.get('recipient') or '').strip()
    raw_amount = data.get('amount')
    if isinstance(raw_amount, int) and not isinstance(raw_amount, bool):
        amount = raw_amount
    elif isinstance(raw_amount, str) and raw_amount.strip().isdigit():
        amount = int(raw_amount.strip())
    else:
        amount = 0
    if amount <= 0 or amount > 1_000_000:
        return jsonify({'status': 'error', 'message': '선물 포인트를 올바르게 입력해 주세요.'}), 400
    if not recipient or recipient == sender:
        return jsonify({'status': 'error', 'message': '자신에게는 포인트를 선물할 수 없습니다.'}), 400

    conn = get_db()
    try:
        ensure_point_schema(conn)
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        recipient_row = conn.execute('''
            SELECT name FROM users
            WHERE name=? AND status='승인'
              AND lower(COALESCE(emp_no, '')) <> 'admin'
            LIMIT 1
        ''', (recipient,)).fetchone()
        if not recipient_row:
            conn.rollback()
            return jsonify({'status': 'error', 'message': '선물할 구성원을 찾을 수 없습니다.'}), 404

        sender_balance = _balance(conn, sender)
        if sender_balance < amount:
            conn.rollback()
            return jsonify({
                'status': 'error',
                'message': f'보유 포인트({sender_balance}점)를 초과해 선물할 수 없습니다.',
                'balance': sender_balance,
            }), 400

        recipient_balance = _balance(conn, recipient)
        transfer_key = uuid.uuid4().hex
        new_sender_balance = sender_balance - amount
        new_recipient_balance = recipient_balance + amount
        conn.execute('''
            INSERT INTO point_transactions
                (event_key, user_name, points_delta, balance_after, activity_type,
                 ranking_points, counterparty_name, note)
            VALUES (?, ?, ?, ?, 'gift_sent', 0, ?, ?)
        ''', (
            f'{transfer_key}:sent', sender, -amount, new_sender_balance,
            recipient, f'{recipient}님에게 포인트 선물',
        ))
        conn.execute('''
            INSERT INTO point_transactions
                (event_key, user_name, points_delta, balance_after, activity_type,
                 ranking_points, counterparty_name, note)
            VALUES (?, ?, ?, ?, 'gift_received', 0, ?, ?)
        ''', (
            f'{transfer_key}:received', recipient, amount, new_recipient_balance,
            sender, f'{sender}님에게 포인트 선물 받음',
        ))
        conn.commit()
        return jsonify({
            'status': 'success',
            'message': f'{recipient}님에게 {amount}점을 선물했습니다.',
            'balance': new_sender_balance,
            'recipient_balance': new_recipient_balance,
        })
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
