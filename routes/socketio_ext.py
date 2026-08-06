from flask_socketio import SocketIO


# 메신저와 업무알림이 함께 사용하는 단일 SocketIO 객체.
# 여러 서버 프로세스로 확장할 때는 Redis 등의 message_queue를 추가한다.
socketio = SocketIO(
    async_mode="threading",
    cors_allowed_origins=None,
    manage_session=False,
    ping_interval=25,
    ping_timeout=20,
)
