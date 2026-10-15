"""One schedule parser for validation, preview and execution. Standard cron weekdays."""
from datetime import datetime
from zoneinfo import ZoneInfo
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.combining import OrTrigger

NAMES = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']


def weekdays(value):
    def number(s):
        if s.lower() in NAMES:
            return NAMES.index(s.lower())
        n = int(s)
        if not 0 <= n <= 7:
            raise ValueError('星期必须为 0–7 或 sun–sat；0、7 均为周日')
        return n
    values = set()
    for part in value.split(','):
        span, *steps = part.split('/')
        if len(steps) > 1:
            raise ValueError('无效星期步长')
        step = int(steps[0]) if steps else 1
        if step < 1 or step > 7:
            raise ValueError('星期步长必须为 1–7')
        if span == '*':
            start, end = 0, 6
        elif '-' in span:
            a, b = span.split('-')
            start, end = number(a), number(b)
        else:
            start = number(span)
            end = 7 if steps else start
        if end < start:
            raise ValueError('星期范围不可倒序，请使用逗号列举')
        values.update(n % 7 for n in range(start, end + 1, step))
    return ','.join(NAMES[n] for n in sorted(values))


def trigger(expression, timezone='Asia/Shanghai', dialect='unix'):
    if dialect not in ('unix','apscheduler'):
        raise ValueError('不支持的星期编号规则')
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError('定时表达式必须包含 分、时、日、月、周 五项')
    zone = ZoneInfo(timezone)
    minute, hour, day, month, dow = parts
    weekday = weekdays(dow) if dialect == 'unix' else dow
    args = dict(minute=minute, hour=hour, month=month, timezone=zone)
    # Unix cron: when both fields are restricted, day-of-month OR day-of-week.
    if dialect == 'unix' and day != '*' and dow != '*':
        return OrTrigger([CronTrigger(day=day, **args), CronTrigger(day_of_week=weekday, **args)])
    return CronTrigger(day=day, day_of_week=weekday, **args)


def describe(expression, timezone='Asia/Shanghai', dialect='unix', now=None):
    t = trigger(expression, timezone, dialect)
    next_time = t.get_next_fire_time(None, now or datetime.now(ZoneInfo(timezone)))
    parts = expression.split()
    label = expression
    if parts[0].isdigit() and parts[1].isdigit() and parts[2:4] == ['*', '*']:
        if parts[4] == '*':
            days = '每天'
        else:
            if dialect == 'unix':
                names = weekdays(parts[4]).split(',')
            else:
                # Only simplify plain legacy numbers/names; keep complex expressions explicit.
                names = [(['mon','tue','wed','thu','fri','sat','sun'][int(parts[4])]
                          if parts[4].isdigit() else parts[4])]
            labels = dict(zip(NAMES, ['周日','周一','周二','周三','周四','周五','周六']))
            days = '、'.join(labels.get(n, n) for n in names)
        label = f'{days} {int(parts[1]):02d}:{int(parts[0]):02d}'
    return {'description': label, 'timezone': timezone,
            'next_run_at': next_time.isoformat() if next_time else None,
            'legacy_weekdays': dialect != 'unix'}
