from flask import Blueprint, render_template, request, jsonify
from .db_handler import read_excel_db, write_excel_db, OWNER_FILE
import pandas as pd

user_mgmt_bp = Blueprint('user_mgmt', __name__)

# 직급별 레벨 정의
LEVEL_MAP = {
    "대표이사": 1, "이사": 2, "실장": 3, "팀장": 4, "사원": 5,
    "센터장": 6, "전담코디": 7, "안전코디": 8, "계약직": 9, "임시회원": 10
}

@user_mgmt_bp.route('/')
def index():
    return render_template('user_mgmt/index.html')

# 1) 회원 입력 기능 (누구나 입력 가능)
@user_mgmt_bp.route('/register', methods=['POST'])
def register():
    data = request.json
    df = read_excel_db(OWNER_FILE)
    
    if not df.empty and data['name'] in df['이름'].values:
        return jsonify({"status": "error", "message": "이미 등록된 이름입니다."}), 400

    new_user = pd.DataFrame([{
        '이름': data['name'],
        '암호': data['password'],
        '직급': data['position'],
        '레벨': 10,  # 초기 레벨은 임시회원
        '주민번호': data['rrn'],
        '전화번호': data['phone'],
        '주소': data['address'],
        '기타사항': data['note'],
        '승인상태': '대기'
    }])
    
    df = pd.concat([df, new_user], ignore_index=True)
    write_excel_db(df, OWNER_FILE)
    return jsonify({"status": "success", "message": "회원 등록 신청이 완료되었습니다."})

# 2, 3) 회원 승인 및 레벨 부여 (대표/이사만 가능)
@user_mgmt_bp.route('/approve', methods=['POST'])
def approve():
    data = request.json
    df = read_excel_db(OWNER_FILE)
    
    # 승인자 권한 체크 (레벨 1: 대표이사, 2: 이사만 가능)
    admin = df[(df['이름'] == data['admin_name']) & (df['암호'].astype(str) == str(data['admin_pass']))]
    if admin.empty or admin.iloc[0]['레벨'] > 2:
        return jsonify({"status": "error", "message": "승인 권한이 없습니다."}), 403

    # 대상자 인덱스 확인 및 정보 업데이트
    try:
        idx = int(data['user_idx'])
        approved_pos = data['approved_position']
        
        df.at[idx, '직급'] = approved_pos
        df.at[idx, '레벨'] = LEVEL_MAP.get(approved_pos, 10) # 직급별 레벨 부여
        df.at[idx, '승인상태'] = '승인'
        
        write_excel_db(df, OWNER_FILE)
        return jsonify({"status": "success", "message": f"{approved_pos}(레벨 {df.at[idx, '레벨']})로 승인되었습니다."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@user_mgmt_bp.route('/list')
def get_user_list():
    df = read_excel_db(OWNER_FILE)
    return jsonify(df.to_dict(orient='records') if not df.empty else [])