"""WSGI 入口：供 waitress / 其它单进程 WSGI 服务器加载。

会话与后台任务都是内存态，务必保持单进程（单进程内可多线程）。
"""
from kfc_college import create_app

app = create_app()

if __name__ == "__main__":
    # 仅供快速试跑；正式部署请用 serve.py（waitress）或其它 WSGI 服务器。
    app.run(host="127.0.0.1", port=8642, debug=False)
