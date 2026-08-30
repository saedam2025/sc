"""새담인트라넷 사내 AI 업무비서 화면과 API."""

from __future__ import annotations

import secrets
import time
from functools import wraps

from flask import Blueprint, current_app, jsonify, render_template, request, session

from routes.menu_access import build_menu_access, center_director_mode_active
from services.ai_agent_history import (
    get_history_item,
    list_history,
    set_pinned,
    usage_overview,
)
from services.openai_agent import (
    OpenAIAgentError,
    ask_ai_agent,
    clear_conversation,
    get_ai_agent_configuration,
    new_conversation_id,
)


ai_agent_bp = Blueprint("ai_agent", __name__)
AI_ERROR_MESSAGE = "AI 응답을 처리하는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."


def _csrf_token() -> str:
    token = session.get("ai_agent_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["ai_agent_csrf_token"] = token
    return str(token)


def _csrf_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        supplied = str(request.headers.get("X-CSRF-Token") or "")
        expected = str(session.get("ai_agent_csrf_token") or "")
        if not expected or not secrets.compare_digest(supplied, expected):
            return jsonify({"status": "error", "message": "요청 보안 토큰이 올바르지 않습니다."}), 403
        return view(*args, **kwargs)

    return wrapped


def _conversation_id() -> str:
    value = str(session.get("ai_agent_conversation_id") or "")
    if not value:
        value = new_conversation_id()
        session["ai_agent_conversation_id"] = value
    return value


def _user_context() -> dict:
    try:
        level = int(session.get("user_level", 99))
    except (TypeError, ValueError):
        level = 99
    return {
        "emp_no": str(session.get("emp_no") or ""),
        "user_name": str(session.get("user_name") or ""),
        "user_level": level,
        "position": str(session.get("position") or ""),
        "department": str(session.get("department") or ""),
        "menu_access": build_menu_access(level),
        "center_director_mode": center_director_mode_active(level),
    }


@ai_agent_bp.route("/ai-agent", strict_slashes=False)
def index():
    configuration = get_ai_agent_configuration(_user_context())
    return render_template(
        "ai_agent.html",
        ai_agent_csrf_token=_csrf_token(),
        ai_agent_configuration=configuration,
        ai_agent_model_configured=configuration["configured"],
    )


@ai_agent_bp.route("/ai-agent/api/chat", methods=["POST"])
@_csrf_required
def chat():
    # 빠른 중복 클릭은 브라우저뿐 아니라 서버에서도 한 번 더 차단한다.
    now = time.monotonic()
    last_request = float(session.get("ai_agent_last_request_at", 0.0) or 0.0)
    if now - last_request < 0.7:
        return jsonify({"status": "error", "message": "이전 질문을 처리 중입니다. 잠시 후 다시 시도해 주세요."}), 429
    session["ai_agent_last_request_at"] = now

    data = request.get_json(silent=True) or {}
    question = str(data.get("question") or "").strip()
    if not question:
        return jsonify({"status": "error", "message": "질문을 입력해 주세요."}), 400
    if len(question) > 2000:
        return jsonify({"status": "error", "message": "질문은 2,000자 이내로 입력해 주세요."}), 400

    try:
        answer = ask_ai_agent(question, _conversation_id(), _user_context())
        return jsonify({"status": "success", "answer": answer})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except OpenAIAgentError:
        current_app.logger.exception("AI에이전트 OpenAI 응답 처리 실패")
    except Exception:
        current_app.logger.exception("AI에이전트 예기치 않은 처리 실패")
    return jsonify({"status": "error", "message": AI_ERROR_MESSAGE}), 503


@ai_agent_bp.route("/ai-agent/api/reset", methods=["POST"])
@_csrf_required
def reset():
    conversation_id = str(session.pop("ai_agent_conversation_id", "") or "")
    if conversation_id:
        clear_conversation(conversation_id)
    session.pop("ai_agent_last_request_at", None)
    return jsonify({"status": "success", "message": "새 대화를 시작했습니다."})


def _emp_no() -> str:
    return str(session.get("emp_no") or "")


@ai_agent_bp.route("/ai-agent/api/history", methods=["GET"])
def history_list():
    return jsonify({
        "status": "success",
        "history": list_history(_emp_no()),
        "usage": usage_overview(_emp_no()),
        "max_total": 10,
    })


@ai_agent_bp.route("/ai-agent/api/history/<int:item_id>", methods=["GET"])
def history_item(item_id: int):
    item = get_history_item(_emp_no(), item_id)
    if not item:
        return jsonify({"status": "error", "message": "기록을 찾을 수 없습니다."}), 404
    return jsonify({"status": "success", "item": item})


@ai_agent_bp.route("/ai-agent/api/history/<int:item_id>/pin", methods=["POST"])
@_csrf_required
def history_pin(item_id: int):
    data = request.get_json(silent=True) or {}
    pinned = bool(data.get("pinned"))
    changed = set_pinned(_emp_no(), item_id, pinned)
    if not changed:
        return jsonify({"status": "error", "message": "기록을 찾을 수 없습니다."}), 404
    return jsonify({"status": "success", "pinned": pinned})
