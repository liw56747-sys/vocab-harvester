"""Twitter (twikit) 登录交互式测试

此脚本需要用户手动输入凭据，不适合自动化测试。
运行方式: python -m pytest tests/test_twitter_login.py -v -m interactive -s
"""

import asyncio
import traceback
from pathlib import Path

import pytest

pytestmark = pytest.mark.interactive


@pytest.mark.skip(reason="交互式测试，需手动运行: python -m pytest tests/test_twitter_login.py -v -m interactive -s --no-header")
async def test_twitter_login():
    """测试 twikit Twitter 登录流程"""
    username = input("\nTwitter 用户名 (@后面的，如 elonmusk): ").strip()
    email = input("邮箱地址: ").strip()
    password = input("密码: ").strip()

    assert all([username, email, password]), "所有字段都必须填写"

    from twikit import Client

    client = Client("en-US")
    await client.login(
        auth_info_1=username,
        auth_info_2=email,
        password=password,
    )

    cookie_path = Path(__file__).parent.parent / ".twitter_cookies.json"
    client.save_cookies(str(cookie_path))
    assert cookie_path.exists(), "Cookie 文件应已创建"
