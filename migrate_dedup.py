from src.common.database import get_db


def migrate():
    print("🔍 正在迁移数据库...")
    with get_db() as conn:
        # 1. 添加新字段
        try:
            conn.execute(""
            ALTER TABLE posts 
            ADD COLUMN post_id TEXT UNIQUE,
            ADD COLUMN keywords TEXT NOT NULL,
            ADD COLUMN user_id TEXT NOT NULL
            """)
            print("✅ 已添加 posts 表新字段")
        except Exception as e:
            if "duplicate column" in str(e):
                print("⚠️ 字段已存在，跳过")
            else:
                raise e

        # 2. 创建索引
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_keywords ON posts(keywords)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_user_id ON posts(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts(created_at)")
            print("✅ 已创建索引")
        except Exception as e:
            print(f"⚠️ 索引创建失败: {e}")

        conn.commit()
        print("✅ 数据库迁移完成")

if __name__ == "__main__":
    migrate()