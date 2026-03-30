from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import os
import re
from datetime import datetime

app = Flask(__name__)

# [경로 설정] Render 및 로컬 환경 대응
STORAGE_DIR = '/mnt/data' if os.path.exists('/mnt/data') else os.path.dirname(os.path.abspath(__file__))

EXCEL_FILE = os.path.join(STORAGE_DIR, 'tasks.xlsx')
OWNER_FILE = os.path.join(STORAGE_DIR, 'owners.xlsx')
ATTEND_FILE = os.path.join(STORAGE_DIR, 'attendance.xlsx')
ADMIN_PASSWORD = "1900" 

def init_files():
    """파일 초기화 및 컬럼 구성"""
    try:
        if not os.path.exists(STORAGE_DIR):
            os.makedirs(STORAGE_DIR, exist_ok=True)

        if not os.path.exists(EXCEL_FILE):
            pd.DataFrame(columns=['연도', '날짜', '담당자', '내근업무', '외근업무', '회의', '비고', '기타']).to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        
        if not os.path.exists(OWNER_FILE):
            # '직책' 컬럼 추가 (이사, 대표이사 등)
            pd.DataFrame(columns=['이름', '암호', '직책']).to_excel(OWNER_FILE, index=False, engine='openpyxl')

        if not os.path.exists(ATTEND_FILE):
            pd.DataFrame(columns=['신청일', '담당자', '구분', '시작일', '종료일', '사유', '승인상태']).to_excel(ATTEND_FILE, index=False, engine='openpyxl')
    except Exception as e:
        print(f"파일 초기화 실패: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_owners')
def get_owners():
    init_files()
    try:
        df = pd.read_excel(OWNER_FILE, engine='openpyxl').fillna('')
        return jsonify(df.to_dict(orient='records'))
    except: return jsonify([])

@app.route('/add_owner', methods=['POST'])
def add_owner():
    init_files()
    data = request.json
    if str(data.get('admin_pass')) != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "관리자 암호 불일치"}), 403
    try:
        df = pd.read_excel(OWNER_FILE, engine='openpyxl')
        if data['name'] in df['이름'].values:
            return jsonify({"status": "error", "message": "이미 등록된 사용자입니다."})
        
        new_row = pd.DataFrame([{
            '이름': data['name'], 
            '암호': str(data['owner_pass']),
            '직책': data.get('position', '담당자')
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_excel(OWNER_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_tasks')
def get_tasks():
    init_files()
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').fillna('')
        tasks = []
        for idx, row in df.iterrows():
            tasks.append({
                "id": str(idx),
                "start": str(row['날짜']),
                "extendedProps": {
                    "owner": str(row['담당자']), "inside": str(row['내근업무']), "outside": str(row['외근업무']),
                    "meeting": str(row['회의']), "note": str(row['비고']), "etc": str(row.get('기타', ''))
                }
            })
        return jsonify(tasks)
    except: return jsonify([])

@app.route('/save_task', methods=['POST'])
def save_task():
    init_files()
    data = request.json
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        new_row = pd.DataFrame([{
            '연도': str(data['date'])[:4], '날짜': str(data['date']), '담당자': str(data['owner']),
            '내근업무': str(data['inside']), '외근업무': str(data['outside']),
            '회의': str(data['meeting']), '비고': str(data['note']), '기타': str(data.get('etc', ''))
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/update_task', methods=['POST'])
def update_task():
    init_files()
    data = request.json
    try:
        owners_df = pd.read_excel(OWNER_FILE, engine='openpyxl').fillna('')
        target_owner = owners_df[owners_df['이름'] == data['owner']]
        if target_owner.empty or str(target_owner.iloc[0]['암호']) != str(data['password']):
            return jsonify({"status": "error", "message": "비밀번호가 일치하지 않습니다."}), 403
            
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').astype(object).fillna('')
        idx = int(data['id'])
        df.at[idx, '내근업무'] = str(data['inside'])
        df.at[idx, '외근업무'] = str(data['outside'])
        df.at[idx, '회의'] = str(data['meeting'])
        df.at[idx, '비고'] = str(data['note'])
        df.at[idx, '기타'] = str(data.get('etc', ''))
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/submit_attendance', methods=['POST'])
def submit_attendance():
    init_files()
    data = request.json
    try:
        owners_df = pd.read_excel(OWNER_FILE, engine='openpyxl').fillna('')
        user = owners_df[(owners_df['이름'] == data['owner']) & (owners_df['암호'].astype(str) == str(data['password']))]
        if user.empty:
            return jsonify({"status": "error", "message": "비밀번호가 틀렸습니다."}), 403

        df = pd.read_excel(ATTEND_FILE, engine='openpyxl')
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
        df.to_excel(ATTEND_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_attendance')
def get_attendance():
    init_files()
    try:
        df = pd.read_excel(ATTEND_FILE, engine='openpyxl').fillna('')
        data = df.to_dict(orient='records')
        # 리스트에 인덱스 번호를 추가하여 승인 시 참조 가능케 함
        for i, item in enumerate(data): item['idx'] = i
        return jsonify(data)
    except: return jsonify([])

@app.route('/approve_attendance', methods=['POST'])
def approve_attendance():
    init_files()
    data = request.json
    try:
        owners_df = pd.read_excel(OWNER_FILE, engine='openpyxl').fillna('')
        admin = owners_df[(owners_df['이름'] == data['admin_name']) & (owners_df['암호'].astype(str) == str(data['admin_password']))]
        
        if admin.empty or admin.iloc[0]['직책'] not in ['이사', '대표이사']:
            return jsonify({"status": "error", "message": "승인 권한이 없습니다."}), 403

        df = pd.read_excel(ATTEND_FILE, engine='openpyxl')
        idx = int(data['idx'])
        df.at[idx, '승인상태'] = data['status']
        df.to_excel(ATTEND_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_school_stats')
def get_school_stats():
    init_files()
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').fillna('')
        school_keywords = ['초', '중', '고', '학교']
        all_schools = []
        for _, row in df.iterrows():
            entries = re.split(r'[\n,]', str(row['외근업무']))
            for entry in entries:
                entry = entry.strip()
                if not entry: continue
                for k in school_keywords:
                    if k in entry:
                        all_schools.append(entry.split()[-1])
                        break
        if not all_schools: return jsonify({"status":"empty"})
        counts = pd.Series(all_schools).value_counts()
        return jsonify({
            "top5": {"labels": counts.head(5).index.tolist(), "datasets": [{"label": "방문수", "data": counts.head(5).values.tolist(), "backgroundColor": '#4e73df'}]},
            "total": len(all_schools)
        })
    except: return jsonify({"status":"empty"})

@app.route('/download')
def download():
    if os.path.exists(EXCEL_FILE): return send_file(EXCEL_FILE, as_attachment=True)
    return "파일 없음", 404

if __name__ == '__main__':
    init_files()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)