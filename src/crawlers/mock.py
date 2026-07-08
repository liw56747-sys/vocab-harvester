"""Mock 爬虫：用于开发和测试，生成模拟的社交帖子数据"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta

from src.common.models import CrawlQuery, ParsedPost, Platform
from src.crawlers.base import BaseCrawler


# 模拟帖子模板
_SAMPLE_TEMPLATES = {
    Platform.WEIBO: [
        "今天试用了{keyword}相关的新产品，体验非常好！推荐给大家 #{keyword}",
        "关于{keyword}的最新趋势：越来越多的品牌开始关注这个领域，市场前景广阔",
        "分享一下我对{keyword}的看法，这个技术正在改变我们的生活方式",
        "{keyword}真的是今年最值得关注的技术之一，各大厂商都在布局",
        "刚刚看到一个关于{keyword}的深度分析，数据非常有说服力",
    ],
    Platform.XIAOHONGSHU: [
        "姐妹们！这个{keyword}真的太绝了，必须安利给你们！#好物分享",
        "最近种草了{keyword}，用了一周来交作业，效果真的很惊喜",
        "干货分享｜关于{keyword}的选购指南，看完不踩坑",
        "{keyword}测评来了！对比了5个品牌，这个性价比最高",
        "被{keyword}圈粉了！颜值高还好用，强烈推荐",
    ],
    Platform.TWITTER: [
        "Just tried {keyword} and I'm blown away by the results. Game changer! #{keyword}",
        "The latest developments in {keyword} are fascinating. Here's what I learned:",
        "Excited to share my thoughts on {keyword} - this is the future of the industry",
        "{keyword} adoption is growing rapidly. The data speaks for itself.",
        "Interesting thread on {keyword}. What do you all think about the potential?",
    ],
    Platform.REDDIT: [
        "Has anyone else noticed the rise of {keyword}? Really interesting trend.",
        "Deep dive into {keyword} - here's my analysis after months of research",
        "AMA: I've been studying {keyword} for 5 years, ask me anything",
        "The {keyword} community is growing fast. Here are some resources to get started.",
        "Unpopular opinion: {keyword} is overhyped and here's why",
    ],
}


class MockCrawler(BaseCrawler):
    """模拟爬虫，生成随机测试数据"""

    def __init__(self, platform: Platform, rate_limit: int = 5):
        super().__init__(rate_limit)
        self.platform = platform.value

    async def fetch(self, query: CrawlQuery) -> list[ParsedPost]:
        """生成模拟帖子数据"""
        posts: list[ParsedPost] = []
        # 使用当前爬虫实例对应的平台模板
        platform_enum = Platform(self.platform)
        templates = _SAMPLE_TEMPLATES.get(platform_enum, _SAMPLE_TEMPLATES[Platform.WEIBO])

        for i in range(query.max_results):
            keyword = random.choice(query.keywords) if query.keywords else "测试"
            template = random.choice(templates)
            content = template.format(keyword=keyword)

            posts.append(ParsedPost(
                platform=self.platform,
                post_id=str(uuid.uuid4())[:12],
                content=content,
                author=f"user_{random.randint(1000, 9999)}",
                published_at=datetime.now() - timedelta(hours=random.randint(0, 72)),
                metrics={
                    "likes": random.randint(0, 10000),
                    "comments": random.randint(0, 500),
                    "shares": random.randint(0, 2000),
                },
                tags=[keyword],
                raw_data={"source": "mock", "index": i},
            ))

        return posts
