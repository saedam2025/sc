from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import os
from datetime import datetime

app = Flask(__name__)

# --- [저장 경로 설정: Render 전용 디스크 적용] ---
MOUNT_PATH = '/mnt/data'
EXCEL_FILE = os.path.join(MOUNT_PATH, 'tasks.xlsx')

# 엑셀 파일 및 폴더 초기화
def init_excel():
    if not os.path.exists(MOUNT_PATH):
        try:
            os.makedirs(MOUNT_PATH)
        except:
            pass # 로컬 테스트 환경 배려
            
    if not os.path.exists(EXCEL_FILE):
        df = pd.DataFrame(columns=['연도', '날짜', '담당자', '구분', '업무내용', '비고', '기타'])
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')

@app.route('/')
def index():
    return render_template('index.html')

# 달력에 표시할 데이터 가져오기
@app.route('/get_tasks')
def get_tasks():
    if not os.path.exists(EXCEL_FILE):
        return jsonify([])
    
    try:
        df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        df = df.fillna('') # 빈 칸 처리
        tasks = []
        for _, row in df.iterrows():
            tasks.append({
                "title": f"[{row['담당자']}] {row['업무내용']}",
                "start": str(row['날짜']),
                "extendedProps": {
                    "owner": row['담당자'],
                    "category": row['구분'],
                    "note": row['비고'],
                    "etc": row['기타']
                }
            })
        return jsonify(tasks)
    except Exception as e:
        print(f"Read Error: {e}")
        return jsonify([])

# 새로운 일정 엑셀에 저장하기
@app.route('/save_task', methods=['POST'])
def save_task():
    data = request.json
    try:
        if os.path.exists(EXCEL_FILE):
            df = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        else:
            df = pd.DataFrame(columns=['연도', '날짜', '담당자', '구분', '업무내용', '비고', '기타'])

        date_obj = datetime.strptime(data['date'], '%Y-%m-%d')
        
        new_row = {
            '연도': date_obj.year,
            '날짜': data['date'],
            '담당자': data.get('owner', ''),
            '구분': data.get('category', ''),
            '업무내용': data.get('title', ''),
            '비고': data.get('note', ''),
            '기타': data.get('etc', '')
        }
        
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        df.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Save Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# 엑셀 파일 다운로드 경로 (주소/download 접속)
@app.route('/download')
def download_file():
    if os.path.exists(EXCEL_FILE):
        return send_file(EXCEL_FILE, as_attachment=True)
    return "파일이 아직 없습니다.", 404

if __name__ == '__main__':
    init_excel()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)