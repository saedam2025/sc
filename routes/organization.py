ORGANIZATION_GROUPS = ('본부', '북부지점', '센터장', '강사', '기타')

HEADQUARTERS_POSITIONS = {
    '최고관리자', '대표이사', '이사', '실장', '팀장', '사원', '계약직'
}


def classify_organization_group(department, position):
    """선택한 소속부서를 우선하여 사용자의 조직도 그룹을 반환한다."""
    department = str(department or '').strip()
    position = str(position or '').strip()

    # 새 인사관리 선택값은 직급보다 우선한다.
    if department in ORGANIZATION_GROUPS:
        return department

    # 기존 자유입력 데이터도 새 5개 그룹에 맞춰 호환 분류한다.
    if '북부지점' in department or '북부 지점' in department:
        return '북부지점'
    if '본부' in department or '본사' in department:
        return '본부'
    if '센터장' in department or '센터장' in position or '코디' in position:
        return '센터장'
    if '강사' in department or '강사' in position:
        return '강사'
    if position in HEADQUARTERS_POSITIONS:
        return '본부'
    return '기타'
