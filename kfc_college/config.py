"""本地配置存储：默认值、校验、原子保存、脱敏输出，以及进程日志配置。

统一认证密码、token、cookie、secretVal 一律不持久化。
SMTP 授权码按用户选择保存在本机 data/settings.json（仅本机文件，被 .gitignore 排除），
页面与 API 读取时只返回“是否已配置”。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")

# 一个版本号，后续变更结构时用于迁移。
SCHEMA_VERSION = 1

SENSITIVE_KEYS = ("password", "token", "cookie", "jsessionid", "route", "secret", "castgc")

_DEFAULT = {
    "schema_version": SCHEMA_VERSION,
    "student_class": "",
    # 批次设置
    "batch_mode": "auto",          # auto | manual
    "batch_manual_id": "",
    "batch_last_id": "",
    "poll_interval_sec": 5,
    "tls_verify": True,            # 默认开启证书校验；特殊网络环境可关闭
    # SMTP
    "smtp": {
        "enabled": True,
        "server": "smtp.qq.com",
        "port": 465,
        "security": "ssl",         # ssl | starttls
        "username": "",
        "receiver": "",
        "password": "",            # 邮箱授权码，本机持久化
    },
}

_lock = threading.Lock()


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = v
    return out


def _coerce(raw: dict) -> dict:
    """把读入的 JSON 与默认结构合并，并做基础类型清洗。"""
    merged = _merge(_DEFAULT, raw)
    merged["schema_version"] = SCHEMA_VERSION
    merged["batch_mode"] = "manual" if merged.get("batch_mode") == "manual" else "auto"
    merged["poll_interval_sec"] = max(1, int(float(merged.get("poll_interval_sec", 5) or 5)))
    merged["tls_verify"] = bool(merged.get("tls_verify", True))
    smtp = merged.setdefault("smtp", {})
    smtp["enabled"] = bool(smtp.get("enabled", True))
    smtp["security"] = "starttls" if smtp.get("security") == "starttls" else "ssl"
    smtp["port"] = int(smtp.get("port", 465) or 0) or 465
    for f in ("server", "username", "receiver", "password"):
        smtp[f] = str(smtp.get(f, "") or "")
    return merged


class ConfigStore:
    """读写 data/settings.json，加锁并原子写盘。"""

    def __init__(self, path: str = SETTINGS_PATH):
        self.path = path
        self.settings: dict = self._load()

    # ---- 内部 ----
    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                return _coerce(raw)
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, ValueError, OSError) as e:
            logging.getLogger("app").warning("读取设置失败，使用默认值: %s", e)
        return dict(_DEFAULT)

    def save(self) -> None:
        with _lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.settings, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise

    # ---- 便捷访问 ----
    def update(self, patch: dict) -> list:
        """将外部白名单字段合并到当前设置，返回无法接受的字段列表（空表示全部接受）。"""
        rejected = []
        with _lock:
            merged = self.settings
            if "student_class" in patch:
                merged["student_class"] = str(patch["student_class"]).strip()
            if "batch_mode" in patch:
                mode = str(patch["batch_mode"]).strip().lower()
                merged["batch_mode"] = "manual" if mode == "manual" else "auto"
            if "batch_manual_id" in patch:
                merged["batch_manual_id"] = str(patch["batch_manual_id"]).strip()
            if "poll_interval_sec" in patch:
                try:
                    merged["poll_interval_sec"] = max(1, int(float(patch["poll_interval_sec"])))
                except (TypeError, ValueError):
                    rejected.append("poll_interval_sec")
            if "tls_verify" in patch:
                merged["tls_verify"] = bool(patch["tls_verify"])
            if "smtp" in patch and isinstance(patch["smtp"], dict):
                sm = patch["smtp"]
                tgt = merged.setdefault("smtp", {})
                for key in ("enabled", "server", "port", "security", "username", "receiver", "password"):
                    if key in sm:
                        v = sm[key]
                        if key == "enabled":
                            tgt["enabled"] = bool(v)
                        elif key == "port":
                            tgt["port"] = int(float(v)) if str(v).strip() else 465
                        elif key == "security":
                            tgt["security"] = "starttls" if str(v) == "starttls" else "ssl"
                        elif key == "password":
                            # 空字符串表示“不修改当前密码”；显式 null 表示清除。
                            if v is None:
                                tgt["password"] = ""
                            elif str(v) != "":
                                tgt["password"] = str(v)
                        else:
                            tgt[key] = str(v or "")
        if not rejected:
            self.save()
        return rejected

    # ---- 查询 ----
    def public(self) -> dict:
        """返回给前端的脱敏设置：绝不包含授权码明文。"""
        s = self.settings
        smtp = s["smtp"]
        return {
            "student_class": s.get("student_class", ""),
            "batch_mode": s.get("batch_mode", "auto"),
            "batch_manual_id": s.get("batch_manual_id", ""),
            "poll_interval_sec": s.get("poll_interval_sec", 5),
            "tls_verify": s.get("tls_verify", True),
            "smtp": {
                "enabled": smtp["enabled"],
                "server": smtp["server"],
                "port": smtp["port"],
                "security": smtp["security"],
                "username": smtp["username"],
                "receiver": smtp["receiver"],
                "password_configured": bool(smtp.get("password", "")),
            },
        }


# ---- 日志 ----

_SECRET_SNIPPETS = set()


def remember_secret(value: str) -> None:
    """把需要脱敏的值登记起来，供日志过滤器替换。"""
    if value and len(value) >= 4:
        _SECRET_SNIPPETS.add(value)
        # 截取片段以覆盖子串情形（取全串与中间段）。
        mid = value[len(value) // 3: len(value) - len(value) // 3]
        if len(mid) >= 4:
            _SECRET_SNIPPETS.add(mid)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            for secret in _SECRET_SNIPPETS:
                if secret in msg:
                    msg = msg.replace(secret, "[已隐藏]")
                    record.msg = msg
                    record.args = ()
        except Exception:
            pass
        return True


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("app")
    if root.handlers:
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"),
                                 maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("无法创建日志文件: %s", e)
    root.addFilter(RedactingFilter())


def log() -> logging.Logger:
    return logging.getLogger("app")
