"""邮件通知：SMTP SSL / STARTTLS，异步发送不阻塞选课任务，失败只记录不中断业务。"""
from __future__ import annotations

import smtplib
import threading
from email.header import Header
from email.mime.text import MIMEText

from .config import ConfigStore, remember_secret, log


class EmailNotifier:
    def __init__(self, cfg: ConfigStore):
        self.cfg = cfg

    # ---- 配置读取 ----
    def config(self) -> dict:
        s = self.cfg.settings.get("smtp", {})
        return {
            "enabled": bool(s.get("enabled", True)),
            "server": str(s.get("server", "") or ""),
            "port": int(s.get("port", 465) or 465),
            "security": str(s.get("security", "ssl") or "ssl"),
            "username": str(s.get("username", "") or ""),
            "password": str(s.get("password", "") or ""),
            "receiver": str(s.get("receiver", "") or ""),
        }

    def is_ready(self) -> bool:
        c = self.config()
        return all([c["enabled"], c["server"], c["username"], c["password"], c["receiver"]])

    # ---- 同步发送（供测试和后台线程使用）----
    def send_sync(self, subject: str, content: str) -> tuple:
        c = self.config()
        if not c["enabled"]:
            return True, "邮件未启用，已跳过"
        if not all([c["server"], c["username"], c["password"], c["receiver"]]):
            return False, "邮件配置不完整（服务器 / 账号 / 授权码 / 收件人）"
        msg = MIMEText(content, "plain", "utf-8")
        msg["From"] = c["username"]
        msg["To"] = c["receiver"]
        msg["Subject"] = Header(subject, "utf-8")
        try:
            if c["security"] == "starttls":
                server = smtplib.SMTP(c["server"], c["port"], timeout=10)
                server.starttls()
            else:
                server = smtplib.SMTP_SSL(c["server"], c["port"], timeout=10)
            with server:
                server.login(c["username"], c["password"])
                server.sendmail(c["username"], [c["receiver"]], msg.as_string())
            log().info("邮件已发送：%s", subject)
            return True, "发送成功"
        except Exception as e:  # noqa: BLE001
            log().warning("邮件发送失败 [%s]：%s", subject, e)
            return False, f"发送失败：{e}"

    # ---- 异步通知 ----
    def notify(self, subject: str, content: str) -> None:
        threading.Thread(target=self._worker, args=(subject, content),
                         daemon=True, name="email-send").start()

    def _worker(self, subject: str, content: str) -> None:
        self.send_sync(subject, content)

    # ---- 测试邮件 ----
    def send_test(self) -> tuple:
        ok, msg = self.send_sync("选课助手测试邮件", "这是一封来自选课助手的测试邮件。\n如果收到本邮件，说明邮件通知配置正确。")
        return ok, msg


def register_secrets(cfg: ConfigStore) -> None:
    """把 SMTP 授权码登记到日志脱敏表。"""
    pwd = str(cfg.settings.get("smtp", {}).get("password", "") or "")
    if pwd:
        remember_secret(pwd)
