from flask import Blueprint, render_template, request, jsonify
from .db_handler import read_excel_db, write_excel_db, OWNER_FILE
import pandas as pd

user_mgmt_bp = Blueprint('user_mgmt', __name__)

# 직급별 권한 레벨 정의
LEVEL_MAP = {
    "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "센터장": 6, "전담코디": 7, "안전코디": 8, "계약직": 9, "임시회원": 10
}

@user_mgmt_bp.route('/')
def index():
    try:
        return render_template('user_mgmt/user_list.html')
    except Exception as e:
        return f"템플릿 에러: {str(e)}", 500

# 공통 인증 로직 (슈퍼바이저 또는 레벨 2 이하 관리자)
def verify_admin(admin_pass):
    # 1. 슈퍼바이저 체크
    if str(admin_pass) == "1900":
        return True, "admin"
    
    # 2. DB 내 관리자 체크 (이사 이상)
    df = read_excel_db(OWNER_FILE)
    if not df.empty:
        # 암호가 일치하고 레벨이 2(이사) 이하인 사람 검색
        admin = df[(df['암호'].astype(str) == str(admin_pass)) & (df['레벨'] <= 2)]
        if not admin.empty:
            return True, admin.iloc[0]['이름']
            
    return False, None

@user_mgmt_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        
        if not df.empty and data['name'] in df['이름'].values:
            return jsonify({"status": "error", "message": "이미 등록된 이름입니다."}), 400

        new_user = pd.DataFrame([{
            '이름': data['name'], 
            '암호': str(data['password']), 
            '직급': data['position'],
            '레벨': 10, 
            '주민번호': data.get('rrn', ''),
            '전화번호': data.get('phone', ''), 
            '주소': data.get('address', ''), 
            '기타사항': data.get('note', ''), 
            '승인상태': '대기'
        }])
        
        df = pd.concat([df, new_user], ignore_index=True)
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "회원 등록 신청이 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/approve', methods=['POST'])
def approve():
    try:
        data = request.json
        is_valid, admin_name = verify_admin(data.get('admin_pass'))
        
        if not is_valid:
            return jsonify({"status": "error", "message": "관리자 암호가 틀리거나 권한이 없습니다."}), 403

        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        approved_pos = data['approved_position']
        
        df.at[idx, '직급'] = approved_pos
        df.at[idx, '레벨'] = LEVEL_MAP.get(approved_pos, 10)
        df.at[idx, '승인상태'] = '승인'
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": f"{approved_pos}(으)로 처리가 완료되었습니다. (인증: {admin_name})"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/delete', methods=['POST'])
def delete_user():
    try:
        data = request.json
        is_valid, _ = verify_admin(data.get('admin_pass'))
        
        if not is_valid:
            return jsonify({"status": "error", "message": "삭제 권한이 없습니다 (관리자 암호 확인)."}), 403

        df = read_excel_db(OWNER_FILE)
        idx = int(data['user_idx'])
        df = df.drop(df.index[idx]).reset_index(drop=True)
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "사용자가 삭제되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/list')
def get_user_list():
    df = read_excel_db(OWNER_FILE)
    return jsonify(df.to_dict(orient='records') if not df.empty else [])