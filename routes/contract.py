from flask import Blueprint, render_template, request, jsonify
from .db_handler import read_excel_db, write_excel_db, EXCEL_FILE
import pandas as pd

contract_bp = Blueprint('contract', __name__)

@contract_bp.route('/')
def index():
    try:
        # 실제 파일명인 contract.html로 연결 (기존 contract/index.html에서 수정)
        return render_template('contract.html')
    except Exception as e:
        return f"템플릿 에러: {str(e)}", 500

@contract_bp.route('/save_task', methods=['POST'])
def save_task():
    data = request.json
    try:
        df = read_excel_db(EXCEL_FILE)
        new_row = pd.DataFrame([{
            '연도': str(data['date'])[:4], 
            '날짜': str(data['date']), 
            '담당자': str(data['owner']),
            '내근업무': str(data['inside']), 
            '외근업무': str(data['outside']),
            '회의': str(data['meeting']), 
            '비고': str(data['note']), 
            '기타': str(data.get('etc', ''))
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        write_excel_db(df, EXCEL_FILE)
        return jsonify({"status": "success"})
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500