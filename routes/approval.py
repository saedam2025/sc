from flask import Blueprint, render_template

approval_bp = Blueprint('approval', __name__)

@approval_bp.route('/')
def index():
    try:
        # 실제 파일명인 approval.html로 연결
        return render_template('approval.html')
    except Exception as e:
        return f"템플릿 에러: {str(e)}", 500