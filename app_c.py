from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import os
import re
from datetime import datetime

app = Flask(__name__)

# [경로 설정] Render 및 로컬 환경 대응
# /mnt/data는 Render의 유료 디스크 경로입니다. 없을 경우 현재 실행 경로를 사용합니다.
STORAGE_DIR = '/mnt/data' if os.path.exists('/mnt/data') else os.path.dirname(os.path.abspath(__file__))

EXCEL_FILE = os.path.join(STORAGE_DIR, 'tasks.xlsx')
OWNER_FILE = os.path.join(STORAGE_DIR, 'owners.xlsx')
ADMIN_PASSWORD = "1900" # 관리자 암호

def init_files():
    """엑셀 파일 초기 생성 및 폴더 권한 확인"""
    try:
        if not os.path.exists(STORAGE_DIR):
            os.makedirs(STORAGE_DIR, exist_ok=True)

        if not os.path.exists(EXCEL_FILE):
            df = pd.DataFrame(columns=['연도', '날짜', '담당자', '내근업무', '외근업무', '회의', '비고', '기타'])
            df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        
        if not os.path.exists(OWNER_FILE):
            df = pd.DataFrame(columns=['이름', '암호'])
            df.to_excel(OWNER_FILE, index=False, engine='openpyxl')
    except Exception as e:
        print(f"파일 초기화 실패: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_owners')
def get_owners():
    init_files() # 파일 존재 여부 상시 체크
    try:
        df = pd.read_excel(OWNER_FILE, engine='openpyxl').fillna('')
        return jsonify(df['이름'].astype(str).tolist())
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
        
        new_row = pd.DataFrame([{'이름': data['name'], '암호': str(data['owner_pass'])}])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_excel(OWNER_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_tasks')
def get_tasks():
    init_files()
    try:
        if not os.path.exists(EXCEL_FILE): return jsonify([])
        # 데이터 로드 시 모든 값을 문자열로 처리하여 오류 방지
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
            
        # [해결] float64 변환 오류를 방지하기 위해 전체 데이터를 문자열(object)로 로드
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').astype(object).fillna('')
        idx = int(data['id'])
        
        df.at[idx, '내근업무'] = str(data['inside'])
        df.at[idx, '외근업무'] = str(data['outside'])
        df.at[idx, '회의'] = str(data['meeting'])
        df.at[idx, '비고'] = str(data['note'])
        df.at[idx, '기타'] = str(data.get('etc', ''))
        
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": f"수정 실패: {str(e)}"}), 500

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
                        # 학교 이름만 깔끔하게 추출 (공백 기준 마지막 단어)
                        all_schools.append(entry.split()[-1])
                        break
        
        if not all_schools: return jsonify({"status":"empty"})
        counts = pd.Series(all_schools).value_counts()
        return jsonify({
            "top5": {
                "labels": counts.head(5).index.tolist(), 
                "datasets": [{"label": "방문수", "data": counts.head(5).values.tolist(), "backgroundColor": '#4e73df'}]
            },
            "total": len(all_schools)
        })
    except: return jsonify({"status":"empty"})

@app.route('/download')
def download():
    if os.path.exists(EXCEL_FILE):
        return send_file(EXCEL_FILE, as_attachment=True)
    return "파일이 존재하지 않습니다.", 404

if __name__ == '__main__':
    init_files()
    # Render 환경의 PORT 대응
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)