from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import os
import re
from datetime import datetime

app = Flask(__name__)

# [경로 설정] Render 및 로컬 환경 대응
STORAGE_DIR = '/mnt/data'
if not os.path.exists(STORAGE_DIR):
    STORAGE_DIR = os.path.dirname(os.path.abspath(__file__))

EXCEL_FILE = os.path.join(STORAGE_DIR, 'tasks.xlsx')
OWNER_FILE = os.path.join(STORAGE_DIR, 'owners.xlsx')
ADMIN_PASSWORD = "1900" # 관리자 암호

def init_files():
    """엑셀 파일 초기 생성 및 컬럼 구성"""
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['연도', '날짜', '담당자', '내근업무', '외근업무', '회의', '비고', '기타'])
        df.to_excel(EXCEL_FILE, index=False)
    if not os.path.exists(OWNER_FILE):
        df = pd.DataFrame(columns=['이름', '암호'])
        df.to_excel(OWNER_FILE, index=False)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_owners')
def get_owners():
    try:
        df = pd.read_excel(OWNER_FILE).fillna('')
        return jsonify(df['이름'].tolist())
    except: return jsonify([])

@app.route('/add_owner', methods=['POST'])
def add_owner():
    data = request.json
    if data.get('admin_pass') != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "관리자 암호 불일치"}), 403
    try:
        df = pd.read_excel(OWNER_FILE)
        if data['name'] in df['이름'].values:
            return jsonify({"status": "error", "message": "이미 등록된 사용자입니다."})
        new_row = pd.DataFrame([{'이름': data['name'], '암호': str(data['owner_pass'])}])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_excel(OWNER_FILE, index=False)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_tasks')
def get_tasks():
    try:
        if not os.path.exists(EXCEL_FILE): return jsonify([])
        df = pd.read_excel(EXCEL_FILE).fillna('')
        tasks = []
        for idx, row in df.iterrows():
            tasks.append({
                "id": str(idx),
                "start": str(row['날짜']),
                "extendedProps": {
                    "owner": row['담당자'], "inside": row['내근업무'], "outside": row['외근업무'],
                    "meeting": row['회의'], "note": row['비고'], "etc": row.get('기타', '')
                }
            })
        return jsonify(tasks)
    except: return jsonify([])

@app.route('/save_task', methods=['POST'])
def save_task():
    data = request.json
    try:
        df = pd.read_excel(EXCEL_FILE)
        new_row = pd.DataFrame([{
            '연도': data['date'][:4], '날짜': data['date'], '담당자': data['owner'],
            '내근업무': data['inside'], '외근업무': data['outside'],
            '회의': data['meeting'], '비고': data['note'], '기타': data.get('etc', '')
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_excel(EXCEL_FILE, index=False)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/update_task', methods=['POST'])
def update_task():
    data = request.json
    try:
        owners_df = pd.read_excel(OWNER_FILE).fillna('')
        target_owner = owners_df[owners_df['이름'] == data['owner']]
        if target_owner.empty or str(target_owner.iloc[0]['암호']) != str(data['password']):
            return jsonify({"status": "error", "message": "비밀번호가 일치하지 않습니다."}), 403
            
        # [수정] dtype='object'를 사용하여 float64 변환 오류 방지
        df = pd.read_excel(EXCEL_FILE).astype(object).fillna('')
        idx = int(data['id'])
        df.at[idx, '내근업무'] = data['inside']
        df.at[idx, '외근업무'] = data['outside']
        df.at[idx, '회의'] = data['meeting']
        df.at[idx, '비고'] = data['note']
        df.at[idx, '기타'] = data.get('etc', '')
        df.to_excel(EXCEL_FILE, index=False)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/get_school_stats')
def get_school_stats():
    """학교 방문 데이터 분석 로직 (줄바꿈/쉼표 구분 대응)"""
    try:
        df = pd.read_excel(EXCEL_FILE).fillna('')
        school_keywords = ['초', '중', '고', '학교']
        all_schools = []
        for _, row in df.iterrows():
            entries = re.split(r'[\n,]', str(row['외근업무']))
            for entry in entries:
                entry = entry.strip()
                for k in school_keywords:
                    if k in entry:
                        all_schools.append(entry.split()[-1]) # 마지막 단어 추출
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
    return send_file(EXCEL_FILE, as_attachment=True)

if __name__ == '__main__':
    init_files()
    app.run(host='0.0.0.0', port=5000)