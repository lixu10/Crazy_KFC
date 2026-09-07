"""服务器部署入口：waitress 单进程多线程。

重要：登录会话、每账号任务、自动重登用的内存密码都只存在于进程内，
因此本服务必须单进程运行——请勿改为 gunicorn 多 worker（会各自持有状态、
互相“顶掉”会话）。waitress 单进程内用多线程即可并发处理多用户。
"""
from __future__ import annotations

import os

from waitress import serve

from kfc_college import create_app

HOST = os.getenv("BIND_HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8642"))
THREADS = int(os.getenv("WAITRESS_THREADS", "8"))


def main() -> None:
    app = create_app()
    print(f"KFC大学选课助手已启动：http://{HOST}:{PORT}/ （多用户 · waitress）")
    serve(app, host=HOST, port=PORT, threads=THREADS)


if __name__ == "__main__":
    main()
