from flask import Blueprint, render_template, request, jsonify
from .db_handler import read_excel_db, write_excel_db, EXCEL_FILE

contract_bp = Blueprint('contract', __name__)

@contract_bp.route('/')
def index():
    # 전자계약 메인 화면
    return render_template('contract/index.html')

@contract_bp.route('/save_task', methods=['POST'])
def save_task():
    # 기존 app_c.py의 업무 저장 로직 통합
    data = request.json
    try:
        df = read_excel_db(EXCEL_FILE)
        import pandas as pd
        new_row = pd.DataFrame([{
            '연도': str(data['date'])[:4], '날짜': str(data['date']), '담당자': str(data['owner']),
            '내근업무': str(data['inside']), '외근업무': str(data['outside']),
            '회의': str(data['meeting']), '비고': str(data['note']), '기타': str(data.get('etc', ''))
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        write_excel_db(df, EXCEL_FILE)
        return jsonify({"status": "success"})
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500