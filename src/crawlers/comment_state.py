"""Comment outcomes shared by collectors, saved summaries and exports."""
import json
from collections import Counter


def error_reason(error):
    text = str(error) or type(error).__name__
    lower = text.lower()
    if any(s in lower for s in ('429', 'rate limit', '限流')):
        return 'rate_limit', '平台限流，请等待后再补抓'
    if any(s in lower for s in ('401', 'cookie', 'login', '登录')):
        return 'authentication', '登录会话不可用，请更新 Cookie 后补抓'
    if any(s in lower for s in ('403', 'forbidden', 'blocked')):
        return 'access_denied', '平台拒绝评论访问（403），请检查账号权限、平台验证和网络出口'
    if any(s in lower for s in ('404', '410', '不存在', 'unavailable', 'deleted')):
        return 'unavailable', '帖子或评论不可访问，无法确认评论完整性'
    if any(s in lower for s in ('timeout', '超时', 'connect', 'network', 'net::', 'proxy', 'closed', 'crash')):
        return 'network', '评论页面加载或网络异常，可稍后补抓'
    if '上限' in text:
        return 'limit', '达到本轮抓取上限，仍需补抓'
    if any(s in text for s in ('页面显示', '未加载', '未返回', '折叠')):
        return 'shortfall', '评论仍有缺口，可能未加载或不可访问'
    return 'unknown', '评论未完成，请查看日志后补抓'


def replies(post):
    value = post.get('replies_data') or []
    return json.loads(value) if isinstance(value, str) else value


def comment_summary(rows, enabled=True):
    posts = [r for r in rows if r.get('type', 'post') != 'comment']
    total = sum(r.get('type') == 'comment' for r in rows)
    total += sum(len(replies(p)) for p in posts if p.get('platform') == 'twitter')
    pending = [p for p in posts if p.get('comments_status') != 'finished'] if enabled else []
    reasons = Counter(p.get('comments_reason') or error_reason(p.get('comments_warning', ''))[0] for p in pending)
    labels = {
        'rate_limit': '平台限流', 'authentication': '需更新登录会话', 'access_denied': '评论访问被拒绝',
        'unavailable': '帖子或评论不可访问', 'network': '网络或页面加载异常',
        'limit': '达到本轮上限', 'shortfall': '评论数量或分页仍有缺口',
        'pending': '等待处理', 'unknown': '原因未明确',
    }
    return {
        'saved_posts': len(posts), 'total_comments': total, 'total_rows': len(posts) + total,
        'comments_pending': len(pending), 'comments_finished': len(posts)-len(pending) if enabled else 0,
        'comments_empty': sum(p.get('comments_empty_verified') is True and p.get('comments_status') == 'finished' for p in posts),
        'comments_unavailable': reasons['unavailable'],
        'comments_reasons': [{'code': code, 'label': labels.get(code, labels['unknown']), 'count': count}
                             for code, count in sorted(reasons.items())],
        'comments_enabled': enabled,
    }
