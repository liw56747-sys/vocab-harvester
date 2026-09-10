"""搜索结果展开：CSV、Excel、行数统计使用同一份评论行。"""
from __future__ import annotations

import json


def search_export_rows(posts: list[dict]) -> list[dict]:
    rows = []
    for post in posts:
        row = dict(post)
        row.setdefault("type", "post")
        platform = row.get("platform", "")
        if platform == "twitter":
            row["post_id"] = post.get("tweet_id") or post.get("post_id", "")
            row["author"] = post.get("author_name") or post.get("author", "")
        elif platform == "reddit":
            if row["type"] == "post":
                row["content"] = (post.get("title", "") + "\n" + post.get("content", "")).strip()
            else:
                row["author"] = post.get("commenter") or post.get("author", "")
        rows.append(row)
        if platform != "twitter" or row["type"] == "comment":
            continue
        raw = post.get("replies_data") or []
        replies = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(replies, list):
            raise ValueError("Twitter 评论数据格式无效，无法导出")
        seen = set()
        for reply in replies:
            rid = reply.get("tweet_id", "")
            if rid and rid in seen:
                continue
            seen.add(rid)
            author = reply.get("display_name") or reply.get("author", "")
            rows.append({
                **reply, "keyword": post.get("keyword", ""),
                "platform": "twitter", "type": "comment", "post_id": rid,
                "parent_id": row["post_id"], "author": author, "commenter": author,
                "url": reply.get("url") or (f"https://x.com/i/status/{rid}" if rid else ""),
            })
    return rows


def comment_warnings(posts: list[dict]) -> list[str]:
    return [
        f"{p.get('platform', '')} {p.get('url') or p.get('tweet_id') or p.get('post_id', '')}: {p['comments_warning']}"
        for p in posts if p.get("comments_warning")
    ]


def search_count_result(posts: list[dict], requested: int, platform: str, keyword: str) -> dict:
    actual = sum(p.get('type', 'post') != 'comment' for p in posts)
    return {'platform': platform, 'keyword': keyword, 'requested_count': requested,
            'actual_count': actual, 'missing_count': max(0, requested - actual),
            'status': 'fulfilled' if actual >= requested else 'shortfall'}


def search_count_warning(result: dict) -> str:
    return (f"「{result['keyword']}」{result['platform']}: 目标 {result['requested_count']} 条主帖，"
            f"实际 {result['actual_count']} 条，缺少 {result['missing_count']} 条。"
            "当前可访问搜索结果未达到目标（可能结果不足、加载中断或超时）；评论另计。")
