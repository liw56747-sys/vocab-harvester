"""一键启动脚本：启动 vocab-harvester Web UI"""

import sys
import os
import asyncio

# 确保项目根目录在 path 中
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_DIR)


def main():
    print("=" * 60)
    print("  vocab-harvester - 社交平台词库采集系统")
    print("=" * 60)

    # 检查依赖
    try:
        import uvicorn
        import fastapi
    except ImportError as e:
        print(f"\n[错误] 缺少依赖: {e}")
        print("请先运行: pip install -r requirements.txt")
        sys.exit(1)

    # 运行测试
    skip_tests = "--skip-tests" in sys.argv
    if not skip_tests:
        print("\n[*] 运行单元测试...")
        import subprocess
        test_result = subprocess.run(
            [
                sys.executable, "-m", "pytest", "tests/",
                "-v", "--tb=short",
                "--ignore=tests/test_api.py",
                "--ignore=tests/test_twitter_login.py",
            ],
            cwd=ROOT_DIR,
            capture_output=False,
        )
        if test_result.returncode != 0:
            print(f"\n[错误] 单元测试未通过，拒绝启动服务")
            print("请修复失败的测试后重试，或使用 --skip-tests 跳过测试")
            sys.exit(1)
        print("[✓] 测试全部通过\n")
    else:
        print("\n[*] 已跳过测试（--skip-tests）\n")

    # 初始化数据库
    from src.common.config import load_settings
    from src.common.database import init_db
    from pathlib import Path

    settings = load_settings()
    db_path = Path(settings.app.data_dir) / "vocab.db"

    print(f"\n[*] 初始化数据库: {db_path}")
    asyncio.run(init_db(db_path))

    # 启动 Web 服务
    host = "0.0.0.0"
    port = 8000

    # 获取本机局域网 IP
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = "（未检测到）"

    print(f"[*] 启动 Web 服务...")
    print(f"\n{'=' * 60}")
    print(f"  本机访问:   http://127.0.0.1:{port}")
    print(f"  局域网访问: http://{lan_ip}:{port}")
    print(f"  按 Ctrl+C 停止服务")
    print(f"{'=' * 60}\n")

    uvicorn.run(
        "src.api.main:app",
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
