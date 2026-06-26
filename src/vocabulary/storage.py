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
                       reason = ?, action = ?, match_type = ?
                   WHERE id = ?""",
                (
                    new_freq, json.dumps(platforms), json.dumps(samples), now,
                    entry.score, entry.candidate_type, entry.source_type,
                    entry.evidence, entry.reason, entry.action, entry.match_type,
                    existing["id"],
                ),
            )
        else:
            # 新增
            await db.execute(
                """INSERT INTO vocabulary
                   (word, category, frequency, score, platforms, first_seen, last_seen,
                    context_samples, status, candidate_type, source_type, evidence,
                    reason, action, match_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )

        await db.commit()

    async def query(
        self,
        keyword: str | None = None,
        category: str | None = None,
        status: VocabStatus | None = None,
        platform: str | None = None,
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
            conditions.append("category = ?")
            params.append(category)
        if status:
            conditions.append("status = ?")
            params.append(status.value)
        if platform:
            conditions.append("platforms LIKE ?")
            params.append(f'%"{platform}"%')

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.extend([limit, offset])

        cursor = await db.execute(
            f"SELECT * FROM vocabulary{where} ORDER BY score DESC, frequency DESC LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def count(self, status: VocabStatus | None = None) -> int:
        """统计词条总数"""
        db = await get_db()
        if status:
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM vocabulary WHERE status = ?", (status.value,))
        else:
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM vocabulary")
        row = await cursor.fetchone()
        return row["cnt"]

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

    @staticmethod
    def _row_to_dict(row) -> dict[str, Any]:
        """将数据库行转换为字典"""
        result = {
            "id": row["id"],
            "word": row["word"],
            "category": row["category"],
            "frequency": row["frequency"],
            "score": row["score"],
            "platforms": json.loads(row["platforms"]),
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "context_samples": json.loads(row["context_samples"]),
            "status": row["status"],
        }
        # 新列可能不存在于旧数据库，安全读取
        for col in ("candidate_type", "source_type", "evidence", "reason", "action", "match_type"):
            try:
                result[col] = row[col]
            except (IndexError, KeyError):
                result[col] = ""
        return result
