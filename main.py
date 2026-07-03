"""vocab-harvester CLI 入口"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

# 确保项目根目录在 sys.path 中
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.common.config import load_settings
from src.common.database import init_db, close_db
from src.common.models import CrawlQuery, Platform, VocabStatus
from src.orchestrator.pipeline import Pipeline
from src.vocabulary.manager import VocabManager

console = Console()


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def cmd_run(args: argparse.Namespace) -> None:
    """执行一次采集流水线"""
    # 解析平台
    platforms = []
    if args.platform:
        for p in args.platform:
            try:
                platforms.append(Platform(p))
            except ValueError:
                console.print(f"[red]未知平台: {p}[/red]，可选: {[p.value for p in Platform]}")
                return
    else:
        platforms = list(Platform)

    # 解析关键词
    keywords = args.query if args.query else ["技术", "科技", "互联网"]

    query = CrawlQuery(
        platforms=platforms,
        keywords=keywords,
        max_results=args.max_results,
    )

    console.print(f"\n[bold cyan]开始采集流水线[/bold cyan]")
    console.print(f"  平台: {[p.value for p in platforms]}")
    console.print(f"  关键词: {keywords}")
    console.print(f"  最大条数: {args.max_results}\n")

    pipeline = Pipeline.from_config()
    stats = await pipeline.run(query)

    # 展示结果
    if stats["status"] == "success":
        console.print(f"[bold green]采集完成[/bold green]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("指标")
        table.add_column("值")
        table.add_row("状态", stats["status"])
        table.add_row("采集帖子数", str(stats["total_posts"]))
        table.add_row("提取关键词数", str(stats["total_keywords"]))
        table.add_row("词库更新数", str(stats.get("ingested_count", 0)))
        for p in stats.get("platforms", []):
            table.add_row(f"  {p['name']}", f"{p['post_count']} 条")
        console.print(table)
    else:
        console.print(f"[bold red]采集失败[/bold red]: {stats.get('error', '未知错误')}")


async def cmd_vocab(args: argparse.Namespace) -> None:
    """查看词库"""
    manager = VocabManager()
    status = VocabStatus(args.status) if args.status else None

    entries = await manager.query(
        keyword=args.search,
        limit=args.limit,
        status=status,
    )

    if not entries:
        console.print("[yellow]词库为空[/yellow]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("#", style="dim")
    table.add_column("词条")
    table.add_column("分类")
    table.add_column("频次", justify="right")
    table.add_column("评分", justify="right")
    table.add_column("候选类型")
    table.add_column("动作")
    table.add_column("平台")
    table.add_column("状态")

    for i, entry in enumerate(entries, 1):
        status_color = {"pending": "yellow", "approved": "green", "rejected": "red"}.get(entry["status"], "white")
        action_color = {
            "add_temp_kb": "green",
            "need_human_review": "yellow",
            "observe": "blue",
            "reject": "red",
        }.get(entry.get("action", ""), "white")
        table.add_row(
            str(i),
            entry["word"],
            entry["category"],
            str(entry["frequency"]),
            f"{entry['score']:.2f}",
            entry.get("candidate_type", ""),
            f"[{action_color}]{entry.get('action', '')}[/{action_color}]",
            ", ".join(entry["platforms"]),
            f"[{status_color}]{entry['status']}[/{status_color}]",
        )

    console.print(f"\n[bold]词库查询结果[/bold] ({len(entries)} 条)")
    console.print(table)


async def cmd_stats(args: argparse.Namespace) -> None:
    """查看词库统计"""
    manager = VocabManager()
    stats = await manager.get_stats()

    console.print("\n[bold cyan]词库统计[/bold cyan]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("指标")
    table.add_column("数量", justify="right")
    table.add_row("总词条", str(stats["total"]))
    table.add_row("待审核", f"[yellow]{stats['pending']}[/yellow]")
    table.add_row("已通过", f"[green]{stats['approved']}[/green]")
    table.add_row("已拒绝", f"[red]{stats['rejected']}[/red]")
    console.print(table)

    if stats["categories"]:
        console.print(f"\n分类: {', '.join(stats['categories'])}")


async def cmd_export(args: argparse.Namespace) -> None:
    """导出词库"""
    manager = VocabManager()
    status = VocabStatus(args.status) if args.status else None

    if args.format == "json":
        content = await manager.export_json(status)
    elif args.format == "csv":
        content = await manager.export_csv(status)
    elif args.format == "txt":
        content = await manager.export_txt(status)
    else:
        console.print(f"[red]不支持的格式: {args.format}[/red]")
        return

    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
        console.print(f"[green]已导出到: {args.output}[/green]")
    else:
        print(content)


async def cmd_review(args: argparse.Namespace) -> None:
    """审核词条"""
    manager = VocabManager()
    if args.action == "approve":
        ok = await manager.approve(args.word, args.category or "")
    elif args.action == "reject":
        ok = await manager.reject(args.word, args.category or "")
    else:
        console.print(f"[red]未知操作: {args.action}[/red]")
        return

    if ok:
        console.print(f"[green]已{args.action}: {args.word}[/green]")
    else:
        console.print(f"[red]操作失败，词条不存在: {args.word}[/red]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vocab-harvester",
        description="社交平台数据采集与词库构建系统",
    )
    parser.add_argument("--config", type=str, help="配置文件路径", default=None)
    parser.add_argument("--log-level", type=str, default="INFO", help="日志级别")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # run 命令
    run_parser = subparsers.add_parser("run", help="执行采集流水线")
    run_parser.add_argument("--platform", "-p", nargs="+", help="目标平台 (weibo/xiaohongshu/twitter)")
    run_parser.add_argument("--query", "-q", nargs="+", help="搜索关键词")
    run_parser.add_argument("--max-results", "-n", type=int, default=20, help="每个平台最大采集条数")

    # vocab 命令
    vocab_parser = subparsers.add_parser("vocab", help="查看词库")
    vocab_parser.add_argument("--search", "-s", help="搜索关键词")
    vocab_parser.add_argument("--status", choices=["pending", "approved", "rejected"])
    vocab_parser.add_argument("--limit", "-l", type=int, default=50)

    # stats 命令
    subparsers.add_parser("stats", help="查看词库统计")

    # export 命令
    export_parser = subparsers.add_parser("export", help="导出词库")
    export_parser.add_argument("--format", "-f", choices=["json", "csv", "txt"], default="json")
    export_parser.add_argument("--output", "-o", help="输出文件路径")
    export_parser.add_argument("--status", choices=["pending", "approved", "rejected"])

    # review 命令
    review_parser = subparsers.add_parser("review", help="审核词条")
    review_parser.add_argument("action", choices=["approve", "reject"])
    review_parser.add_argument("word", help="词条")
    review_parser.add_argument("--category", "-c", default="")

    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    setup_logging(args.log_level)

    # 加载配置
    load_settings(args.config)

    # 初始化数据库
    settings = load_settings(args.config)
    db_path = Path(settings.app.data_dir) / "vocab.db"
    await init_db(db_path)

    try:
        commands = {
            "run": cmd_run,
            "vocab": cmd_vocab,
            "stats": cmd_stats,
            "export": cmd_export,
            "review": cmd_review,
        }
        handler = commands.get(args.command)
        if handler:
            await handler(args)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
