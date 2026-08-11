from flask_socketio import SocketIO


# 메신저와 업무알림이 함께 사용하는 단일 SocketIO 객체.
# 여러 서버 프로세스로 확장할 때는 Redis 등의 message_queue를 추가한다.
socketio = SocketIO(
    async_mode="threading",
    cors_allowed_origins=None,
    manage_session=False,
    ping_interval=25,
    ping_timeout=20,
    # Windows의 Werkzeug 개발 서버에서는 WebSocket 연결 종료 시
    # ``write() before start_response``가 반복될 수 있다. 메신저와 알림은
    # Socket.IO long-polling으로도 동일하게 동작하므로 안정적인 전송만 허용한다.
    transports=["polling"],
    allow_upgrades=False,
)
