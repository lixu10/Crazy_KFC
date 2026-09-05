"""启动器：本地单机网页版选课助手。

只监听 127.0.0.1，自动挑选空闲端口并打开默认浏览器；
按 Ctrl+C 或关闭本窗口退出，退出时尝试安全停止正在运行的任务。
"""
from __future__ import annotations

import socket
import threading
import time
import webbrowser

HOST = "127.0.0.1"
PREFERRED_PORT = 8642


def _free_port(preferred: int = PREFERRED_PORT) -> int:
    for port in range(preferred, preferred + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
            except OSError:
                continue
            return port
    return preferred


def _open_browser_when_ready(url: str, port: int) -> None:
    for _ in range(150):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            try:
                s.connect((HOST, port))
            except OSError:
                time.sleep(0.1)
                continue
        break
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main() -> None:
    try:
        from kfc_college import create_app
    except ModuleNotFoundError as e:
        if getattr(e, "name", "") in ("flask", "kfc_college"):
            print("缺少运行依赖。请双击 start.bat 完成首次安装，或执行：")
            print("    python -m pip install -r requirements.txt")
        else:
            raise
        return

    port = _free_port()
    url = f"http://{HOST}:{port}/"
    print("=" * 58)
    print("  KFC大学选课助手（本地网页版）")
    print(f"  请访问：{url}")
    print("  本窗口保持打开；关闭或按 Ctrl+C 即退出。")
    print("=" * 58)

    app = create_app()
    threading.Thread(target=_open_browser_when_ready, args=(url, port), daemon=True).start()

    try:
        app.run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True)
    finally:
        try:
            services = app.config["SERVICES"]
            tasks = services["tasks"]
            if tasks.active:
                tasks.request_stop()
                time.sleep(0.6)  # 给临界区一段稳定时间再结束进程
        except Exception:
            pass
        print("\n已退出。")


if __name__ == "__main__":
    main()
