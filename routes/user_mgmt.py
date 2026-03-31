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
        # user_list.html 템플릿 호출
        return render_template('user_mgmt/user_list.html')
    except Exception as e:
        return f"템플릿 에러: {str(e)}", 500

@user_mgmt_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        
        if not df.empty and data['name'] in df['이름'].values:
            return jsonify({"status": "error", "message": "이미 등록된 이름입니다."}), 400

        new_user = pd.DataFrame([{
            '이름': data['name'], '암호': data['password'], '직급': data['position'],
            '레벨': 10, '주민번호': data.get('rrn', ''), '전화번호': data.get('phone', ''),
            '주소': data.get('address', ''), '기타사항': data.get('note', ''), '승인상태': '대기'
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
        df = read_excel_db(OWNER_FILE)
        
        # 관리자 인증 (이사 이상 권한 확인)
        admin = df[(df['이름'] == data['admin_name']) & (df['암호'].astype(str) == str(data['admin_pass']))]
        if admin.empty or admin.iloc[0]['레벨'] > 2:
            return jsonify({"status": "error", "message": "권한이 없습니다 (이사 이상 가능)."}), 403

        idx = int(data['user_idx'])
        approved_pos = data['approved_position']
        
        # 직급 및 레벨 업데이트
        df.at[idx, '직급'] = approved_pos
        df.at[idx, '레벨'] = LEVEL_MAP.get(approved_pos, 10)
        df.at[idx, '승인상태'] = '승인'
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": f"처리가 완료되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 추가된 삭제(Delete) 기능 ---
@user_mgmt_bp.route('/delete', methods=['POST'])
def delete_user():
    try:
        data = request.json
        df = read_excel_db(OWNER_FILE)
        
        # 관리자 인증
        admin = df[(df['이름'] == data['admin_name']) & (df['암호'].astype(str) == str(data['admin_pass']))]
        if admin.empty or admin.iloc[0]['레벨'] > 2:
            return jsonify({"status": "error", "message": "삭제 권한이 없습니다."}), 403

        idx = int(data['user_idx'])
        # 해당 인덱스 행 삭제
        df = df.drop(df.index[idx])
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": "회원이 삭제되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/list')
def get_user_list():
    df = read_excel_db(OWNER_FILE)
    return jsonify(df.to_dict(orient='records') if not df.empty else [])