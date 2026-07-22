"""词库存储层：SQLite CRUD 操作"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.common.database import get_db
from src.common.models import VocabEntry, VocabStatus


class VocabStorage:
    """词库 SQLite 存储管理"""

    async def upsert(self, entry: VocabEntry) -> None:
        """插入或更新词条（词+分类唯一）"""
        db = await get_db()
        now = datetime.now().isoformat()

        # 尝试查找已有词条
        cursor = await db.execute(
            "SELECT id, frequency, platforms, context_samples FROM vocabulary WHERE word = ? AND category = ?",
            (entry.word, entry.category),
        )
        existing = await cursor.fetchone()

        if existing:
            # 更新：累加频次，合并平台和上下文
            new_freq = existing["frequency"] + 1
            platforms = json.loads(existing["platforms"])
            for p in entry.platforms:
                if p not in platforms:
                    platforms.append(p)
            samples = json.loads(existing["context_samples"])
            for s in entry.context_samples:
                if s not in samples:
                    samples.append(s)

            await db.execute(
                """UPDATE vocabulary
                   SET frequency = ?, platforms = ?, context_samples = ?, last_seen = ?,
                       score = ?, candidate_type = ?, source_type = ?, evidence = ?,
                       reason = ?, action = ?, match_type = ?, task_name = ?
                   WHERE id = ?""",
                (
                    new_freq, json.dumps(platforms), json.dumps(samples), now,
                    entry.score, entry.candidate_type, entry.source_type,
                    entry.evidence, entry.reason, entry.action, entry.match_type,
                    entry.task_name, existing["id"],
                ),
            )
        else:
            # 新增
            await db.execute(
                """INSERT INTO vocabulary
                   (word, category, frequency, score, platforms, first_seen, last_seen,
                    context_samples, status, candidate_type, source_type, evidence,
                    reason, action, match_type, task_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.word,
                    entry.category,
                    entry.frequency,
                    entry.score,
                    json.dumps(entry.platforms),
                    now,
                    now,
                    json.dumps(entry.context_samples),
                    entry.status.value,
                    entry.candidate_type,
                    entry.source_type,
                    entry.evidence,
                    entry.reason,
                    entry.action,
                    entry.match_type,
                    entry.task_name,
                ),
            )

        await db.commit()

    async def query(
        self,
        keyword: str | None = None,
        category: str | None = None,
        status: VocabStatus | None = None,
        platform: str | None = None,
        action: str | None = None,
        candidate_type: str | None = None,
        score_min: float | None = None,
        score_max: float | None = None,
        task_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """多条件查询词库"""
        db = await get_db()
        conditions: list[str] = []
        params: list[Any] = []
    
        if keyword:
            conditions.append("word LIKE ?")
            params.append(f"%{keyword}%")
        if category:
            # 清洗分类参数：去除所有空白符和换行符
            sanitized_category = category.replace('\n', '').replace('\r', '').strip()
            # 使用 TRIM 函数清洗数据库字段后进行比对
            conditions.append("TRIM(REPLACE(REPLACE(category, CHAR(10), ''), CHAR(13), '')) = ?")
            params.append(sanitized_category)
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if platform:
            conditions.append("platforms LIKE ?")
            params.append(f'%"{platform}"%')
        if action:
            conditions.append("action = ?")
            params.append(action)
        if candidate_type:
            conditions.append("candidate_type = ?")
            params.append(candidate_type)
        if score_min is not None:
            conditions.append("score >= ?")
            params.append(score_min)
        if score_max is not None:
            conditions.append("score <= ?")
            params.append(score_max)
        if task_name:
            conditions.append("task_name = ?")
            params.append(task_name)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])

        cursor = await db.execute(
            f"SELECT * FROM vocabulary{where} ORDER BY score DESC, frequency DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def count(
        self,
        keyword: str | None = None,
        category: str | None = None,
        status: VocabStatus | None = None,
        platform: str | None = None,
        action: str | None = None,
        candidate_type: str | None = None,
        score_min: float | None = None,
        score_max: float | None = None,
        task_name: str | None = None,
    ) -> int:
        """统计词条总数（支持多条件筛选）"""
        db = await get_db()
        conditions: list[str] = []
        params: list[Any] = []

        if keyword:
            conditions.append("word LIKE ?")
            params.append(f"%{keyword}%")
        if category:
            # 清洗分类参数：去除所有空白符和换行符
            sanitized_category = category.replace('\n', '').replace('\r', '').strip()
            # 使用 TRIM 函数清洗数据库字段后进行比对
            conditions.append("TRIM(REPLACE(REPLACE(category, CHAR(10), ''), CHAR(13), '')) = ?")
            params.append(sanitized_category)
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if platform:
            conditions.append("platforms LIKE ?")
            params.append(f'%"{platform}"%')
        if action:
            conditions.append("action = ?")
            params.append(action)
        if candidate_type:
            conditions.append("candidate_type = ?")
            params.append(candidate_type)
        if score_min is not None:
            conditions.append("score >= ?")
            params.append(score_min)
        if score_max is not None:
            conditions.append("score <= ?")
            params.append(score_max)
        if task_name:
            conditions.append("task_name = ?")
            params.append(task_name)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        cursor = await db.execute(f"SELECT COUNT(*) as cnt FROM vocabulary{where}", params)
        row = await cursor.fetchone()
        return row["cnt"]

    # ── 定时任务历史去重 ──────────────────────────────

    async def filter_seen_posts(self, post_ids: list[str], dimension: str, since_iso: str) -> set[str]:
        """返回在指定维度、时间窗内已出现过的 post_id 集合（并顺带清理过期记录）。"""
        if not post_ids:
            return set()
        db = await get_db()
        # 清理超出保留窗口的过期记录（超期后允许重新抓取）
        await db.execute(
            "DELETE FROM scheduled_seen_posts WHERE dimension = ? AND first_seen_at < ?",
            (dimension, since_iso),
        )
        await db.commit()
        seen: set[str] = set()
        chunk_size = 400
        for i in range(0, len(post_ids), chunk_size):
            chunk = [p for p in post_ids[i:i + chunk_size] if p]
            if not chunk:
                continue
            placeholders = ",".join(["?"] * len(chunk))
            cursor = await db.execute(
                f"SELECT post_id FROM scheduled_seen_posts WHERE dimension = ? AND post_id IN ({placeholders})",
                [dimension, *chunk],
            )
            for row in await cursor.fetchall():
                seen.add(row["post_id"])
        return seen

    async def record_seen_posts(self, post_ids: list[str], dimension: str, task_id: str, seen_at: str) -> None:
        """记录本次新出现的帖子（已存在则忽略）。"""
        rows = [(pid, dimension, task_id, seen_at) for pid in post_ids if pid]
        if not rows:
            return
        db = await get_db()
        await db.executemany(
            "INSERT OR IGNORE INTO scheduled_seen_posts (post_id, dimension, task_id, first_seen_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        await db.commit()

    async def update_status(self, word: str, status: VocabStatus, category: str = "") -> bool:
        """更新词条审核状态"""
        db = await get_db()
        cursor = await db.execute(
            "UPDATE vocabulary SET status = ? WHERE word = ? AND category = ?",
            (status.value, word, category),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def delete(self, word: str, category: str = "") -> bool:
        """删除词条"""
        db = await get_db()
        cursor = await db.execute(
            "DELETE FROM vocabulary WHERE word = ? AND category = ?",
            (word, category),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def export_all(self, status: VocabStatus | None = None) -> list[dict[str, Any]]:
        """导出所有词条"""
        db = await get_db()
        if status:
            cursor = await db.execute(
                "SELECT * FROM vocabulary WHERE status = ? ORDER BY score DESC",
                (status.value,),
            )
        else:
            cursor = await db.execute("SELECT * FROM vocabulary ORDER BY score DESC")
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def get_stats(self) -> dict[str, Any]:
        """获取词库统计概览"""
        db = await get_db()

        total = (await (await db.execute("SELECT COUNT(*) as cnt FROM vocabulary")).fetchone())["cnt"]
        pending = (await (await db.execute("SELECT COUNT(*) as cnt FROM vocabulary WHERE status = 'pending'")).fetchone())["cnt"]
        approved = (await (await db.execute("SELECT COUNT(*) as cnt FROM vocabulary WHERE status = 'approved'")).fetchone())["cnt"]
        rejected = (await (await db.execute("SELECT COUNT(*) as cnt FROM vocabulary WHERE status = 'rejected'")).fetchone())["cnt"]

        cursor = await db.execute("SELECT DISTINCT category FROM vocabulary WHERE category != ''")
        categories = [row["category"] for row in await cursor.fetchall()]

        return {
            "total": total,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "categories": categories,
        }

    async def get_filter_options(self) -> dict[str, Any]:
        """获取筛选器选项（分类、候选类型、任务名、激活工作流、工作流分类映射）"""
        db = await get_db()

        cursor = await db.execute("SELECT DISTINCT category FROM vocabulary WHERE category != '' ORDER BY category")
        categories = [row["category"] for row in await cursor.fetchall()]

        cursor = await db.execute("SELECT DISTINCT candidate_type FROM vocabulary WHERE candidate_type != '' ORDER BY candidate_type")
        candidate_types = [row["candidate_type"] for row in await cursor.fetchall()]

        cursor = await db.execute("SELECT DISTINCT task_name FROM vocabulary WHERE task_name != '' ORDER BY task_name")
        task_names = [row["task_name"] for row in await cursor.fetchall()]

        # 获取已激活的工作流列表（从定时任务中提取）
        cursor = await db.execute("SELECT workflows FROM scheduled_tasks WHERE enabled = 1")
        rows = await cursor.fetchall()
        active_workflows_set = set()
        for row in rows:
            workflows_json = row["workflows"]
            if workflows_json:
                try:
                    workflows_list = json.loads(workflows_json)
                    for wf in workflows_list:
                        if isinstance(wf, dict) and "name" in wf:
                            active_workflows_set.add(wf["name"])
                except (json.JSONDecodeError, TypeError):
                    pass

        # 提取工作流分类映射（从工作流的提示词中获取）
        workflow_category_map = await self._extract_workflow_category_map(active_workflows_set)

        return {
            "categories": categories,
            "candidate_types": candidate_types,
            "task_names": task_names,
            "active_workflows": list(active_workflows_set),
            "workflow_category_map": workflow_category_map,
        }

    async def _extract_workflow_category_map(self, active_workflows: set[str]) -> dict[str, list[str]]:
        """从工作流的提示词配置中提取分类映射"""
        # 目前只支持舆情工作流，其他工作流待后续添加
        workflow_map = {}
        
        if "舆情" in active_workflows:
            # 舆情工作流的分类从其系统提示词中提取
            # 这些分类来自 xingpan.py 中的 _SYSTEM_PROMPT
            workflow_map["舆情"] = [
                "事件核心词",
                "相关主体",
                "风险观点口号",
                "隐晦影射",
                "传播扩散",
                "规避变体",
                "上下文依赖",
                "其他",
            ]
        
        # TODO: 后续添加其他工作流时，从其对应的提示词中提取分类
        # if "涉政" in active_workflows:
        #     workflow_map["涉政"] = [...]
        # if "违法" in active_workflows:
        #     workflow_map["违法"] = [...]
        # if "色情低俗" in active_workflows:
        #     workflow_map["色情低俗"] = [...]
        
        return workflow_map

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        """将数据库行转换为字典"""
        # 清洗分类字段：去除换行符和多余空白
        raw_category = row["category"] or ""
        sanitized_category = raw_category.replace('\n', '').replace('\r', '').strip()
        
        result = {
            "id": row["id"],
            "word": row["word"],
            "category": sanitized_category,  # 使用清洗后的分类
            "frequency": row["frequency"],
            "score": row["score"],
            "platforms": json.loads(row["platforms"]),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "context_samples": json.loads(row["context_samples"]),
            "status": row["status"],
        }
        # 新列可能不存在于旧数据库，安全读取
        for col in ("candidate_type", "source_type", "evidence", "reason", "action", "match_type", "task_name"):
            try:
                result[col] = row[col]
            except (IndexError, KeyError):
                result[col] = ""
        return result
