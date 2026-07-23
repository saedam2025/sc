from flask_socketio import SocketIO


# 사내망의 단일 Gunicorn 프로세스에서도 동작하는 스레드 기반 WebSocket 구성.
# 서버를 여러 프로세스/인스턴스로 확장할 때는 message_queue(예: Redis)를 추가한다.
socketio = SocketIO(
    async_mode="threading",
    cors_allowed_origins=None,
    manage_session=False,
    ping_interval=25,
    ping_timeout=20,
)
