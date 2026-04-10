from flask import Blueprint, render_template

# 'noti_widget'이라는 이름의 블루프린트 생성
noti_bp = Blueprint('noti_bp', __name__, url_prefix='/widget')

@noti_bp.route('/notifications')
def get_notification_widget():
    # 실제 환경에서는 DB에서 사용자의 알림 데이터를 조회해옵니다.
    # 예시를 위해 하드코딩된 더미 데이터를 사용합니다.
    noti_data = {
        'approval_vacation': 2,
        'approval_draft': 1,
        'cert_wait': 4,
        'contract_miss': 3,
        'board_new': 1,
        'message_new': 5
    }
    
    # 위젯의 HTML 조각만 렌더링하여 반환합니다.
    return render_template('widgets/noti_widget.html', data=noti_data)