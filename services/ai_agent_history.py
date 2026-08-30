"""AI에이전트 좌측 대화 기록과 질문별 크레딧(토큰) 사용 로그.

회원(emp_no)별로 최근 질문/답변을 저장한다. 고정(pin)한 기록을 포함해 총
MAX_TOTAL_HISTORY개까지만 보관하며, 총 개수가 초과되면 고정되지 않은 기록 중
오래된 것부터 자동 삭제한다. 고정된 기록은 삭제 대상에서만 제외될 뿐
개수 제한 자체에는 포함된다.
"""

from __future__ import annotations

import json
from typing import Any

from routes.database import get_db


MAX_TOTAL_HISTORY = 10
QUESTION_PREVIEW_LENGTH = 80

# OpenAI 공개 요금표 기준 1,000토큰당 예상 단가(USD). 등록되지 않은 모델은
# 접두어로 매칭하고, 그마저 없으면 비용을 계산하지 않고 토큰 수만 표시한다.
MODEL_PRICING_USD_PER_1K: dict[str, tuple[float, float]] = {
    "gpt-4.1-nano": (0.0001, 0.0004),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "o4-mini": (0.0011, 0.0044),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    key = str(model or "").strip().lower()
    pricing = MODEL_PRICING_USD_PER_1K.get(key)
    if not pricing:
        for prefix, value in MODEL_PRICING_USD_PER_1K.items():
            if key.startswith(prefix):
                pricing = value
                break
    if not pricing:
        return None
    input_price, output_price = pricing
    cost = (max(int(input_tokens or 0), 0) / 1000) * input_price
    cost += (max(int(output_tokens or 0), 0) / 1000) * output_price
    return round(cost, 6)


def _prune(conn, emp_no: str) -> None:
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM ai_agent_history WHERE emp_no=?", (emp_no,),
    ).fetchone()["c"]
    overflow = total - MAX_TOTAL_HISTORY
    if overflow <= 0:
        return
    rows = conn.execute(
        """SELECT id FROM ai_agent_history
           WHERE emp_no=? AND pinned=0
           ORDER BY created_at ASC, id ASC
           LIMIT ?""",
        (emp_no, overflow),
    ).fetchall()
    stale_ids = [row["id"] for row in rows]
    if stale_ids:
        conn.executemany(
            "DELETE FROM ai_agent_history WHERE id=?",
            [(rid,) for rid in stale_ids],
        )


def _preview(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > QUESTION_PREVIEW_LENGTH:
        return text[:QUESTION_PREVIEW_LENGTH] + "…"
    return text


def record_history(
    emp_no: str,
    question: str,
    answer_text: str,
    payload: dict[str, Any],
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    emp_no = str(emp_no or "").strip()
    if not emp_no:
        return
    input_tokens = max(int(input_tokens or 0), 0)
    output_tokens = max(int(output_tokens or 0), 0)
    total_tokens = input_tokens + output_tokens
    cost = estimate_cost_usd(model, input_tokens, output_tokens)
    try:
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload_json = "{}"

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO ai_agent_history
               (emp_no, question, answer_text, answer_payload, model,
                input_tokens, output_tokens, total_tokens, estimated_cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                emp_no, str(question or "")[:2000], str(answer_text or "")[:4000],
                payload_json[:20000], str(model or ""),
                input_tokens, output_tokens, total_tokens,
                cost if cost is not None else 0.0,
            ),
        )
        _prune(conn, emp_no)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_history(emp_no: str) -> list[dict[str, Any]]:
    emp_no = str(emp_no or "").strip()
    if not emp_no:
        return []
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, question, model, input_tokens, output_tokens, total_tokens,
                      estimated_cost_usd, pinned, created_at
               FROM ai_agent_history
               WHERE emp_no=?
               ORDER BY pinned DESC, created_at DESC, id DESC
               LIMIT 200""",
            (emp_no,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": row["id"],
            "question": _preview(row["question"]),
            "model": row["model"] or "",
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "estimated_cost_usd": float(row["estimated_cost_usd"] or 0.0),
            "pinned": bool(row["pinned"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def get_history_item(emp_no: str, item_id: int) -> dict[str, Any] | None:
    emp_no = str(emp_no or "").strip()
    if not emp_no:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, question, answer_text, answer_payload, model,
                      input_tokens, output_tokens, total_tokens,
                      estimated_cost_usd, pinned, created_at
               FROM ai_agent_history WHERE id=? AND emp_no=?""",
            (item_id, emp_no),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["answer_payload"] or "{}")
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
    except (TypeError, ValueError):
        payload = {"type": "text", "message": row["answer_text"] or ""}
    return {
        "id": row["id"],
        "question": row["question"],
        "answer_text": row["answer_text"] or "",
        "payload": payload,
        "model": row["model"] or "",
        "input_tokens": int(row["input_tokens"] or 0),
        "output_tokens": int(row["output_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "estimated_cost_usd": float(row["estimated_cost_usd"] or 0.0),
        "pinned": bool(row["pinned"]),
        "created_at": row["created_at"],
    }


def set_pinned(emp_no: str, item_id: int, pinned: bool) -> bool:
    emp_no = str(emp_no or "").strip()
    if not emp_no:
        return False
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE ai_agent_history SET pinned=? WHERE id=? AND emp_no=?",
            (1 if pinned else 0, item_id, emp_no),
        )
        changed = cursor.rowcount > 0
        if changed and not pinned:
            _prune(conn, emp_no)
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def usage_overview(emp_no: str) -> dict[str, Any]:
    """가장 최근 질문의 사용량과 당일/누적 합계를 반환한다."""
    emp_no = str(emp_no or "").strip()
    empty = {
        "latest": None, "today_total_tokens": 0, "today_cost_usd": 0.0,
        "all_total_tokens": 0, "all_cost_usd": 0.0, "all_count": 0,
    }
    if not emp_no:
        return empty
    conn = get_db()
    try:
        latest = conn.execute(
            """SELECT question, model, input_tokens, output_tokens, total_tokens,
                      estimated_cost_usd, created_at
               FROM ai_agent_history WHERE emp_no=?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (emp_no,),
        ).fetchone()
        today = conn.execute(
            """SELECT COALESCE(SUM(total_tokens),0) AS tokens,
                      COALESCE(SUM(estimated_cost_usd),0) AS cost
               FROM ai_agent_history
               WHERE emp_no=? AND DATE(created_at, '+9 hours')=DATE('now', '+9 hours')""",
            (emp_no,),
        ).fetchone()
        overall = conn.execute(
            """SELECT COALESCE(SUM(total_tokens),0) AS tokens,
                      COALESCE(SUM(estimated_cost_usd),0) AS cost,
                      COUNT(*) AS count
               FROM ai_agent_history WHERE emp_no=?""",
            (emp_no,),
        ).fetchone()
    finally:
        conn.close()
    if not latest:
        return empty
    return {
        "latest": {
            "question": _preview(latest["question"]),
            "model": latest["model"] or "",
            "input_tokens": int(latest["input_tokens"] or 0),
            "output_tokens": int(latest["output_tokens"] or 0),
            "total_tokens": int(latest["total_tokens"] or 0),
            "estimated_cost_usd": float(latest["estimated_cost_usd"] or 0.0),
            "created_at": latest["created_at"],
        },
        "today_total_tokens": int(today["tokens"] or 0),
        "today_cost_usd": float(today["cost"] or 0.0),
        "all_total_tokens": int(overall["tokens"] or 0),
        "all_cost_usd": float(overall["cost"] or 0.0),
        "all_count": int(overall["count"] or 0),
    }
