from flask import Blueprint, render_template, request, session, jsonify
from datetime import datetime
import re

# app.py에서 사용하는 데이터베이스 연결 함수 가져오기
from routes.database import get_db

attendance_bp = Blueprint('attendance', __name__)

# 지각/정시퇴근 판단 기준 시각
WORK_START_STANDARD = '09:00:00'
WORK_END_STANDARD = '18:00:00'

# 통계 패널에서 보여줄 기간 수
MONTHLY_PERIODS = 12
QUARTERLY_PERIODS = 8

WEEKDAY_LABELS = ['월', '화', '수', '목', '금', '토', '일']

# 근무시간(시간 단위) 계산용 SQL 조각. 퇴근기록이 없거나 역전된 값은 제외한다.
WORK_HOURS_SQL = """
    CASE WHEN a.clock_out_time IS NOT NULL AND a.clock_out_time > a.clock_in_time
         THEN (STRFTIME('%s', a.date || ' ' || a.clock_out_time)
               - STRFTIME('%s', a.date || ' ' || a.clock_in_time)) / 3600.0
    END
"""

BASE_SELECT = """
    SELECT a.id,
           CAST(a.emp_no AS TEXT) AS emp_no,
           a.date,
           a.clock_in_time,
           a.clock_out_time,
           a.status,
           a.reason,
           a.in_ip,
           a.in_device,
           a.in_user_agent,
           a.out_ip,
           a.out_device,
           a.out_user_agent,
           COALESCE(NULLIF(a.position, ''), u.position, '미지정') AS position,
           COALESCE(u.name, CAST(a.emp_no AS TEXT)) AS user_name
    FROM daily_attendance a
    LEFT JOIN users u ON CAST(u.emp_no AS TEXT) = CAST(a.emp_no AS TEXT)
"""

STATS_FROM = """
    FROM daily_attendance a
    LEFT JOIN users u ON CAST(u.emp_no AS TEXT) = CAST(a.emp_no AS TEXT)
    LEFT JOIN user_work_schedule ws
           ON ws.emp_no = CAST(a.emp_no AS TEXT)
          AND ws.weekday = CAST(STRFTIME('%w', a.date) AS INTEGER)
"""

# 지각 판정: 개인정보 수정창의 요일별 출근시각 기준, 미설정이면 기본값(파라미터).
# 토·일과 휴무로 지정한 요일은 판정에서 제외한다.
LATE_SQL = """
    CASE
        WHEN CAST(STRFTIME('%w', a.date) AS INTEGER) NOT BETWEEN 1 AND 5 THEN 0
        WHEN COALESCE(ws.is_off, 0) = 1 THEN 0
        WHEN a.clock_in_time > COALESCE(ws.start_time || ':00', ?) THEN 1
        ELSE 0
    END
"""


# ---------------------------------------------------------------------------
# 접속정보(전송자 IP · 기기) 기록과 이상 감지
# ---------------------------------------------------------------------------

# 평소 접속정보를 계산할 기간(일)과, '평소'로 인정하기 위한 최소 기록 수.
ACCESS_BASELINE_DAYS = 90
ACCESS_BASELINE_MIN_RECORDS = 3

# User-Agent에서 사람이 읽을 수 있는 기기 요약을 뽑기 위한 규칙. 위에서부터 먼저 맞는 것을 쓴다.
_DEVICE_OS_RULES = (
    ('iPhone', 'iPhone'),
    ('iPad', 'iPad'),
    ('Android', 'Android'),
    ('Windows NT', 'Windows'),
    ('Windows', 'Windows'),
    ('Mac OS X', 'Mac'),
    ('Macintosh', 'Mac'),
    ('CrOS', 'ChromeOS'),
    ('Linux', 'Linux'),
)
_DEVICE_BROWSER_RULES = (
    ('Edg', 'Edge'),
    ('Whale', 'Whale'),
    ('SamsungBrowser', 'Samsung Internet'),
    ('OPR/', 'Opera'),
    ('CriOS', 'Chrome'),
    ('FxiOS', 'Firefox'),
    ('Chrome/', 'Chrome'),
    ('Firefox/', 'Firefox'),
    ('Safari/', 'Safari'),
)
_MOBILE_TOKENS = ('Mobile', 'Android', 'iPhone', 'iPad', 'iPod')


def _client_ip():
    """프록시(Render 등) 뒤에서도 실제 전송자 IP를 얻는다."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    ip = forwarded.split(',')[0].strip() if forwarded else (request.remote_addr or '')
    return ip[:100]


def _client_user_agent():
    return str(request.headers.get('User-Agent') or '')[:500]


def _device_label(user_agent):
    """User-Agent를 '모바일 · Android · Chrome' 형태의 짧은 기기 정보로 요약한다."""
    ua = str(user_agent or '').strip()
    if not ua:
        return ''
    os_name = next((label for token, label in _DEVICE_OS_RULES if token in ua), '기타')
    browser = next((label for token, label in _DEVICE_BROWSER_RULES if token in ua), '기타')
    kind = '모바일' if any(token in ua for token in _MOBILE_TOKENS) else 'PC'
    return '%s · %s · %s' % (kind, os_name, browser)


def _current_access():
    """지금 요청을 보낸 단말의 (IP, 기기요약, User-Agent)."""
    user_agent = _client_user_agent()
    return _client_ip(), _device_label(user_agent), user_agent


def _access_pairs(row):
    """한 기록에서 (구분, IP, 기기) 목록을 뽑는다. 값이 하나도 없으면 건너뛴다."""
    pairs = []
    for label, ip_key, device_key in (('출근', 'in_ip', 'in_device'), ('퇴근', 'out_ip', 'out_device')):
        ip = (row.get(ip_key) or '').strip()
        device = (row.get(device_key) or '').strip()
        if ip or device:
            pairs.append((label, ip, device))
    return pairs


def load_access_baseline(conn, emp_nos, before_date):
    """지정 일자 이전 기록으로 사람별 평소 IP·기기 사용 횟수를 만든다."""
    baseline = {}
    emp_nos = [str(emp_no) for emp_no in dict.fromkeys(emp_nos) if str(emp_no).strip()]
    if not emp_nos:
        return baseline

    placeholders = ','.join('?' * len(emp_nos))
    rows = conn.execute(
        """SELECT CAST(emp_no AS TEXT) AS emp_no, date,
                  in_ip, in_device, out_ip, out_device
             FROM daily_attendance
            WHERE CAST(emp_no AS TEXT) IN (%s)
              AND date < ?
              AND date >= DATE(?, '-%d day')
            ORDER BY date ASC""" % (placeholders, ACCESS_BASELINE_DAYS),
        emp_nos + [before_date, before_date]
    ).fetchall()

    for raw in rows:
        row = dict(raw)
        pairs = _access_pairs(row)
        if not pairs:
            continue
        entry = baseline.setdefault(row['emp_no'], {'ips': {}, 'devices': {}, 'records': 0, 'last_date': ''})
        for _, ip, device in pairs:
            if ip:
                entry['ips'][ip] = entry['ips'].get(ip, 0) + 1
            if device:
                entry['devices'][device] = entry['devices'].get(device, 0) + 1
        entry['records'] += 1
        entry['last_date'] = row['date']
    return baseline


def _access_anomaly(row, baseline):
    """평소 쓰던 IP·기기와 다른 접속인지 판단한다.

    비교할 과거 기록이 충분하지 않으면(신규 입사자 등) 이상으로 보지 않는다.
    """
    entry = baseline.get(str(row.get('emp_no')))
    pairs = _access_pairs(row)
    if not pairs:
        return {'is_unusual': False, 'reasons': []}
    if not entry or entry['records'] < ACCESS_BASELINE_MIN_RECORDS:
        return {'is_unusual': False, 'reasons': []}

    reasons = []
    for label, ip, device in pairs:
        if ip and entry['ips'] and ip not in entry['ips']:
            reasons.append('%s IP(%s)가 평소 접속 기록에 없습니다.' % (label, ip))
        if device and entry['devices'] and device not in entry['devices']:
            reasons.append('%s 기기(%s)가 평소 접속 기록에 없습니다.' % (label, device))
    return {'is_unusual': bool(reasons), 'reasons': reasons}


def _usual_list(counter, limit=5):
    """평소 접속정보를 많이 쓴 순서로 정렬해 화면용 목록으로 만든다."""
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{'value': value, 'count': count} for value, count in items]


def _scope_clause(user_level, emp_no, search_emp_no='', search_position=''):
    """권한과 검색조건에 맞는 WHERE 조각과 파라미터를 만든다."""
    clauses = []
    params = []

    # 레벨 4 이상(숫자가 큰 일반회원)은 본인 기록만 볼 수 있다.
    if int(user_level or 4) >= 4:
        clauses.append('CAST(a.emp_no AS TEXT) = ?')
        params.append(str(emp_no))
    else:
        if search_emp_no:
            clauses.append('CAST(a.emp_no AS TEXT) = ?')
            params.append(str(search_emp_no))
        if search_position:
            clauses.append("COALESCE(NULLIF(a.position, ''), u.position, '미지정') = ?")
            params.append(search_position)

    return (' AND ' + ' AND '.join(clauses)) if clauses else '', params


def _shift_month(year, month, delta):
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _normalize_month(value):
    if value and re.fullmatch(r'\d{4}-\d{2}', value):
        return value
    return datetime.now().strftime('%Y-%m')


def _seconds_of(time_text):
    if not time_text:
        return None
    parts = str(time_text).split(':')
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        second = int(parts[2]) if len(parts) > 2 else 0
    except (TypeError, ValueError):
        return None
    return hour * 3600 + minute * 60 + second


def _format_clock(seconds):
    if seconds is None:
        return '-'
    seconds = int(round(seconds))
    return '%02d:%02d' % (seconds // 3600, (seconds % 3600) // 60)


def _format_hours(hours):
    if hours is None:
        return '-'
    total_minutes = int(round(float(hours) * 60))
    return '%d시간 %d분' % (total_minutes // 60, total_minutes % 60)


def _work_hours(row):
    start = _seconds_of(row.get('clock_in_time'))
    end = _seconds_of(row.get('clock_out_time'))
    if start is None or end is None or end <= start:
        return None
    return (end - start) / 3600.0


def _weekday_label(date_text):
    try:
        return WEEKDAY_LABELS[datetime.strptime(date_text, '%Y-%m-%d').weekday()]
    except (TypeError, ValueError):
        return '-'


def _weekday_key(date_text):
    """개인정보 수정창의 근무시간과 같은 규칙(1=월 ~ 5=금, 토·일은 None)."""
    try:
        index = datetime.strptime(date_text, '%Y-%m-%d').weekday()
    except (TypeError, ValueError):
        return None
    return index + 1 if index <= 4 else None


def load_work_schedules(conn):
    """{(사번, 요일): {start, end, is_off}} 형태로 전 직원 근무시간을 한 번에 읽는다."""
    schedules = {}
    for row in conn.execute(
        'SELECT emp_no, weekday, start_time, end_time, is_off FROM user_work_schedule'
    ).fetchall():
        schedules[(str(row['emp_no']), int(row['weekday']))] = {
            'start': row['start_time'] or None,
            'end': row['end_time'] or None,
            'is_off': bool(row['is_off']),
        }
    return schedules


def _schedule_of(schedules, row):
    weekday = _weekday_key(row.get('date'))
    if weekday is None:
        return None
    return (schedules or {}).get((str(row.get('emp_no')), weekday))


def _expected_start(schedules, row):
    """그 사람의 그 요일 기준 출근시각. 휴무이거나 주말이면 None(지각 판정 제외)."""
    weekday = _weekday_key(row.get('date'))
    if weekday is None:
        return None
    plan = _schedule_of(schedules, row)
    if plan is None:
        return WORK_START_STANDARD
    if plan['is_off']:
        return None
    return (plan['start'] + ':00') if plan['start'] else WORK_START_STANDARD


def _plan_text(schedules, row):
    """상세 명단에 보여줄 '09:00~18:00' 형태의 기준 근무시간."""
    weekday = _weekday_key(row.get('date'))
    if weekday is None:
        return '주말'
    plan = _schedule_of(schedules, row)
    if plan is None:
        return '%s~%s (기본)' % (WORK_START_STANDARD[:5], WORK_END_STANDARD[:5])
    if plan['is_off']:
        return '휴무'
    return '%s~%s' % (plan['start'] or WORK_START_STANDARD[:5], plan['end'] or WORK_END_STANDARD[:5])


def _is_late(row, schedules=None):
    start = _seconds_of(row.get('clock_in_time'))
    expected = _expected_start(schedules, row)
    if start is None or expected is None:
        return False
    return start > _seconds_of(expected)


def _is_early_leave(row):
    return (row.get('status') or '') == '조퇴'


def _is_checked_out(row):
    return bool(row.get('clock_out_time')) and not _is_early_leave(row)


def _summarize_rows(rows, schedules=None):
    """공통 집계: 인원수 / 지각 / 평균 출근시각 / 평균 근무시간."""
    checked_out = sum(1 for r in rows if _is_checked_out(r))
    early = sum(1 for r in rows if _is_early_leave(r))
    working = sum(1 for r in rows if not r.get('clock_out_time'))
    late = sum(1 for r in rows if _is_late(r, schedules))

    in_seconds = [_seconds_of(r.get('clock_in_time')) for r in rows]
    in_seconds = [s for s in in_seconds if s is not None]
    avg_in = sum(in_seconds) / len(in_seconds) if in_seconds else None

    hours = [_work_hours(r) for r in rows]
    hours = [h for h in hours if h is not None]
    avg_hours = sum(hours) / len(hours) if hours else None

    return {
        'total': len(rows),
        'checked_out': checked_out,
        'early': early,
        'working': working,
        'late': late,
        'members': len(set(r.get('emp_no') for r in rows)),
        'avg_in_text': _format_clock(avg_in),
        'avg_hours': avg_hours,
        'avg_hours_text': _format_hours(avg_hours),
        'total_hours': sum(hours) if hours else 0.0,
    }


def _period_stats(conn, scope_sql, scope_params, period_expr, since_date, limit):
    """월별/분기별 공통 집계 쿼리."""
    rows = conn.execute('''
        SELECT %s AS period,
               COUNT(*) AS total,
               COUNT(DISTINCT a.date) AS work_days,
               COUNT(DISTINCT CAST(a.emp_no AS TEXT)) AS members,
               SUM(CASE WHEN a.clock_out_time IS NOT NULL AND a.status <> '조퇴' THEN 1 ELSE 0 END) AS checked_out,
               SUM(CASE WHEN a.status = '조퇴' THEN 1 ELSE 0 END) AS early,
               SUM(CASE WHEN a.clock_out_time IS NULL THEN 1 ELSE 0 END) AS working,
               SUM(%s) AS late,
               AVG(STRFTIME('%%s', '2000-01-01 ' || a.clock_in_time)
                   - STRFTIME('%%s', '2000-01-01 00:00:00')) AS avg_in_seconds,
               AVG(%s) AS avg_hours,
               COALESCE(SUM(%s), 0) AS total_hours
        %s
        WHERE a.date >= ? %s
        GROUP BY period
        ORDER BY period DESC
        LIMIT ?
    ''' % (period_expr, LATE_SQL, WORK_HOURS_SQL, WORK_HOURS_SQL, STATS_FROM, scope_sql),
        [WORK_START_STANDARD, since_date] + scope_params + [limit]).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        item['avg_in_text'] = _format_clock(item.pop('avg_in_seconds', None))
        item['avg_hours_text'] = _format_hours(item.get('avg_hours'))
        item['total_hours_text'] = _format_hours(item.get('total_hours'))
        result.append(item)
    return result


def _empty_period(period):
    """기록이 없는 기간도 그래프/표에서 빠지지 않도록 0으로 채운다."""
    return {
        'period': period, 'total': 0, 'work_days': 0, 'members': 0,
        'checked_out': 0, 'early': 0, 'working': 0, 'late': 0,
        'avg_in_text': '-', 'avg_hours': None, 'avg_hours_text': '-',
        'total_hours': 0, 'total_hours_text': '-',
    }


def _fill_periods(stats, period_keys):
    """조회된 집계를 기간 순서(최신순)에 맞춰 빠짐없이 채운다."""
    found = {item['period']: item for item in stats}
    return [found.get(key) or _empty_period(key) for key in reversed(period_keys)]


def _today_timeline(conn, user_level, emp_no, search_emp_no, search_position, today, today_rows, schedules):
    """오늘 조직원 전체의 출근~퇴근 구간을 시간축 위에 올린 그래프 데이터를 만든다."""
    where = ["status = '승인'", "emp_no IS NOT NULL", "emp_no != 'admin'"]
    params = []
    if int(user_level or 4) >= 4:
        where.append('CAST(emp_no AS TEXT) = ?')
        params.append(str(emp_no))
    else:
        if search_emp_no:
            where.append('CAST(emp_no AS TEXT) = ?')
            params.append(str(search_emp_no))
        if search_position:
            where.append("COALESCE(NULLIF(position, ''), '미지정') = ?")
            params.append(search_position)

    members = conn.execute('''
        SELECT CAST(emp_no AS TEXT) AS emp_no,
               COALESCE(name, CAST(emp_no AS TEXT)) AS name,
               COALESCE(NULLIF(position, ''), '미지정') AS position,
               COALESCE(level, 999) AS level
        FROM users
        WHERE %s
        ORDER BY COALESCE(level, 999) ASC, name ASC
    ''' % ' AND '.join(where), params).fetchall()

    # 한 사람이 같은 날 여러 건이면 가장 이른 출근 기록을 대표로 쓴다.
    record_by_emp = {}
    for row in sorted(today_rows, key=lambda r: r.get('clock_in_time') or ''):
        record_by_emp.setdefault(str(row['emp_no']), row)

    now_seconds = _seconds_of(datetime.now().strftime('%H:%M:%S'))

    # 시간축은 실제 기록을 감싸되 최소 08~19시는 항상 보이게 잡는다.
    today_weekday = _weekday_key(today)
    marks = [now_seconds]
    for row in record_by_emp.values():
        for key in ('clock_in_time', 'clock_out_time'):
            value = _seconds_of(row.get(key))
            if value is not None:
                marks.append(value)
    if today_weekday is not None:
        for member in members:
            plan = (schedules or {}).get((member['emp_no'], today_weekday))
            if plan and not plan['is_off']:
                for key in ('start', 'end'):
                    value = _seconds_of(plan[key])
                    if value is not None:
                        marks.append(value)
    start_hour = max(0, min(8, min(marks) // 3600))
    end_hour = min(24, max(19, -(-max(marks) // 3600) + 1))
    if end_hour <= start_hour:
        end_hour = min(24, start_hour + 1)
    axis_start, axis_span = start_hour * 3600, (end_hour - start_hour) * 3600

    def percent(seconds):
        return round(max(0.0, min(100.0, (seconds - axis_start) / axis_span * 100)), 3)

    rows = []
    present = 0
    for member in members:
        record = record_by_emp.get(member['emp_no'])
        item = {
            'emp_no': member['emp_no'],
            'name': member['name'],
            'position': member['position'],
            'state': 'absent',
            'in_text': '-',
            'out_text': '-',
            'range_text': '미출근',
            'left': 0,
            'width': 0,
            'plan_left': None,
            'plan_width': 0,
            'plan_text': '미설정',
            'is_late': False,
        }

        # 개인정보 수정창에서 설정한 오늘 요일의 기준 근무시간을 옅은 구간으로 깔아준다.
        plan = (schedules or {}).get((member['emp_no'], today_weekday)) if today_weekday else None
        if today_weekday is None:
            item['plan_text'] = '주말'
        elif plan is None:
            item['plan_text'] = '%s~%s (기본)' % (WORK_START_STANDARD[:5], WORK_END_STANDARD[:5])
            plan = {'start': WORK_START_STANDARD[:5], 'end': WORK_END_STANDARD[:5], 'is_off': False}
        elif plan['is_off']:
            item['plan_text'] = '휴무'
        else:
            item['plan_text'] = '%s~%s' % (plan['start'] or WORK_START_STANDARD[:5],
                                           plan['end'] or WORK_END_STANDARD[:5])
        if plan and not plan['is_off']:
            plan_start = _seconds_of(plan['start'] or WORK_START_STANDARD)
            plan_end = _seconds_of(plan['end'] or WORK_END_STANDARD)
            if plan_start is not None and plan_end is not None and plan_end > plan_start:
                item['plan_left'] = percent(plan_start)
                item['plan_width'] = max(1.2, percent(plan_end) - percent(plan_start))

        start = _seconds_of(record.get('clock_in_time')) if record else None
        if start is not None:
            present += 1
            item['is_late'] = _is_late(record, schedules)
            end = _seconds_of(record.get('clock_out_time'))
            if (record.get('status') or '') == '조퇴':
                item['state'] = 'early'
            elif end is None:
                item['state'] = 'working'
            else:
                item['state'] = 'done'
            close = end if end is not None else max(now_seconds, start)
            item['in_text'] = str(record.get('clock_in_time'))[:5]
            item['out_text'] = str(record.get('clock_out_time'))[:5] if end is not None else '근무중'
            item['range_text'] = '%s ~ %s' % (item['in_text'], item['out_text'])
            item['left'] = percent(start)
            # 기록 간격이 짧아도 막대가 사라지지 않도록 최소 폭을 준다.
            item['width'] = max(1.2, percent(close) - percent(start))
        rows.append(item)

    ticks = []
    hour = start_hour
    while hour <= end_hour:
        ticks.append({'label': '%02d시' % hour, 'left': percent(hour * 3600)})
        hour += 2

    return {
        'day': today,
        'weekday': _weekday_label(today),
        'rows': rows,
        'ticks': ticks,
        'now_left': percent(now_seconds) if axis_start <= now_seconds <= axis_start + axis_span else None,
        'now_text': _format_clock(now_seconds),
        'total': len(rows),
        'present': present,
        'absent': len(rows) - present,
    }


def _quarter_label(period):
    """'2026-3' 형태를 '2026년 3분기'로 바꾼다."""
    try:
        year, quarter = str(period).split('-')
        return '%s년 %d분기' % (year, int(quarter))
    except (ValueError, AttributeError):
        return str(period)


@attendance_bp.route('/attendance')
def attendance_list():
    emp_no = session.get('emp_no')
    user_level = session.get('user_level', 4)

    target_month = _normalize_month(request.args.get('month'))
    search_emp_no = (request.args.get('search_emp_no') or '').strip()
    search_position = (request.args.get('search_position') or '').strip()

    conn = get_db()
    scope_sql, scope_params = _scope_clause(user_level, emp_no, search_emp_no, search_position)
    # 지각 판정 기준은 개인정보 수정창에서 각자 설정한 요일별 근무시간이다.
    schedules = load_work_schedules(conn)

    # ── 선택한 달의 원본 기록 (일별 현황 집계용)
    month_rows = [dict(r) for r in conn.execute(
        BASE_SELECT + ' WHERE a.date LIKE ? ' + scope_sql + ' ORDER BY a.date DESC, a.clock_in_time ASC',
        [target_month + '-%'] + scope_params
    ).fetchall()]

    # ── 오늘 기록 (요약 카드용)
    today = datetime.now().strftime('%Y-%m-%d')
    today_rows = [dict(r) for r in conn.execute(
        BASE_SELECT + ' WHERE a.date = ? ' + scope_sql,
        [today] + scope_params
    ).fetchall()]

    today_summary = _summarize_rows(today_rows, schedules)
    month_summary = _summarize_rows(month_rows, schedules)
    month_summary['work_days'] = len(set(r['date'] for r in month_rows))

    # ── 일별 현황 (직급별이 아닌 날짜별 집계)
    daily_map = {}
    for row in month_rows:
        daily_map.setdefault(row['date'], []).append(row)

    daily_rows = []
    for day in sorted(daily_map.keys(), reverse=True):
        summary = _summarize_rows(daily_map[day], schedules)
        summary['day'] = day
        summary['weekday'] = _weekday_label(day)
        daily_rows.append(summary)

    # ── 월별 통계 (최근 12개월)
    now = datetime.now()
    m_year, m_month = _shift_month(now.year, now.month, -(MONTHLY_PERIODS - 1))
    monthly_stats = _period_stats(
        conn, scope_sql, scope_params,
        "STRFTIME('%Y-%m', a.date)",
        '%04d-%02d-01' % (m_year, m_month),
        MONTHLY_PERIODS,
    )
    month_keys = []
    for offset in range(MONTHLY_PERIODS):
        y, m = _shift_month(m_year, m_month, offset)
        month_keys.append('%04d-%02d' % (y, m))
    monthly_stats = _fill_periods(monthly_stats, month_keys)
    for item in monthly_stats:
        item['label'] = item['period']

    # ── 분기별 통계 (최근 8분기)
    current_quarter = (now.month + 2) // 3
    q_index = now.year * 4 + (current_quarter - 1) - (QUARTERLY_PERIODS - 1)
    q_year, q_quarter = q_index // 4, q_index % 4 + 1
    quarterly_stats = _period_stats(
        conn, scope_sql, scope_params,
        "STRFTIME('%Y', a.date) || '-' || ((CAST(STRFTIME('%m', a.date) AS INTEGER) + 2) / 3)",
        '%04d-%02d-01' % (q_year, (q_quarter - 1) * 3 + 1),
        QUARTERLY_PERIODS,
    )
    quarter_keys = []
    for offset in range(QUARTERLY_PERIODS):
        index = q_index + offset
        quarter_keys.append('%d-%d' % (index // 4, index % 4 + 1))
    quarterly_stats = _fill_periods(quarterly_stats, quarter_keys)
    for item in quarterly_stats:
        item['label'] = _quarter_label(item['period'])

    # ── 오늘 조직원 전체 출퇴근 타임라인
    timeline = _today_timeline(
        conn, user_level, emp_no, search_emp_no, search_position, today, today_rows, schedules
    )

    # ── 요약 카드 (8개를 한 행에 모두 배치하므로 라벨은 짧게 유지한다)
    month_tag = '%d월' % int(target_month[5:7])
    summary_cards = [
        {'label': '오늘 출근', 'value': today_summary['total'],
         'hint': '근무중 %d명' % today_summary['working'], 'icon': 'fa-door-open'},
        {'label': '오늘 퇴근', 'value': today_summary['checked_out'],
         'hint': '조퇴 %d명' % today_summary['early'], 'icon': 'fa-door-closed'},
        {'label': '오늘 지각', 'value': today_summary['late'],
         'hint': '개인 근무시간 기준', 'icon': 'fa-hourglass-half'},
        {'label': '오늘 평균 출근', 'value': today_summary['avg_in_text'], 'is_text': True,
         'hint': '평균 근무 %s' % today_summary['avg_hours_text'], 'icon': 'fa-clock'},
        {'label': month_tag + ' 근무일수', 'value': month_summary['work_days'],
         'hint': '기록 %d건' % month_summary['total'], 'icon': 'fa-calendar-check'},
        {'label': month_tag + ' 출근 인원', 'value': month_summary['members'],
         'hint': '연인원 %d명' % month_summary['total'], 'icon': 'fa-users'},
        {'label': month_tag + ' 평균 근무', 'value': month_summary['avg_hours_text'], 'is_text': True,
         'hint': '총 ' + _format_hours(month_summary['total_hours']), 'icon': 'fa-business-time'},
        {'label': month_tag + ' 지각·조퇴', 'value': month_summary['late'] + month_summary['early'],
         'hint': '지각 %d · 조퇴 %d' % (month_summary['late'], month_summary['early']),
         'icon': 'fa-triangle-exclamation'},
    ]

    # 관리자용 회원 및 직급 목록 (셀렉트 박스용)
    all_users = []
    all_positions = []
    if int(user_level or 4) <= 3:
        all_users = [dict(u) for u in conn.execute(
            'SELECT emp_no, name FROM users ORDER BY name'
        ).fetchall()]
        # 직급은 가나다순이 아니라 직급이 높은 순(레벨 숫자가 작을수록 상위)으로 정렬한다.
        all_positions = [row['position'] for row in conn.execute('''
            SELECT position, MIN(COALESCE(level, 999)) AS rank_level
            FROM users
            WHERE position IS NOT NULL AND position != ''
            GROUP BY position
            ORDER BY rank_level ASC, position ASC
        ''').fetchall()]

    conn.close()

    return render_template(
        'attendance.html',
        daily_rows=daily_rows,
        summary_cards=summary_cards,
        timeline=timeline,
        month_summary=month_summary,
        monthly_stats=monthly_stats,
        quarterly_stats=quarterly_stats,
        has_records=bool(month_rows),
        current_month=target_month,
        all_users=all_users,
        all_positions=all_positions,
        user_level=user_level,
        search_emp_no=search_emp_no,
        search_position=search_position,
        current_emp_no=emp_no,
        work_start_standard=WORK_START_STANDARD[:5],
    )


@attendance_bp.route('/attendance/daily-detail')
def daily_detail():
    """날짜별 현황 행을 펼쳤을 때 보여줄 그날의 출퇴근 명단."""
    day = (request.args.get('day') or '').strip()
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        return jsonify({'status': 'error', 'message': '올바른 일자를 입력해 주세요.'}), 400

    emp_no = session.get('emp_no')
    if not emp_no:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    user_level = session.get('user_level', 4)
    search_emp_no = (request.args.get('search_emp_no') or '').strip()
    search_position = (request.args.get('search_position') or '').strip()
    scope_sql, scope_params = _scope_clause(user_level, emp_no, search_emp_no, search_position)

    conn = get_db()
    schedules = load_work_schedules(conn)
    rows = [dict(r) for r in conn.execute(
        BASE_SELECT + ' WHERE a.date = ? ' + scope_sql + ' ORDER BY a.clock_in_time ASC, a.id ASC',
        [day] + scope_params
    ).fetchall()]

    # 이름을 빨갛게 표시할지 판단하려면 그 사람의 평소 접속정보가 필요하다.
    baseline = load_access_baseline(conn, [row['emp_no'] for row in rows], day)

    members = []
    for index, row in enumerate(rows, start=1):
        anomaly = _access_anomaly(row, baseline)
        members.append({
            'id': row['id'],
            'emp_no': row['emp_no'],
            'user_name': row['user_name'],
            'position': row['position'],
            'daily_rank': index,
            'clock_in_time': row['clock_in_time'] or '-',
            'clock_out_time': row['clock_out_time'] or '',
            'status': row['status'] or '-',
            'reason': row['reason'] or '-',
            'is_late': _is_late(row, schedules),
            'plan_text': _plan_text(schedules, row),
            'work_hours_text': _format_hours(_work_hours(row)),
            'has_access_info': bool(_access_pairs(row)),
            'is_unusual_access': anomaly['is_unusual'],
            'access_reasons': anomaly['reasons'],
        })

    conn.close()

    return jsonify({
        'status': 'success',
        'day': day,
        'weekday': _weekday_label(day),
        'summary': _summarize_rows(rows, schedules),
        'members': members,
    })


@attendance_bp.route('/attendance/access-info')
def access_info():
    """일별 명단에서 이름을 눌렀을 때 보여줄 접속 IP·기기 정보."""
    record_id = (request.args.get('record_id') or '').strip()
    if not record_id.isdigit():
        return jsonify({'status': 'error', 'message': '잘못된 요청입니다.'}), 400

    emp_no = session.get('emp_no')
    if not emp_no:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    user_level = session.get('user_level', 4)

    conn = get_db()
    row = conn.execute(
        BASE_SELECT + ' WHERE a.id = ?', (int(record_id),)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'status': 'error', 'message': '기록을 찾을 수 없습니다.'}), 404

    row = dict(row)
    # 레벨 4 이상(일반회원)은 본인 기록의 접속정보만 볼 수 있다.
    if int(user_level or 4) >= 4 and str(row['emp_no']) != str(emp_no):
        conn.close()
        return jsonify({'status': 'error', 'message': '다른 사람의 접속정보를 볼 권한이 없습니다.'}), 403

    baseline = load_access_baseline(conn, [row['emp_no']], row['date'])
    conn.close()

    anomaly = _access_anomaly(row, baseline)
    entry = baseline.get(str(row['emp_no'])) or {'ips': {}, 'devices': {}, 'records': 0, 'last_date': ''}

    return jsonify({
        'status': 'success',
        'record': {
            'id': row['id'],
            'emp_no': row['emp_no'],
            'user_name': row['user_name'],
            'position': row['position'],
            'date': row['date'],
            'weekday': _weekday_label(row['date']),
            'clock_in_time': row['clock_in_time'] or '-',
            'clock_out_time': row['clock_out_time'] or '',
        },
        'access': {
            'in': {
                'time': row['clock_in_time'] or '',
                'ip': row['in_ip'] or '',
                'device': row['in_device'] or '',
                'user_agent': row['in_user_agent'] or '',
            },
            'out': {
                'time': row['clock_out_time'] or '',
                'ip': row['out_ip'] or '',
                'device': row['out_device'] or '',
                'user_agent': row['out_user_agent'] or '',
            },
        },
        'usual': {
            'records': entry['records'],
            'baseline_days': ACCESS_BASELINE_DAYS,
            'min_records': ACCESS_BASELINE_MIN_RECORDS,
            'last_date': entry.get('last_date', ''),
            'ips': _usual_list(entry['ips']),
            'devices': _usual_list(entry['devices']),
        },
        'anomaly': anomaly,
    })


@attendance_bp.route('/attendance/clock_out', methods=['POST'])
def clock_out():
    """퇴근/조퇴 처리 API"""
    emp_no = session.get('emp_no')
    data = request.json
    record_id = data.get('record_id')
    action_type = data.get('type')  # '퇴근' 또는 '조퇴'
    reason = data.get('reason', '')

    conn = get_db()
    record = conn.execute("SELECT * FROM daily_attendance WHERE id = ?", (record_id,)).fetchone()

    if not record or str(record['emp_no']) != str(emp_no):
        conn.close()
        return jsonify({"success": False, "message": "권한이 없거나 잘못된 요청입니다."}), 403

    if record['clock_out_time']:
        conn.close()
        return jsonify({"success": False, "message": "이미 퇴근 처리가 완료되었습니다."}), 400

    current_time = datetime.now().strftime('%H:%M:%S')
    out_ip, out_device, out_user_agent = _current_access()

    conn.execute(
        """UPDATE daily_attendance
              SET clock_out_time = ?, status = ?, reason = ?,
                  out_ip = ?, out_device = ?, out_user_agent = ?
            WHERE id = ?""",
        (current_time, action_type, reason, out_ip, out_device, out_user_agent, record_id)
    )
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": f"{action_type} 처리가 완료되었습니다."})


# [기존] 기록 삭제 처리 API
@attendance_bp.route('/attendance/delete', methods=['POST'])
def delete_record():
    user_level = session.get('user_level', 4)

    # [권한 체크] 권한 레벨 1, 2인 관리자만 삭제 가능
    if user_level > 2:
        return jsonify({"success": False, "message": "삭제 권한이 없습니다. (레벨 2 이상 전용)"}), 403

    data = request.json
    record_id = data.get('record_id')

    conn = get_db()
    conn.execute("DELETE FROM daily_attendance WHERE id = ?", (record_id,))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "message": "기록이 정상적으로 삭제되었습니다."})


# 🚀 [신규 통합] 메인 화면 출퇴근 버튼 처리용 API
@attendance_bp.route('/api/attendance/<action_type>', methods=['POST'])
def record_attendance(action_type):
    if 'emp_no' not in session:
        return jsonify({'status': 'error', 'message': '로그인이 필요합니다.'}), 401

    emp_no = session.get('emp_no')
    current_date = datetime.now().strftime('%Y-%m-%d')
    now_time = datetime.now().strftime('%H:%M:%S')

    conn = get_db()
    # 오늘 날짜의 출퇴근 기록 확인
    record = conn.execute("SELECT * FROM daily_attendance WHERE emp_no = ? AND date = ?", (emp_no, current_date)).fetchone()

    if action_type == 'in':
        if record and record['clock_in_time']:
            conn.close()
            return jsonify({'status': 'error', 'message': '이미 오늘의 출근 처리가 완료되었습니다.'})

        position = session.get('position', '미지정')
        in_ip, in_device, in_user_agent = _current_access()

        if not record:
            conn.execute(
                """INSERT INTO daily_attendance
                       (emp_no, date, clock_in_time, status, position,
                        in_ip, in_device, in_user_agent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (emp_no, current_date, now_time, '출근', position,
                 in_ip, in_device, in_user_agent)
            )
        else:
            conn.execute(
                """UPDATE daily_attendance
                      SET clock_in_time = ?, status = '출근',
                          in_ip = ?, in_device = ?, in_user_agent = ?
                    WHERE id = ?""",
                (now_time, in_ip, in_device, in_user_agent, record['id'])
            )

        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': f'{now_time} 출근 처리되었습니다.'})

    elif action_type == 'out':
        if not record or not record['clock_in_time']:
            conn.close()
            return jsonify({'status': 'error', 'message': '출근 기록이 없습니다. 먼저 출근 처리를 해주세요.'})

        if record['clock_out_time']:
            conn.close()
            return jsonify({'status': 'error', 'message': '이미 퇴근 처리가 완료되었습니다.'})

        # 근무시간 계산 로직 (시:분:초 -> 시간 단위)
        fmt = '%H:%M:%S'
        tdelta = datetime.strptime(now_time, fmt) - datetime.strptime(record['clock_in_time'], fmt)
        hours = round(tdelta.total_seconds() / 3600, 1)

        out_ip, out_device, out_user_agent = _current_access()
        conn.execute(
            """UPDATE daily_attendance
                  SET clock_out_time = ?, status = '퇴근', reason = ?,
                      out_ip = ?, out_device = ?, out_user_agent = ?
                WHERE id = ?""",
            (now_time, f"{hours}시간 근무", out_ip, out_device, out_user_agent, record['id'])
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'success', 'message': f'{now_time} 퇴근 처리되었습니다.'})

    conn.close()
    return jsonify({'status': 'error', 'message': '잘못된 요청입니다.'})
