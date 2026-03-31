import pandas as pd
import os

STORAGE_DIR = '/mnt/data' if os.path.exists('/mnt/data') else 'database'
EXCEL_FILE = os.path.join(STORAGE_DIR, 'tasks.xlsx')
OWNER_FILE = os.path.join(STORAGE_DIR, 'owners.xlsx')
ATTEND_FILE = os.path.join(STORAGE_DIR, 'attendance.xlsx')

if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR, exist_ok=True)

def init_files():
    """파일이 없거나 컬럼이 부족하면 자동 보정"""
    # 1. 업무 파일
    if not os.path.exists(EXCEL_FILE):
        pd.DataFrame(columns=['연도', '날짜', '담당자', '내근업무', '외근업무', '회의', '비고', '기타']).to_excel(EXCEL_FILE, index=False)
    
    # 2. 회원 파일 (새로운 컬럼들 강제 추가)
    owner_cols = ['이름', '암호', '직급', '레벨', '주민번호', '전화번호', '주소', '기타사항', '승인상태']
    if not os.path.exists(OWNER_FILE):
        pd.DataFrame(columns=owner_cols).to_excel(OWNER_FILE, index=False)
    else:
        # 기존 파일이 있다면 부족한 컬럼이 있는지 확인 후 추가
        df = pd.read_excel(OWNER_FILE)
        changed = False
        for col in owner_cols:
            if col not in df.columns:
                df[col] = ''
                changed = True
        if changed:
            df.to_excel(OWNER_FILE, index=False)

    # 3. 근태 파일
    if not os.path.exists(ATTEND_FILE):
        pd.DataFrame(columns=['신청일', '담당자', '구분', '시작일', '종료일', '사유', '승인상태']).to_excel(ATTEND_FILE, index=False)

def read_excel_db(file_path):
    init_files()
    try:
        return pd.read_excel(file_path).fillna('')
    except:
        return pd.DataFrame()

def write_excel_db(df, file_path):
    df.to_excel(file_path, index=False)