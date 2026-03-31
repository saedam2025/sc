import pandas as pd
import os

# Render 환경 및 로컬 환경 대응
STORAGE_DIR = '/mnt/data' if os.path.exists('/mnt/data') else 'database'
EXCEL_FILE = os.path.join(STORAGE_DIR, 'tasks.xlsx')
OWNER_FILE = os.path.join(STORAGE_DIR, 'owners.xlsx')
ATTEND_FILE = os.path.join(STORAGE_DIR, 'attendance.xlsx')

if not os.path.exists(STORAGE_DIR):
    os.makedirs(STORAGE_DIR, exist_ok=True)

def read_excel_db(file_path):
    if not os.path.exists(file_path):
        return pd.DataFrame()
    return pd.read_excel(file_path, engine='openpyxl').fillna('')

def write_excel_db(df, file_path):
    df.to_excel(file_path, index=False, engine='openpyxl')