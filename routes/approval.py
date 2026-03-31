from flask import Blueprint, render_template

approval_bp = Blueprint('approval', __name__)

@approval_bp.route('/')
def list_approval():
    return render_template('approval/list.html')