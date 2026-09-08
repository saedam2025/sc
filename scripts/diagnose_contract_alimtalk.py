"""Read-only SOLAPI diagnostics; never send a message or print credentials/recipients."""

import json
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from routes.solapi_settings import get_settings, _authorization
from routes.solapi_settings import send_alimtalk


def main():
    settings = get_settings()
    print(json.dumps({"configured": settings["configured"], "source": settings["source"]}))
    if not settings["configured"]:
        return

    def get(path, params):
        req = Request("https://api.solapi.com" + path + "?" + urlencode(params),
                      headers={"Authorization": _authorization(settings)}, method="GET")
        with urlopen(req, timeout=20) as response:
            return json.load(response)

    template = get("/kakao/v2/templates/", {"templateId": settings["template_id"]})
    if "--template-check" in sys.argv:
        items = template.get("templateList", [])
        if len(items) != 1:
            raise RuntimeError("설정한 템플릿을 찾을 수 없습니다.")
        approved = items[0]
        link = "https://works.saedam.org/verified-contract/sign/diagnostic-not-a-real-contract"
        with patch("routes.solapi_settings._send_message", return_value={}) as send:
            send_alimtalk("01012345678", signer_name="테스트계약자", invitation_url=link, settings=settings)
        message = send.call_args.args[0]
        variables = message["kakaoOptions"]["variables"]

        def replace(value):
            for key, text in variables.items():
                value = value.replace(key, text)
            return value

        buttons = approved.get("buttons") or []
        checks = {
            "approved": approved.get("status") == "APPROVED",
            "name_resolved": replace(approved.get("content", "")).startswith("테스트계약자님"),
            "no_body_placeholders": "#{" not in replace(approved.get("content", "")),
            "mobile_link_matches": bool(buttons) and replace(buttons[0].get("linkMo", "")) == link,
            "pc_link_matches": bool(buttons) and replace(buttons[0].get("linkPc", "")) == link,
            "template_fields_preserved": "text" not in message and "buttons" not in message["kakaoOptions"],
        }
        print(json.dumps({"live_template_checks": checks, "messages_sent": 0}, ensure_ascii=False))
        if not all(checks.values()):
            raise RuntimeError("승인 템플릿과 발송 변수가 일치하지 않습니다.")
        return
    for item in template.get("templateList", []):
        print(json.dumps({"template": {k: item.get(k) for k in (
            "templateId", "name", "status", "content", "emphasizeType", "emphasizeTitle",
            "emphasizeSubtitle", "buttons", "variables")}}, ensure_ascii=False))

    data = get("/messages/v4/list", {"limit": 10})
    for msg in data.get("messageList", {}).values():
        options = msg.get("kakaoOptions") or {}
        if options.get("templateId") != settings["template_id"]:
            continue
        buttons = []
        for button in options.get("buttons") or []:
            summary = {k: button.get(k) for k in ("buttonName", "buttonType", "targetOut")}
            for field in ("linkMo", "linkPc"):
                link = str(button.get(field) or "")
                parts = urlsplit(link)
                summary[field] = {"scheme": parts.scheme, "host": parts.netloc,
                                  "has_placeholder": "#{" in link,
                                  "duplicate_scheme": "://" in link.split("://", 1)[-1],
                                  "present": bool(link)}
            buttons.append(summary)
        print(json.dumps({"message": {
            "created": msg.get("dateCreated"), "statusCode": msg.get("statusCode"),
            "reason": msg.get("reason"), "type": msg.get("type"),
            "text_present": bool(msg.get("text")), "title": options.get("title"),
            "buttons": buttons,
        }}, ensure_ascii=False))


if __name__ == "__main__":
    main()
