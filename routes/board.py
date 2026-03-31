from flask import Blueprint, render_template

board_bp = Blueprint('board', __name__)

@board_bp.route('/')
def index():
    return render_template('board/index.html')