DEPARTMENT_OPTIONS = ('본부', '북부지점', '파견', '기타')
ORGANIZATION_GROUPS = ('본부', '북부지점', '센터장', '코디', '강사', '기타')
MESSENGER_ORGANIZATION_GROUPS = ('본부', '센터장', '코디', '강사', '기타')

POSITION_GROUPS = {
    '본부': {'대표이사', '이사', '실장', '팀장', '사원', '계약직'},
    '센터장': {'센터장(팀장)', '센터장'},
    '코디': {'전담코디', '보조코디', '안전코디'},
    '강사': {'방과후강사', '맞춤형강사'},
    '기타': {'임시회원'},
}

MESSENGER_POSITION_GROUPS = {
    '본부': {'대표이사', '이사', '실장', '팀장', '사원', '계약직'},
    '센터장': {'센터장팀장', '센터장'},
    '코디': {'전담코디', '보조코디', '안전코디'},
    '강사': {'방과후강사', '맞춤형강사'},
}


def normalize_department(department):
    """기존 자유입력 소속을 표준 부서 선택값으로 안전하게 표시한다."""
    department = str(department or '').strip()
    if '북부지점' in department or '북부 지점' in department:
        return '북부지점'
    if department in {'본부', '본사'}:
        return '본부'
    if department == '파견' or '파견' in department:
        return '파견'
    return '기타'


def classify_organization_group(department, position):
    """북부지점 소속을 우선하고, 그 외 사용자는 직급으로 조직도 그룹을 정한다."""
    department = str(department or '').strip()
    position = str(position or '').strip()

    # 북부지점 소속은 직급과 관계없이 하나의 지점 그룹으로 묶는다.
    if '북부지점' in department or '북부 지점' in department:
        return '북부지점'

    for group, positions in POSITION_GROUPS.items():
        if position in positions:
            return group

    return '기타'


def normalize_messenger_department(department):
    """메신저에서는 본부와 북부지점을 하나의 본부 소속으로 표시한다."""
    normalized = normalize_department(department)
    return '본부' if normalized in {'본부', '북부지점'} else normalized


def classify_messenger_organization_group(department, position, level=None):
    """메신저 조직도를 직급 레벨과 직급명 기준의 5개 그룹으로 분류한다."""
    try:
        position_level = int(level)
    except (TypeError, ValueError):
        position_level = None

    # 직급 레벨관리에서 신규 직급을 추가해도 1~6레벨은 모두 본부로 묶는다.
    if position_level is not None and 1 <= position_level <= 6:
        return '본부'

    normalized_department = normalize_department(department)
    compact_position = ''.join(
        character for character in str(position or '').strip()
        if not character.isspace() and character not in '()'
    )
    if (
        normalized_department in {'본부', '북부지점'}
        and compact_position in MESSENGER_POSITION_GROUPS['본부']
    ):
        return '본부'
    for group in ('센터장', '코디', '강사'):
        positions = MESSENGER_POSITION_GROUPS[group]
        if compact_position in positions:
            return group
    return '기타'
