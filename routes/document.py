from flask import Blueprint, render_template, request, jsonify, send_file
from .db_handler import read_excel_db, write_excel_db, EXCEL_FILE, OWNER_FILE, ATTEND_FILE
from datetime import datetime
import os
import pandas as pd

document_bp = Blueprint('document', __name__)

# 관리자 암호 (기존 설정 유지)
ADMIN_PASSWORD = "1900" 

@document_bp.route('/')
def index():
    try:
        # 실제 파일명인 document.html로 연결 (하위 폴더 경로 제거)
        return render_template('document.html')
    except Exception as e:
        return f"템플릿 에러: {str(e)}", 500

# --- 사용자/관리자 관련 로직 ---
@document_bp.route('/get_owners')
def get_owners():
    try:
        df = read_excel_db(OWNER_FILE)
        return jsonify(df.to_dict(orient='records') if not df.empty else [])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@document_bp.route('/add_owner', methods=['POST'])
def add_owner():
    try:
        data = request.json
        if str(data.get('admin_pass')) != ADMIN_PASSWORD:
            return jsonify({"status": "error", "message": "관리자 암호 불일치"}), 403
        
        df = read_excel_db(OWNER_FILE)
        if not df.empty and data['name'] in df['이름'].values:
            idx = df[df['이름'] == data['name']].index[0]
            df.at[idx, '직책'] = data.get('position', '담당자')
            df.at[idx, '암호'] = str(data['owner_pass'])
            msg = "사용자 정보가 수정되었습니다."
        else:
            new_row = pd.DataFrame([{
                '이름': data['name'], 
                '암호': str(data['owner_pass']), 
                '직책': data.get('position', '담당자')
            }])
            df = pd.concat([df, new_row], ignore_index=True)
            msg = "신규 인원이 등록되었습니다."
            
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 근태 신청/승인 로직 ---
@document_bp.route('/submit_attendance', methods=['POST'])
def submit_attendance():
    try:
        data = request.json
        owners_df = read_excel_db(OWNER_FILE)
        user = owners_df[(owners_df['이름'] == data['owner']) & (owners_df['암호'].astype(str) == str(data['password']))]
        
        if user.empty:
            return jsonify({"status": "error", "message": "비밀번호가 틀렸습니다."}), 403

        df = read_excel_db(ATTEND_FILE)
        new_row = pd.DataFrame([{
            '신청일': datetime.now().strftime('%Y-%m-%d'),
            '담당자': data['owner'],
            '구분': data['type'],
            '시작일': data['start_date'],
            '종료일': data['end_date'],
            '사유': data['reason'],
            '승인상태': '대기'
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        write_excel_db(df, ATTEND_FILE)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@document_bp.route('/approve_attendance', methods=['POST'])
def approve_attendance():
    try:
        data = request.json
        owners_df = read_excel_db(OWNER_FILE)
        admin = owners_df[(owners_df['이름'] == data['admin_name']) & (owners_df['암호'].astype(str) == str(data['admin_password']))]
        
        if admin.empty or admin.iloc[0]['직책'] not in ['이사', '대표이사']:
            return jsonify({"status": "error", "message": "승인 권한이 없습니다 (이사 이상 가능)."}), 403

        df = read_excel_db(ATTEND_FILE)
        idx = int(data['idx'])
        df.at[idx, '승인상태'] = data['status']
        write_excel_db(df, ATTEND_FILE)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@document_bp.route('/download')
def download():
    try:
        if os.path.exists(EXCEL_FILE):
            return send_file(EXCEL_FILE, as_attachment=True)
        return "파일 없음", 404
    except Exception as e:
        return f"다운로드 에러: {str(e)}", 500