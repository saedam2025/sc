"""통합관리에서 사용하는 SOLAPI 설정과 메시지 발송 도우미."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken

from .database import get_db
from .security import load_credential_secret


SETTINGS_KEY = "solapi_settings"
SOLAPI_SEND_URL = "https://api.solapi.com/messages/v4/send-many/detail"
CONTRACT_PUBLIC_ORIGIN = "https://works.saedam.org"
# 알림톡 실패 시 나가는 대체문자 제목. LMS는 제목이 비면 접수되지 않는다.
ALIMTALK_FALLBACK_SUBJECT = "새담 전자계약 안내"
# 설문조사 등 링크 문자에 사용하는 기본 기관명. 통합관리에서 바꿀 수 있다.
DEFAULT_SENDER_NAME = "새담"
# SMS 1건의 최대 본문 길이(EUC-KR 기준 바이트). 넘으면 LMS로 전환한다.
SMS_BYTE_LIMIT = 90
# 한 번의 send-many 요청에 담을 최대 건수.
BULK_CHUNK_SIZE = 100


def _fernet() -> Fernet:
    digest = hashlib.sha256(
        f"saedam-solapi-settings:{load_credential_secret()}".encode("utf-8")
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value: str) -> str:
    text = str(value or "").strip()
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii") if text else ""


def _decrypt(token: object) -> str:
    text = str(token or "").strip()
    if not text:
        return ""
    try:
        return _fernet().decrypt(text.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "저장된 SOLAPI 자격증명을 복호화할 수 없습니다. 통합관리에서 다시 저장해 주세요."
        ) from exc


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _load_store(conn) -> dict[str, Any]:
    _ensure_table(conn)
    row = conn.execute(
        "SELECT value, updated_at FROM admin_settings WHERE key=?", (SETTINGS_KEY,)
    ).fetchone()
    if not row or not row["value"]:
        return {}
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    data["updated_at"] = str(row["updated_at"] or data.get("updated_at") or "")
    return data


def normalize_phone(value: object, *, required: bool = False) -> str:
    """대한민국 휴대폰번호를 SOLAPI 전송용 숫자 형식으로 정규화한다."""
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise ValueError("휴대폰번호를 입력해 주세요.")
        return ""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("82"):
        digits = "0" + digits[2:]
    # 엑셀에서 휴대폰번호를 숫자로 저장하면 맨 앞의 0이 사라질 수 있다.
    if re.fullmatch(r"1[016789]\d{7,8}", digits):
        digits = "0" + digits
    if not re.fullmatch(r"01[016789]\d{7,8}", digits):
        raise ValueError("휴대폰번호는 010-1234-5678 형식으로 입력해 주세요.")
    return digits


def format_phone(value: object) -> str:
    digits = normalize_phone(value)
    if not digits:
        return ""
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"


def mask_phone(value: object) -> str:
    formatted = format_phone(value)
    if not formatted:
        return "-"
    parts = formatted.split("-")
    return f"{parts[0]}-{'*' * len(parts[1])}-{parts[2]}"


def _environment_settings() -> dict[str, str]:
    return {
        "api_key": str(os.environ.get("SOLAPI_API_KEY", "")).strip(),
        "api_secret": str(os.environ.get("SOLAPI_API_SECRET", "")).strip(),
        "pf_id": str(os.environ.get("SOLAPI_PF_ID", "")).strip(),
        "template_id": str(os.environ.get("SOLAPI_TEMPLATE_ID", "")).strip(),
        "from_number": re.sub(r"\D", "", str(os.environ.get("SOLAPI_FROM", ""))),
        "public_origin": str(os.environ.get("PUBLIC_ORIGIN", "")).strip().rstrip("/"),
        "sender_name": str(os.environ.get("SOLAPI_SENDER_NAME", "")).strip(),
    }


def normalize_origin(value: object) -> str:
    """문자에 담을 공개 링크의 기준 주소를 검증한다."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc             or parsed.path or parsed.query or parsed.fragment             or re.search(r"\s", text):
        raise ValueError("공개 링크 주소는 https://example.com 형식으로 입력해 주세요.")
    return f"{parsed.scheme}://{parsed.netloc}"


def get_settings(conn=None) -> dict[str, Any]:
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        store = _load_store(conn)
    finally:
        if owns_connection:
            conn.close()

    environment = _environment_settings()
    api_key = _decrypt(store.get("api_key_encrypted")) if store.get("api_key_encrypted") else ""
    api_secret = (
        _decrypt(store.get("api_secret_encrypted"))
        if store.get("api_secret_encrypted")
        else ""
    )
    result = {
        "api_key": api_key or environment["api_key"],
        "api_secret": api_secret or environment["api_secret"],
        "pf_id": str(store.get("pf_id") or environment["pf_id"]).strip(),
        "template_id": str(store.get("template_id") or environment["template_id"]).strip(),
        "from_number": re.sub(
            r"\D", "", str(store.get("from_number") or environment["from_number"])
        ),
        "public_origin": str(
            store.get("public_origin")
            or environment["public_origin"]
            or CONTRACT_PUBLIC_ORIGIN
        ).rstrip("/"),
        "sender_name": str(
            store.get("sender_name") or environment["sender_name"] or DEFAULT_SENDER_NAME
        ),
        "updated_by": str(store.get("updated_by") or ""),
        "updated_at": str(store.get("updated_at") or ""),
    }
    configured_from_db = any(
        store.get(key)
        for key in (
            "api_key_encrypted",
            "api_secret_encrypted",
            "pf_id",
            "template_id",
            "from_number",
        )
    )
    configured_from_env = any(environment.values())
    result["source"] = (
        "database" if configured_from_db else "environment" if configured_from_env else "none"
    )
    result["configured"] = all(
        result.get(key)
        for key in ("api_key", "api_secret", "pf_id", "template_id", "from_number")
    )
    # 설문조사 URL 문자는 알림톡 템플릿 없이 API 키·시크릿·발신번호만으로 보낸다.
    result["sms_configured"] = all(
        result.get(key) for key in ("api_key", "api_secret", "from_number")
    )
    return result


def settings_for_view() -> dict[str, Any]:
    settings = get_settings()
    api_key = str(settings.get("api_key") or "")
    return {
        "api_key_masked": f"{'*' * max(4, len(api_key) - 4)}{api_key[-4:]}" if api_key else "",
        "has_api_key": bool(api_key),
        "has_api_secret": bool(settings.get("api_secret")),
        "pf_id": settings.get("pf_id", ""),
        "template_id": settings.get("template_id", ""),
        "from_number": settings.get("from_number", ""),
        "public_origin": settings.get("public_origin", ""),
        "sender_name": settings.get("sender_name", ""),
        "updated_by": settings.get("updated_by", ""),
        "updated_at": settings.get("updated_at", ""),
        "source": settings.get("source", "none"),
        "configured": bool(settings.get("configured")),
        "sms_configured": bool(settings.get("sms_configured")),
    }


def save_settings(
    *,
    api_key: object,
    api_secret: object,
    pf_id: object,
    template_id: object,
    from_number: object,
    actor: object,
    public_origin: object = None,
    sender_name: object = None,
    clear_credentials: bool = False,
) -> None:
    api_key_text = str(api_key or "").strip()
    api_secret_text = str(api_secret or "").strip()
    pf_id_text = str(pf_id or "").strip()
    template_id_text = str(template_id or "").strip()
    from_digits = re.sub(r"\D", "", str(from_number or ""))
    origin_text = normalize_origin(public_origin) if public_origin is not None else None
    sender_text = (
        str(sender_name).strip()[:30] if sender_name is not None else None
    )

    # 알림톡 항목은 문자 전용으로만 쓰는 설치본을 위해 선택 입력으로 둔다.
    for label, value, maximum in (
        ("SOLAPI PF ID", pf_id_text, 120),
        ("SOLAPI 템플릿 ID", template_id_text, 120),
    ):
        if value and (len(value) > maximum or re.search(r"\s", value)):
            raise ValueError(f"{label} 값을 확인해 주세요.")
    if not re.fullmatch(r"\d{8,12}", from_digits):
        raise ValueError("회사 발신번호는 지역번호를 포함한 숫자 8~12자리로 입력해 주세요.")
    if api_key_text and (len(api_key_text) > 500 or re.search(r"\s", api_key_text)):
        raise ValueError("SOLAPI API KEY 형식을 확인해 주세요.")
    if api_secret_text and (len(api_secret_text) > 500 or re.search(r"\s", api_secret_text)):
        raise ValueError("SOLAPI API SECRET 형식을 확인해 주세요.")

    conn = get_db()
    try:
        store = _load_store(conn)
        environment = _environment_settings()
        stored_api_key = (
            _decrypt(store.get("api_key_encrypted"))
            if store.get("api_key_encrypted") and not clear_credentials
            else ""
        )
        stored_api_secret = (
            _decrypt(store.get("api_secret_encrypted"))
            if store.get("api_secret_encrypted") and not clear_credentials
            else ""
        )
        if not (api_key_text or stored_api_key or environment["api_key"]):
            raise ValueError("SOLAPI API KEY를 입력해 주세요.")
        if not (api_secret_text or stored_api_secret or environment["api_secret"]):
            raise ValueError("SOLAPI API SECRET을 입력해 주세요.")
        if clear_credentials:
            store["api_key_encrypted"] = ""
            store["api_secret_encrypted"] = ""
        if api_key_text:
            store["api_key_encrypted"] = _encrypt(api_key_text)
        if api_secret_text:
            store["api_secret_encrypted"] = _encrypt(api_secret_text)
        store.update(
            {
                "pf_id": pf_id_text,
                "template_id": template_id_text,
                "from_number": from_digits,
                "updated_by": str(actor or "admin")[:100],
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        if origin_text is not None:
            store["public_origin"] = origin_text
        if sender_text is not None:
            store["sender_name"] = sender_text
        conn.execute(
            """
            INSERT INTO admin_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (SETTINGS_KEY, json.dumps(store, ensure_ascii=False)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def csrf_token(session_store) -> str:
    token = session_store.get("solapi_settings_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session_store["solapi_settings_csrf"] = token
    return str(token)


def valid_csrf(session_store, supplied: object) -> bool:
    expected = str(session_store.get("solapi_settings_csrf") or "")
    received = str(supplied or "")
    return bool(expected and received and hmac.compare_digest(expected, received))


def _require_complete(settings: dict[str, Any], *, kakao: bool) -> None:
    required = ["api_key", "api_secret", "from_number"]
    if kakao:
        required.extend(["pf_id", "template_id"])
    if not all(settings.get(key) for key in required):
        destination = "알림톡" if kakao else "문자"
        raise RuntimeError(f"SOLAPI {destination} 발송 설정이 완료되지 않았습니다.")


def _authorization(settings: dict[str, Any]) -> str:
    date_text = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    salt = secrets.token_hex(16)
    signature = hmac.new(
        str(settings["api_secret"]).encode("utf-8"),
        f"{date_text}{salt}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"HMAC-SHA256 apiKey={settings['api_key']}, date={date_text}, "
        f"salt={salt}, signature={signature}"
    )


def _send_message(message: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        {
            "messages": [message],
            "strict": True,
            "allowDuplicates": True,
            "showMessageList": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request_object = Request(
        SOLAPI_SEND_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": _authorization(settings),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Saedam-Intranet/1.0",
        },
    )
    try:
        with urlopen(request_object, timeout=20) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            failures = detail.get("failedMessageList") or []
            first = failures[0] if failures else {}
            reason = first.get("statusMessage") or detail.get("errorMessage") or detail.get("message") or "요청 거절"
            if first.get("statusCode"):
                reason = f"[{first['statusCode']}] {reason}"
        except Exception:
            reason = str(exc)
        raise RuntimeError(f"SOLAPI 요청 실패: {reason}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"SOLAPI 서버 연결 실패: {exc}") from exc

    return _parse_send_result(result)


def _parse_send_result(result: object) -> dict[str, str]:
    """send-many/detail 접수 결과를 판별한다. 접수 성공은 수신 완료와 다르다."""
    if not isinstance(result, dict):
        raise RuntimeError("SOLAPI 응답 형식을 확인할 수 없습니다.")
    group = result.get("groupInfo") or {}
    counts = group.get("count") or {}
    failures = result.get("failedMessageList") or []
    messages = result.get("messageList") or []
    first = messages[0] if isinstance(messages, list) and messages else {}
    failed = failures[0] if isinstance(failures, list) and failures else {}
    code = str(first.get("statusCode") or "")
    if failures or counts.get("registeredFailed") or counts.get("sentFailed") or group.get("status") == "FAILED" or result.get("errorCode") or (code and code not in {"2000", "4000"}):
        item = failed or first
        reason = item.get("statusMessage") or item.get("reason") or result.get("errorMessage") or "발송 접수 실패"
        error_code = str(item.get("statusCode") or result.get("errorCode") or "")
        raise RuntimeError(f"SOLAPI 발송 실패 [{error_code or 'UNKNOWN'}]: {reason}")
    # showMessageList=True이므로 성공 메시지 ID가 없으면 성공으로 기록하지 않는다.
    if not first.get("messageId") or code not in {"2000", "4000"}:
        raise RuntimeError("SOLAPI 발송 접수를 확인할 수 없습니다. 솔라피 발송 내역을 확인해 주세요.")
    return {
        "message_id": str(first["messageId"]),
        "group_id": str(group.get("groupId") or ""),
        "status_code": code,
    }


def send_alimtalk(
    to: object,
    *,
    signer_name: object,
    invitation_url: object,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active = settings or get_settings()
    _require_complete(active, kakao=True)
    phone = normalize_phone(to, required=True)
    name = str(signer_name or "").strip()
    if not name:
        raise ValueError("알림톡에 사용할 계약자 이름이 없습니다.")
    link = str(invitation_url or "").strip()
    parsed = urlsplit(link)
    if (parsed.scheme != "https" or parsed.netloc != "works.saedam.org"
            or not parsed.path.startswith("/verified-contract/sign/")
            or not parsed.path.removeprefix("/verified-contract/sign/")
            or re.search(r"\s", link) or parsed.query or parsed.fragment):
        raise ValueError("계약 링크는 https://works.saedam.org의 인증전자계약 주소여야 합니다.")
    # 승인 버튼 주소는 https://#{url}. 변수에는 스킴을 제외해야 한다.
    # 본문·강조문구·버튼(targetOut 포함)은 SOLAPI의 승인 템플릿을 그대로 사용한다.
    # 대체발송 문구는 템플릿에 저장된 값(제목이 빈 문자열)을 쓰면 안 된다. 계약 링크가 붙어
    # 90바이트를 넘는 순간 LMS로 전환되는데, 제목이 비어 있으면 1010(필수 입력 값 미입력)으로
    # 접수 자체가 거절된다. 발송할 때마다 제목이 있는 대체문자를 직접 실어 보낸다.
    return _send_message(
        {
            "to": phone,
            "from": active["from_number"],
            "type": "ATA",
            "kakaoOptions": {
                "pfId": active["pf_id"],
                "templateId": active["template_id"],
                "disableSms": False,
                "variables": {
                    "#{이름}": name,
                    "#{url}": link.removeprefix("https://"),
                },
            },
            "replacements": [
                {
                    "from": active["from_number"],
                    "subject": ALIMTALK_FALLBACK_SUBJECT,
                    "text": (
                        f"[새담 인증전자계약] {name}님, 전자계약서가 도착했습니다.\n"
                        f"아래 주소에서 계약을 진행해 주세요.\n{link}"
                    ),
                }
            ],
        },
        active,
    )


def send_sms_otp(
    to: object,
    *,
    signer_name: object,
    code: object,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active = settings or get_settings()
    _require_complete(active, kakao=False)
    phone = normalize_phone(to, required=True)
    return _send_message(
        {
            "to": phone,
            "from": active["from_number"],
            "type": "SMS",
            "text": (
                f"[새담 인증전자계약] {str(signer_name or '계약자').strip()}님 "
                f"인증번호는 {str(code)}입니다. 5분 안에 입력해 주세요."
            ),
            "autoTypeDetect": False,
        },
        active,
    )


def resolve_public_origin(settings: dict[str, Any] | None = None) -> str:
    """설문 링크 등 외부 공개 주소의 기준이 되는 origin을 반환한다."""
    active = settings or get_settings()
    return str(active.get("public_origin") or CONTRACT_PUBLIC_ORIGIN).rstrip("/")


def message_byte_length(text: object) -> int:
    """SOLAPI가 SMS/LMS를 나누는 기준인 EUC-KR 바이트 길이를 계산한다."""
    body = str(text or "")
    try:
        return len(body.encode("euc-kr"))
    except UnicodeEncodeError:
        # EUC-KR로 표현할 수 없는 글자(이모지 등)가 있으면 LMS로 보내야 한다.
        return SMS_BYTE_LIMIT + 1


def _send_many(messages: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    """여러 건을 한 번에 접수한다. strict=False라 일부 실패해도 나머지는 접수된다."""
    payload = json.dumps(
        {
            "messages": messages,
            "strict": False,
            "allowDuplicates": True,
            "showMessageList": True,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request_object = Request(
        SOLAPI_SEND_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": _authorization(settings),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Saedam-Intranet/1.0",
        },
    )
    try:
        with urlopen(request_object, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            reason = (
                detail.get("errorMessage") or detail.get("message") or "요청 거절"
            )
        except Exception:
            reason = str(exc)
        raise RuntimeError(f"SOLAPI 요청 실패: {reason}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError(f"SOLAPI 서버 연결 실패: {exc}") from exc


def _entry_phone(item: object) -> str:
    """응답 항목에서 수신번호를 뽑는다. 문자열(접수번호)만 오는 응답도 있다."""
    if isinstance(item, dict):
        return re.sub(r"\D", "", str(item.get("to") or ""))
    return ""


def _entry_message_id(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(item.get("messageId") or "")
    return ""


def _entry_failure_reason(item: object) -> str:
    if not isinstance(item, dict):
        return "발송 접수 실패"
    code = str(item.get("statusCode") or "")
    reason = (
        item.get("statusMessage")
        or item.get("reason")
        or item.get("errorMessage")
        or "발송 접수 실패"
    )
    return f"[{code or 'UNKNOWN'}] {reason}"


def _apply_bulk_result(chunk: list[dict[str, Any]], result: object) -> None:
    """send-many 응답을 수신자 행에 반영한다.

    strict=False로 접수하면 SOLAPI는 거절한 건만 failedMessageList에 담아 돌려준다.
    따라서 이 목록에 없는 번호는 접수된 것으로 판정해야 한다. messageList는 접수번호를
    붙이는 용도로만 쓰며, 항목이 객체든 접수번호 문자열이든, to가 있든 없든 견딘다.
    (예전 구현은 messageList의 to로만 성공을 확인해서, to를 돌려주지 않는 응답에서는
     실제로 발송된 문자까지 전부 실패로 기록했다.)
    """
    payload = result if isinstance(result, dict) else {}
    group = payload.get("groupInfo") or {}
    if payload.get("errorCode") or str(group.get("status") or "") == "FAILED":
        reason = str(
            payload.get("errorMessage") or payload.get("message") or "그룹 발송 실패"
        )
        code = str(payload.get("errorCode") or "GROUP_FAILED")
        for row in chunk:
            row["error"] = f"[{code}] {reason}"
        return

    failed_by_phone: dict[str, object] = {}
    floating_failures: list[object] = []
    for item in payload.get("failedMessageList") or []:
        phone = _entry_phone(item)
        if phone:
            failed_by_phone[phone] = item
        else:
            floating_failures.append(item)

    sent_by_phone: dict[str, object] = {}
    floating_sent: list[object] = []
    for item in payload.get("messageList") or []:
        code = str(item.get("statusCode") or "") if isinstance(item, dict) else ""
        phone = _entry_phone(item)
        if code and code not in {"2000", "4000"}:
            if phone:
                failed_by_phone.setdefault(phone, item)
            else:
                floating_failures.append(item)
            continue
        if phone:
            sent_by_phone[phone] = item
        else:
            floating_sent.append(item)

    unresolved: list[dict[str, Any]] = []
    for row in chunk:
        phone = row["phone"]
        if phone in failed_by_phone:
            row["error"] = _entry_failure_reason(failed_by_phone[phone])
        elif phone in sent_by_phone:
            row["ok"] = True
            row["message_id"] = _entry_message_id(sent_by_phone[phone])
        else:
            unresolved.append(row)

    # 번호를 알 수 없는 실패 건수가 남은 행 수와 정확히 맞을 때만 실패로 돌린다.
    if floating_failures and len(floating_failures) == len(unresolved):
        for row, item in zip(unresolved, floating_failures):
            row["error"] = _entry_failure_reason(item)
        return
    for row in unresolved:
        row["ok"] = True
        row["message_id"] = _entry_message_id(floating_sent.pop(0)) if floating_sent else ""


def send_bulk_text(
    recipients: list[dict[str, Any]],
    *,
    settings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """문자(SMS/LMS)를 여러 명에게 보내고 수신자별 접수 결과를 그대로 돌려준다.

    recipients 각 항목은 {'to': 휴대폰번호, 'text': 본문, 'subject': 제목(선택)} 형태이며,
    반환값은 입력 순서를 유지한 {'to', 'ok', 'message_id', 'error'} 목록이다.
    """
    active = settings or get_settings()
    _require_complete(active, kakao=False)

    prepared: list[dict[str, Any]] = []
    for entry in recipients:
        row: dict[str, Any] = {
            "to": str((entry or {}).get("to") or ""),
            "ok": False,
            "message_id": "",
            "error": "",
            "phone": "",
        }
        try:
            row["phone"] = normalize_phone(row["to"], required=True)
        except ValueError as exc:
            row["error"] = str(exc)
            prepared.append(row)
            continue
        body = str((entry or {}).get("text") or "").strip()
        if not body:
            row["error"] = "발송할 문자 내용이 없습니다."
            prepared.append(row)
            continue
        subject = str((entry or {}).get("subject") or "").strip()
        if message_byte_length(body) > SMS_BYTE_LIMIT:
            row["payload"] = {
                "to": row["phone"],
                "from": active["from_number"],
                "type": "LMS",
                "subject": (subject or DEFAULT_SENDER_NAME)[:40],
                "text": body,
                "autoTypeDetect": False,
            }
        else:
            row["payload"] = {
                "to": row["phone"],
                "from": active["from_number"],
                "type": "SMS",
                "text": body,
                "autoTypeDetect": False,
            }
        prepared.append(row)

    sendable = [row for row in prepared if row.get("payload")]
    for start in range(0, len(sendable), BULK_CHUNK_SIZE):
        chunk = sendable[start:start + BULK_CHUNK_SIZE]
        try:
            result = _send_many([row["payload"] for row in chunk], active)
        except RuntimeError as exc:
            for row in chunk:
                row["error"] = str(exc)
            continue
        _apply_bulk_result(chunk, result)

    for row in prepared:
        row.pop("payload", None)
    return prepared
