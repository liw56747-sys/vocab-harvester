"""
平台抓取参数配置 — 每个平台独立配置组，所有等待时间从此处读取，严禁在业务代码中硬编码。

两套配置：twitter_config / reddit_config
公共逻辑：
  - 达到目标数量 → 立即停止
  - 连续 2 次滚动无新增 → 触底停止
  - 滚动轮数仅为保底硬上限
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformTiming:
    """平台时间/轮次参数（不可变对象，防止运行时误改）"""

    # ── 搜索页 ──
    search_initial_wait: float          # 搜索页初始加载等待（秒）
    search_scroll_min: float            # 搜索页每轮滚动等待下限
    search_scroll_max: float            # 搜索页每轮滚动等待上限
    search_load_timeout: int            # 搜索页推文加载超时（毫秒）
    search_max_rounds: int              # 搜索页最大滚动轮数（硬性保底）
    search_expand_wait: float           # 搜索页"展开全文"后等待（秒）

    # ── 评论页 ──
    comment_scroll_min: float           # 评论页每轮滚动等待下限
    comment_scroll_max: float           # 评论页每轮滚动等待上限
    comment_max_rounds: int             # 评论页单帖最大滚动轮数
    comment_max_replies: int            # 评论页单帖最多抓取评论数
    comment_more_wait: float            # 评论页"更多回复"后等待（秒）
    comment_expand_wait: float          # 评论页"展开全文"后等待（秒）

    # ── 用户主页（仅 Twitter 使用）──
    user_initial_wait: float            # 用户主页初始加载等待（秒）
    user_scroll_min: float              # 用户主页每轮滚动等待下限
    user_scroll_max: float              # 用户主页每轮滚动等待上限
    user_expand_wait: float             # 用户主页"展开全文"后等待（秒）
    user_max_rounds: int                # 用户主页最大滚动轮数

    # ── 通用交互 ──
    click_more_wait: float            # 点击"加载更多"类按钮后等待（秒）

    # ── 超时 ──
    single_keyword_timeout: int         # 单关键词整体超时（秒）
    batch_max_timeout: int              # 批量搜索整体超时上限（秒）

    def scroll_wait(self, page_type: str = "search") -> float:
        """返回指定页面类型的随机滚动等待值"""
        if page_type == "search":
            return random.uniform(self.search_scroll_min, self.search_scroll_max)
        elif page_type == "comment":
            return random.uniform(self.comment_scroll_min, self.comment_scroll_max)
        elif page_type == "user":
            return random.uniform(self.user_scroll_min, self.user_scroll_max)
        else:
            return random.uniform(self.search_scroll_min, self.search_scroll_max)


# ═══════════════════════════════════════════════════════════
#  Twitter / X  配置
# ═══════════════════════════════════════════════════════════
twitter_config = PlatformTiming(
    # 搜索页
    search_initial_wait=1.5,
    search_scroll_min=1.5,
    search_scroll_max=2.5,
    search_load_timeout=15000,       # 15 秒
    search_max_rounds=30,
    search_expand_wait=0.5,

    # 评论页
    comment_scroll_min=2.5,
    comment_scroll_max=4.0,
    comment_max_rounds=8,
    comment_max_replies=50,
    comment_more_wait=0.5,
    comment_expand_wait=0.5,

    # 用户主页
    user_initial_wait=1.5,
    user_scroll_min=1.5,
    user_scroll_max=2.5,
    user_expand_wait=0.5,
    user_max_rounds=20,

    # 通用交互
    click_more_wait=0.5,

    # 超时
    single_keyword_timeout=1800,     # 30 分钟（仅兆底，真正的效率控制由滚动轮数和目标条数负责）
    batch_max_timeout=3600,          # 60 分钟
)


# ═══════════════════════════════════════════════════════════
#  Reddit  配置
# ═══════════════════════════════════════════════════════════
reddit_config = PlatformTiming(
    # 搜索页（Reddit JSON API 翻页间隔）
    search_initial_wait=1.0,
    search_scroll_min=0.8,
    search_scroll_max=1.5,
    search_load_timeout=10000,       # 10 秒
    search_max_rounds=20,
    search_expand_wait=0.0,          # Reddit 无"展开全文"

    # 评论页
    comment_scroll_min=1.0,
    comment_scroll_max=1.8,
    comment_max_rounds=6,
    comment_max_replies=30,
    comment_more_wait=0.5,
    comment_expand_wait=0.0,         # Reddit 无"展开全文"

    # 用户主页（Reddit 不使用 Playwright，保留字段兼容性）
    user_initial_wait=1.0,
    user_scroll_min=0.8,
    user_scroll_max=1.5,
    user_expand_wait=0.0,
    user_max_rounds=20,

    # 通用交互
    click_more_wait=0.5,

    # 超时
    single_keyword_timeout=1200,     # 20 分钟（仅兆底，真正的效率控制由滚动轮数和目标条数负责）
    batch_max_timeout=2400,          # 40 分钟
)


# ── 平台名称 → 配置 映射 ──────────────────────────────────

PLATFORM_TIMING_MAP: dict[str, PlatformTiming] = {
    "twitter": twitter_config,
    "reddit": reddit_config,
}


def get_timing(platform: str) -> PlatformTiming:
    """根据平台名获取对应时间配置，未知平台默认返回 twitter_config"""
    return PLATFORM_TIMING_MAP.get(platform, twitter_config)
