from src.common.database import get_db

def migrate():
    print("🔍 正在迁移数据库...")
    with get_db() as conn:
        # 1. 添加 task_type 字段（如果不存在）
        try:
            conn.execute("""
            ALTER TABLE scheduled_tasks 
            ADD COLUMN task_type TEXT DEFAULT 'standard'
            """)
            print("✅ 已添加 scheduled_tasks.task_type 字段")
        except Exception as e:
            if "duplicate column" in str(e):
                print("⚠️ task_type 字段已存在，跳过")
            else:
                raise e

        # 2. 创建黑词表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL,
            source TEXT NOT NULL,
            task_name TEXT NOT NULL,
            detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        conn.commit()
        print("✅ 已创建 blacklist 表")

if __name__ == "__main__":
    migrate()