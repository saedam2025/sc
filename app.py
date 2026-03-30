from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import os
from datetime import datetime

app = Flask(__name__)

# Render 무료 티어 환경: 현재 실행 폴더에 엑셀 저장
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXCEL_FILE = os.path.join(BASE_DIR, 'tasks.xlsx')

def init_excel():
    """서버 시작 시 엑셀 파일이 없으면 7개 항목으로 초기화"""
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['연도', '날짜', '담당자', '내근업무', '외근업무', '비고', '기타'])
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_tasks')
def get_tasks():
    """달력에 표시할 모든 데이터를 불러옴"""
    if not os.path.exists(EXCEL_FILE): return jsonify([])
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl').fillna('')
        tasks = []
        for _, row in df.iterrows():
            tasks.append({
                "start": str(row['날짜']),
                "extendedProps": {
                    "year": row['연도'],
                    "owner": row['담당자'],
                    "inside": row['내근업무'],
                    "outside": row['외근업무'],
                    "note": row['비고'],
                    "etc": row['기타']
                }
            })
        return jsonify(tasks)
    except Exception as e:
        print(f"로드 에러: {e}")
        return jsonify([])

@app.route('/save_task', methods=['POST'])
def save_task():
    """7개 항목을 받아서 엑셀에 한 줄 추가"""
    data = request.json
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl') if os.path.exists(EXCEL_FILE) else pd.DataFrame()
        date_obj = datetime.strptime(data['date'], '%Y-%m-%d')
        
        new_row = {
            '연도': date_obj.year, 
            '날짜': data['date'],
            '담당자': data.get('owner', ''), 
            '내근업무': data.get('inside', ''), 
            '외근업무': data.get('outside', ''),
            '비고': data.get('note', ''), 
            '기타': data.get('etc', '')
        }
        
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"저장 에러: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/download')
def download_file():
    """엑셀 파일 내 컴퓨터로 받기"""
    if os.path.exists(EXCEL_FILE):
        return send_file(EXCEL_FILE, as_attachment=True)
    return "파일이 아직 생성되지 않았습니다.", 404

if __name__ == '__main__':
    init_excel()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)