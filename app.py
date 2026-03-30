from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import os
from datetime import datetime

app = Flask(__name__)

# [경로 설정] Render 환경 및 로컬 환경 호환을 위한 절대경로 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE = os.path.join(BASE_DIR, 'tasks.xlsx')
OWNER_FILE = os.path.join(BASE_DIR, 'owners.xlsx')

def init_files():
    """서버 시작 시 파일이 없으면 자동 생성"""
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['연도', '날짜', '담당자', '내근업무', '외근업무', '회의', '면접', '비고', '기타'])
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
    if not os.path.exists(OWNER_FILE):
        df = pd.DataFrame(columns=['이름'])
        df.to_excel(OWNER_FILE, index=False, engine='openpyxl')

@app.route('/')
def index():
    return render_template('index.html')

# --- 담당자 관리 API ---
@app.route('/get_owners')
def get_owners():
    if not os.path.exists(OWNER_FILE): init_files()
    try:
        df = pd.read_excel(OWNER_FILE, engine='openpyxl').fillna('')
        return jsonify(df['이름'].tolist())
    except:
        return jsonify([])

@app.route('/add_owner', methods=['POST'])
def add_owner():
    name = request.json.get('name')
    if not name: return jsonify({"status": "error", "message": "이름 누락"})
    if not os.path.exists(OWNER_FILE): init_files()
    try:
        df = pd.read_excel(OWNER_FILE, engine='openpyxl')
        if name not in df['이름'].values:
            new_row = pd.DataFrame([{'이름': name}])
            df = pd.concat([df, new_row], ignore_index=True)
            df.to_excel(OWNER_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# --- 업무 데이터 API ---
@app.route('/get_tasks')
def get_tasks():
    if not os.path.exists(EXCEL_FILE): init_files()
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').fillna('')
        tasks = []
        for _, row in df.iterrows():
            tasks.append({
                "start": str(row['날짜']),
                "extendedProps": {
                    "owner": row['담당자'], "inside": row['내근업무'], "outside": row['외근업무'],
                    "meeting": row['회의'], "interview": row['면접'], "note": row['비고'], "etc": row['기타']
                }
            })
        return jsonify(tasks)
    except:
        return jsonify([])

@app.route('/save_task', methods=['POST'])
def save_task():
    data = request.json
    if not os.path.exists(EXCEL_FILE): init_files()
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        date_obj = datetime.strptime(data['date'], '%Y-%m-%d')
        new_row = {
            '연도': date_obj.year, '날짜': data['date'], '담당자': data['owner'],
            '내근업무': data['inside'], '외근업무': data['outside'],
            '회의': data['meeting'], '면접': data['interview'],
            '비고': data['note'], '기타': data.get('etc', '')
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download')
def download_file():
    if os.path.exists(EXCEL_FILE):
        return send_file(EXCEL_FILE, as_attachment=True)
    return "파일 없음", 404

if __name__ == '__main__':
    init_files()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)