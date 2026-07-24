import sqlite3
import os
import re
import subprocess
import sys

from flask import Flask, render_template

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import routes.chat as chat


URI = "file:chat_upgrade_test?mode=memory&cache=shared"


def connection():
    conn = sqlite3.connect(URI, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


keeper = connection()
keeper.executescript(
    """
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id TEXT,
        sender TEXT,
        receiver TEXT,
        content TEXT,
        sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_read INTEGER DEFAULT 0,
        filename TEXT,
        filepath TEXT
    );
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        status TEXT,
        profile_icon TEXT,
        level INTEGER
    );
    INSERT INTO users(name, status, level) VALUES
        ('Alice', '승인', 1),
        ('Bob', '승인', 2),
        ('Carol', '승인', 3);
    """
)
keeper.commit()
chat.get_db = connection
chat._ensure_chat_tables(keeper)

columns = {row["name"] for row in keeper.execute("PRAGMA table_info(messages)")}
assert {"message_uid", "reply_to_uid", "edited_at", "deleted_for_all", "deleted_at"} <= columns

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__, template_folder=os.path.join(PROJECT_ROOT, "templates"))
app.secret_key = "test"
app.register_blueprint(chat.chat_bp)
chat.socketio.init_app(app)
client = app.test_client()


def login(name):
    with client.session_transaction() as session:
        session["user_name"] = name
        session["emp_no"] = name


login("Alice")
response = client.post("/send_message", data={"receiver": "Bob", "content": "hello"})
assert response.status_code == 200, response.get_json()
first_id = response.get_json()["message_id"]

login("Bob")
history = client.get("/get_chat_history/Alice?limit=50").get_json()
assert history["messages"][0]["content"] == "hello"
response = client.post(
    "/send_message",
    data={"receiver": "Alice", "content": "reply", "reply_to_id": str(first_id)},
)
assert response.status_code == 200, response.get_json()

login("Alice")
history = client.get("/get_chat_history/Bob?limit=50").get_json()
assert history["messages"][-1]["reply"]["content"] == "hello"
response = client.patch(f"/api/messages/{first_id}", json={"content": "hello edited"})
assert response.status_code == 200, response.get_json()
search = client.get("/api/chat/search?partner=Bob&q=edited").get_json()
assert search["results"][0]["id"] == first_id

login("Bob")
response = client.delete(f"/delete_message/{first_id}?mode=me")
assert response.status_code == 200, response.get_json()
history = client.get("/get_chat_history/Alice").get_json()
assert all(item["id"] != first_id for item in history["messages"])

login("Alice")
response = client.delete(f"/delete_message/{first_id}?mode=all")
assert response.status_code == 200, response.get_json()
history = client.get("/get_chat_history/Bob").get_json()
assert any(item["id"] == first_id and item["is_deleted"] for item in history["messages"])

message_count = keeper.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
login("Bob")
response = client.post("/api/leave_chat", json={"partner": "Alice"})
assert response.status_code == 200 and response.get_json()["history_deleted"] is False
assert keeper.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"] == message_count
rooms = client.get("/api/unread_messages").get_json()["rooms"]
assert all(room["partner"] != "Alice" for room in rooms)

login("Alice")
client.post("/send_message", data={"receiver": "Bob", "content": "come back"})
login("Bob")
rooms = client.get("/api/unread_messages").get_json()["rooms"]
assert any(room["partner"] == "Alice" for room in rooms)
response = client.post(
    "/api/chat/room/mute",
    json={"partner": "Alice", "muted": True},
)
assert response.status_code == 200 and response.get_json()["muted"] is True
room = client.get("/api/chat/room?partner=Alice").get_json()["room"]
assert room["notifications_muted"] is True
rooms = client.get("/api/unread_messages").get_json()["rooms"]
assert next(item for item in rooms if item["partner"] == "Alice")["notifications_muted"] is True
assert client.post(
    "/api/chat/room/mute",
    json={"partner": "Alice", "muted": "false"},
).status_code == 400
response = client.post(
    "/api/chat/room/mute",
    json={"partner": "Alice", "muted": False},
)
assert response.status_code == 200 and response.get_json()["muted"] is False

login("Alice")
group_key = "Alice,Bob,Carol"
response = client.post(
    "/send_message",
    data={"room_id": group_key, "content": "group hello"},
)
assert response.status_code == 200, response.get_json()
room = client.get(f"/api/chat/room?partner={group_key}").get_json()["room"]
assert room["is_admin"] and room["member_count"] == 3
response = client.post(
    "/api/chat/room/name",
    json={"partner": group_key, "display_name": "프로젝트방"},
)
assert response.status_code == 200
response = client.post(
    "/api/chat/room/admin",
    json={"partner": group_key, "admin_user": "Bob"},
)
assert response.status_code == 200

login("Bob")
response = client.post(
    "/api/chat/room/remove-member",
    json={"partner": group_key, "member": "Carol"},
)
assert response.status_code == 200, response.get_json()
login("Carol")
assert client.get(f"/get_chat_history/{group_key}").status_code == 403

login("Alice")
for index in range(55):
    response = client.post(
        "/send_message",
        data={"receiver": "Bob", "content": f"page-{index:02d}"},
    )
    assert response.status_code == 200
history = client.get("/get_chat_history/Bob?limit=50").get_json()
assert len(history["messages"]) == 50 and history["has_more"]
older = client.get(
    f"/get_chat_history/Bob?limit=50&before_id={history['oldest_id']}"
).get_json()
assert older["messages"]

socket_client = chat.socketio.test_client(
    app, namespace="/chat", flask_test_client=client
)
assert socket_client.is_connected("/chat")
ack = socket_client.emit(
    "join_chat",
    {"partner": "Bob"},
    namespace="/chat",
    callback=True,
)
assert ack["status"] == "success"

bob_http_client = app.test_client()
with bob_http_client.session_transaction() as bob_session:
    bob_session["user_name"] = "Bob"
    bob_session["emp_no"] = "Bob"
bob_socket = chat.socketio.test_client(
    app, namespace="/chat", flask_test_client=bob_http_client
)
assert bob_socket.is_connected("/chat")
bob_ack = bob_socket.emit(
    "join_chat",
    {"partner": "Alice"},
    namespace="/chat",
    callback=True,
)
assert bob_ack["status"] == "success"
socket_client.emit(
    "typing",
    {"partner": "Bob", "is_typing": True},
    namespace="/chat",
)
typing_events = bob_socket.get_received("/chat")
assert any(event["name"] == "chat_typing" for event in typing_events)

response = client.post(
    "/send_message",
    data={"receiver": "Bob", "content": "realtime"},
)
assert response.status_code == 200
message_events = bob_socket.get_received("/chat")
assert any(
    event["name"] == "chat_event"
    and event["args"][0]["type"] == "message"
    for event in message_events
)
bob_socket.disconnect(namespace="/chat")
socket_client.disconnect(namespace="/chat")

with app.test_request_context("/"):
    popup_html = render_template(
        "chat_popup.html", partner="Alice,Bob", current_user="Alice"
    )
    assert "loadChatMessages" in popup_html and "notifyIncomingChatMessage" in popup_html
    widget_html = render_template(
        "chat_widget.html",
        current_user="Alice",
        chat_user_list=[],
        chat_user_icons={},
        widget_recv_msgs=[],
        widget_sent_msgs=[],
        received_messages=[],
        user_icons={},
    )
    assert "initializeChatWidgetSocket" in widget_html
    assert "handleIncomingChatAlert" in widget_html and "toggleChatAlerts" in widget_html
    node_executable = os.environ.get("CHAT_TEST_NODE")
    if node_executable:
        subprocess.run(
            [
                node_executable,
                "--check",
                os.path.join(PROJECT_ROOT, "static", "js", "chat_notifications.js"),
            ],
            check=True,
        )
        for rendered_html in (popup_html, widget_html):
            source = "\n".join(
                match.group(1)
                for match in re.finditer(
                    r"<script(?![^>]*src)[^>]*>([\s\S]*?)</script>",
                    rendered_html,
                )
            )
            subprocess.run(
                [
                    node_executable,
                    "-e",
                    "new Function(require('fs').readFileSync(0, 'utf8'));",
                ],
                input=source.encode("utf-8"),
                check=True,
            )

print("chat upgrade integration tests OK")
keeper.close()
