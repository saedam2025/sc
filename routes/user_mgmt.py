from flask import Blueprint, render_template, request, jsonify
from .db_handler import read_excel_db, write_excel_db, OWNER_FILE

user_mgmt_bp = Blueprint('user_mgmt', __name__)

@user_mgmt_bp.route('/')
def index():
    return render_template('user_mgmt/index.html')

@user_mgmt_bp.route('/list')
def get_users():
    df = read_excel_db(OWNER_FILE)
    return jsonify(df.to_dict(orient='records') if not df.empty else [])