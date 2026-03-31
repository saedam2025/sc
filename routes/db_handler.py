import pandas as pd
import os

# Render 환경 및 로컬 환경 대응
STORAGE_DIR = '/mnt/data' if os.path.exists('/mnt/data') else 'database'
EXCEL_FILE = os.path.join(STORAGE_DIR, 'tasks.xlsx')
OWNER_FILE = os.path.join(STORAGE_DIR, 'owners.xlsx')
ATTEND_FILE = os.path.join(STORAGE_DIR, 'attendance.xlsx')

if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR, exist_ok=True)

def init_files():
    """파일이 없을 경우 초기 컬럼과 함께 생성"""
    try:
        # 업무 스케줄 파일
        if not os.path.exists(EXCEL_FILE):
            pd.DataFrame(columns=['연도', '날짜', '담당자', '내근업무', '외근업무', '회의', '비고', '기타']).to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        
        # 회원 관리 파일 (확장 필드 포함)
        if not os.path.exists(OWNER_FILE):
            columns = ['이름', '암호', '직급', '레벨', '주민번호', '전화번호', '주소', '기타사항', '승인상태']
            pd.DataFrame(columns=columns).to_excel(OWNER_FILE, index=False, engine='openpyxl')

        # 근태/휴가 관리 파일
        if not os.path.exists(ATTEND_FILE):
            pd.DataFrame(columns=['신청일', '담당자', '구분', '시작일', '종료일', '사유', '승인상태']).to_excel(ATTEND_FILE, index=False, engine='openpyxl')
    except Exception as e:
        print(f"파일 초기화 실패: {e}")

def read_excel_db(file_path):
    init_files()
    if not os.path.exists(file_path):
        return pd.DataFrame()
    return pd.read_excel(file_path, engine='openpyxl').fillna('')

def write_excel_db(df, file_path):
    df.to_excel(file_path, index=False, engine='openpyxl')