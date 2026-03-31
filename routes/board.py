from flask import Blueprint, render_template

board_bp = Blueprint('board', __name__)

@board_bp.route('/')
def index():
    try:
        # 게시판 메인으로 index.html 연결
        return render_template('index.html')
    except Exception as e:
        return f"템플릿 에러: {str(e)}", 500