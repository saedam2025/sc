from flask import Blueprint, request, send_file, render_template, abort, current_app
import io, os, re, zipfile
import pandas as pd
from datetime import datetime

# 블루프린트 이름 지정
excel_bp = Blueprint("excel_generator", __name__)
DEPOSIT_CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", "200"))  # 시트 누적 인원 기준 (기본 200)

def deposit_clean_account(acc):
    if pd.isna(acc):
        return ""
    s = str(acc).strip()
    if re.fullmatch(r"\d+(\.\d+)?[eE][+-]?\d+", s):
        try:
            from decimal import Decimal
            s = format(Decimal(s), "f").rstrip("0").rstrip(".")
        except Exception:
            pass
    if s.endswith(".0"):
        s = s[:-2]
    s = s.replace(" ", "")
    # s = s.replace("-", "")  # 필요 시 하이픈 제거
    return s

@excel_bp.route("/excel-generator", methods=["GET"])
def deposit_index():
    # 분리된 HTML 템플릿 렌더링
    return render_template('excel_generator.html', chunk_size=DEPOSIT_CHUNK_SIZE)

@excel_bp.route("/excel-generator/process", methods=["POST"])
def deposit_process():
    up = request.files.get("file")
    if not up:
        abort(400, "파일이 없습니다.")
    if not up.filename.lower().endswith(".xlsx"):
        abort(400, "xlsx 형식만 지원합니다.")

    # BASE_DIR 대신 현재 작업 디렉토리를 기준으로 업로드 폴더 설정
    upload_root = os.path.join(os.getcwd(), "uploads_deposit")
    os.makedirs(upload_root, exist_ok=True)

    input_path = os.path.join(upload_root, f"_upload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
    up.save(input_path)

    try:
        sheets = deposit_read_sheets_as_list(input_path)
        if not sheets:
            abort(400, "추출할 데이터가 없습니다. (열 이름/헤더 3행 확인)")

        parts = deposit_split_by_sheet_boundary(sheets, DEPOSIT_CHUNK_SIZE)
        today = datetime.today().strftime("%Y-%m-%d")

        # 파트 1개면 단일 파일 반환
        if len(parts) == 1:
            merged = pd.concat([df for _, df in parts[0]], ignore_index=True)
            xlsx_bytes = deposit_build_excel_bytes(merged, file_label=today)
            return send_file(
                io.BytesIO(xlsx_bytes),
                as_attachment=True,
                download_name=f"입금내역_{today}.xlsx",
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        # 여러 파트면 ZIP으로 압축하여 반환
        buff = io.BytesIO()
        with zipfile.ZipFile(buff, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for i, part in enumerate(parts, start=1):
                merged = pd.concat([df for _, df in part], ignore_index=True)
                part_no = f"{i:02d}"
                fname = f"입금내역_{today}_part{part_no}.xlsx"
                xlsx_bytes = deposit_build_excel_bytes(merged, file_label=f"{today} part {part_no}")
                zf.writestr(fname, xlsx_bytes)

        buff.seek(0)
        return send_file(buff, as_attachment=True, download_name=f"입금내역_{today}_split.zip", mimetype="application/zip")

    finally:
        try:
            os.remove(input_path)
        except Exception:
            pass

def deposit_read_sheets_as_list(input_path: str):
    """[(sheet_name, df_with__시트), ...]"""
    target = ["은행", "계좌번호", "예금주", "입금액"]
    out = []
    xlsx = pd.ExcelFile(input_path)
    for sheet_name in xlsx.sheet_names:
        df = pd.read_excel(xlsx, sheet_name=sheet_name, header=2, dtype={"계좌번호": str})
        if all(col in df.columns for col in target):
            filtered = df[target].dropna(subset=["예금주"]).copy()
            if filtered.empty:
                continue
            filtered["계좌번호"] = filtered["계좌번호"].apply(deposit_clean_account)
            filtered["입금액"] = pd.to_numeric(filtered["입금액"], errors="coerce")
            filtered["_시트"] = sheet_name
            out.append((sheet_name, filtered))
    return out

def deposit_split_by_sheet_boundary(sheet_dfs, chunk_size: int):
    """시트 경계 기준 누적 인원 chunk_size 초과 시 파트 분할 (시트는 절대 쪼개지지 않음)."""
    parts, current, count = [], [], 0
    for sheet_name, df in sheet_dfs:
        rows = len(df)
        if rows == 0:
            current.append((sheet_name, df))
            continue
        if count > 0 and (count + rows) > chunk_size:
            parts.append(current)
            current, count = [], 0
        current.append((sheet_name, df))
        count += rows
        if rows > chunk_size:
            parts.append(current)
            current, count = [], 0
    if current:
        parts.append(current)
    return parts

def deposit_build_excel_bytes(df_with_sheet: pd.DataFrame, file_label: str) -> bytes:
    """'_시트'는 계산용(출력 제외). 시트 구간 마지막 행 아래 H/I에 합계, H2/I2에 총합, F열 '새담청소년교육'."""
    visible_cols = [c for c in df_with_sheet.columns if c != "_시트"]
    result_df = df_with_sheet[visible_cols].copy()

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        result_df.to_excel(writer, index=False)
        workbook = writer.book
        ws = writer.sheets["Sheet1"]

        for idx, col in enumerate(result_df.columns):
            if col == "계좌번호":
                ws.set_column(idx, idx, 25, workbook.add_format({"num_format": "@"}))
            elif col == "입금액":
                ws.set_column(idx, idx, 15, workbook.add_format({"num_format": "#,##0"}))
            else:
                ws.set_column(idx, idx, 20)

        # F열 "새담청소년교육" (헤더 제외)
        tf = workbook.add_format({"num_format": "@"})
        for r in range(1, len(result_df) + 1):
            ws.write(r, 5, "새담청소년교육", tf)
        ws.set_column(5, 5, 20, tf)

        # 계좌번호 문자열 강제
        if "계좌번호" in result_df.columns:
            acc_idx = list(result_df.columns).index("계좌번호")
            for r, v in enumerate(result_df["계좌번호"].fillna(""), start=1):
                ws.write_string(r, acc_idx, str(v))

        # 시트별 합계 (H/I)
        amt_fmt = workbook.add_format({"num_format": "#,##0"})
        if "_시트" in df_with_sheet.columns and "입금액" in df_with_sheet.columns and len(df_with_sheet) > 0:
            ss = df_with_sheet["_시트"].astype(str).tolist()
            blocks = [0]
            for i in range(1, len(ss)):
                if ss[i] != ss[i-1]:
                    blocks.append(i)
            blocks.append(len(ss))
            for b in range(len(blocks)-1):
                start, end = blocks[b], blocks[b+1]
                sheet_name = ss[start]
                target_row = end  # 헤더 다음부터 데이터 1행이므로 end가 맞음
                sub_total = pd.to_numeric(df_with_sheet.iloc[start:end]["입금액"], errors="coerce").sum(skipna=True)
                ws.write(target_row, 7, f"{sheet_name} 합계:")
                ws.write_number(target_row, 8, sub_total if pd.notna(sub_total) else 0, amt_fmt)

        grand_total = pd.to_numeric(result_df.get("입금액", []), errors="coerce").sum(skipna=True) if len(result_df) else 0
        ws.write(1, 7, "총입금액:")
        ws.write_number(1, 8, grand_total if pd.notna(grand_total) else 0, amt_fmt)

    output.seek(0)
    return output.getvalue()