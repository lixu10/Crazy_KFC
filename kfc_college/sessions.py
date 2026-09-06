"""多用户会话层：内存态会话 + 每用户设置 + 每账号单活动会话。

- 一次成功的统一认证登录对应一个 UserSession（uid=学号，sid=随机 HttpOnly cookie）。
- 会话内的 client / cfg / notifier / tasks 全部为该用户私有；统一认证密码只在内存。
- 每用户设置落在 data/users/<学号>/settings.json；首登且旧版 data/settings.json
  存在时做一次性迁移（仅在该用户首次建立文件时发生一次）。
- 同一学号全局只允许一个活动会话：已有活动会话时再次登录会被拒绝（请先退出）。
- 后台线程定期回收“无运行任务且长期空闲”的会话，避免清掉浏览器 Cookie 后账号被占死；
  只要该账号有任务在跑就绝不被回收（后台抢课需要在断线后继续运行）。
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
from typing import Callable, List, Optional

from .client import ElectionClient, LoginError
from .config import ConfigStore, log, remember_proxy_secret
from .notifier import EmailNotifier, register_secrets
from .tasks import TaskManager

DEFAULT_IDLE_TTL = 7200          # 无任务会话空闲超过该秒数即回收（默认 2 小时）
DEFAULT_REAP_INTERVAL = 60       # 回收线程扫描周期（秒）
SID_COOKIE = "kfc_sid"


class LoginConflict(Exception):
    """同一账号已存在活动会话时的登录拒绝。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _sanitize_uid(uid: str) -> str:
    """把学号清洗成安全的目录名。"""
    s = str(uid or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", s)
    return (safe or "anon")[:64]


def _new_sid() -> str:
    return secrets.token_urlsafe(24)


class UserSession:
    """一个已登录用户的服务端状态对象。"""

    def __init__(self, sid: str, uid: str, cfg: ConfigStore, client: ElectionClient,
                 notifier: EmailNotifier, tasks: TaskManager):
        self.sid = sid
        self.uid = uid
        self.cfg = cfg
        self.client = client
        self.notifier = notifier
        self.tasks = tasks
        self.created_at = time.time()
        self.last_active = time.time()

    def touch(self) -> None:
        self.last_active = time.time()

    def stop_tasks(self, wait: float = 0.6) -> None:
        """安全停止本会话正在运行的任务。"""
        if self.tasks.active:
            self.tasks.request_stop()
            time.sleep(wait)  # 给临界区一段稳定时间

    def close(self) -> None:
        """停止任务并清空内存会话（幂等）。"""
        try:
            self.stop_tasks()
        except Exception:  # noqa: BLE001
            log().warning("停止任务时出错", exc_info=True)
        try:
            self.client.logout()
        except Exception:  # noqa: BLE001
            log().warning("清理会话时出错", exc_info=True)


class UserManager:
    """维护 sid/uid 到会话的映射；登录/回收/关闭都在这里收敛。"""

    def __init__(self, data_dir: str,
                 client_factory: Optional[Callable] = None,
                 notifier_factory: Optional[Callable] = None,
                 task_factory: Optional[Callable] = None,
                 idle_ttl: float = DEFAULT_IDLE_TTL,
                 reaper_interval: float = DEFAULT_REAP_INTERVAL):
        self.data_dir = data_dir
        self._client_factory = client_factory or (lambda cfg: ElectionClient(cfg))
        self._notifier_factory = notifier_factory or (lambda cfg: EmailNotifier(cfg))
        self._task_factory = task_factory or (
            lambda cfg, client, notifier: TaskManager(cfg, client, notifier))
        self._idle_ttl = idle_ttl
        self._reaper_interval = reaper_interval
        self._lock = threading.RLock()
        self._by_sid: dict = {}
        self._by_uid: dict = {}
        self._users_root = os.path.join(data_dir, "users")
        os.makedirs(self._users_root, exist_ok=True)
        if reaper_interval and reaper_interval > 0:
            self._reaper = threading.Thread(target=self._reap_loop, daemon=True,
                                            name="session-reaper")
            self._reaper.start()

    # ---------- 路径 ----------
    def settings_path_for(self, uid: str) -> str:
        return os.path.join(self._users_root, _sanitize_uid(uid), "settings.json")

    def _cfg_for(self, uid: str) -> ConfigStore:
        """构造某用户的 ConfigStore；必要时从旧版单用户文件一次性迁移。"""
        path = self.settings_path_for(uid)
        if not os.path.exists(path):
            legacy = os.path.join(self.data_dir, "settings.json")
            if os.path.exists(legacy):
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    shutil.copyfile(legacy, path)
                    log().info("已将旧版 data/settings.json 迁移为账号 %s 的设置。", uid)
                except OSError as e:
                    log().warning("迁移旧版设置失败：%s", e)
        cfg = ConfigStore(path)
        # SMTP 授权码与代理凭据登记进日志脱敏表（针对该用户配置）。
        register_secrets(cfg)
        remember_proxy_secret((cfg.settings.get("proxy", {}) or {}).get("url", ""))
        return cfg

    # ---------- 查询 ----------
    def get_by_sid(self, sid: Optional[str]):
        if not sid:
            return None
        with self._lock:
            return self._by_sid.get(sid)

    def get_by_uid(self, uid: str):
        with self._lock:
            return self._by_uid.get(uid)

    def all(self) -> List[UserSession]:
        with self._lock:
            return list(self._by_sid.values())

    def count(self) -> int:
        with self._lock:
            return len(self._by_sid)

    # ---------- 生命周期 ----------
    def login(self, uid: str, password: str, proxy: Optional[dict] = None) -> UserSession:
        """完成统一认证登录并登记会话。失败抛出 LoginError/NetworkError/LoginConflict。"""
        user_id = str(uid or "").strip()
        password = str(password or "")
        if not user_id or not password:
            raise LoginError("请输入学号与密码。")
        with self._lock:
            if user_id in self._by_uid:
                raise LoginConflict("该账号已在别处登录：同一账号仅允许一个活动会话，请先退出原会话后再登录。")

        cfg = self._cfg_for(user_id)
        if isinstance(proxy, dict) and proxy:
            # 首次登录也可带上代理（用于登录本身就需要走代理的受限网络）。
            cfg.update({"proxy": proxy})
            remember_proxy_secret((cfg.settings.get("proxy", {}) or {}).get("url", ""))

        client = self._client_factory(cfg)
        client.login(user_id, password)   # 真实 SSO 登录，可能抛 LoginError/NetworkError
        notifier = self._notifier_factory(cfg)
        tasks = self._task_factory(cfg, client, notifier)
        sess = UserSession(_new_sid(), user_id, cfg, client, notifier, tasks)

        with self._lock:
            existing = self._by_uid.get(user_id)
            if existing is not None:
                # 并发同账号：后完成的登录被拒绝（把刚建立的会话干净地收掉）。
                try:
                    client.logout()
                except Exception:  # noqa: BLE001
                    pass
                raise LoginConflict("该账号已在别处登录：同一账号仅允许一个活动会话，请先退出原会话后再登录。")
            self._by_uid[user_id] = sess
            self._by_sid[sess.sid] = sess
        log().info("用户登录成功（%s）", uid[:3] + "***")
        return sess

    def logout(self, sess: UserSession) -> None:
        """注销单个会话（安全停止任务 + 清理客户端 + 移出登记）。"""
        if sess is None:
            return
        with self._lock:
            if self._by_sid.get(sess.sid) is sess:
                del self._by_sid[sess.sid]
            if self._by_uid.get(sess.uid) is sess:
                del self._by_uid[sess.uid]
        sess.close()
        log().info("会话已注销（%s）", sess.uid[:3] + "***")

    def shutdown(self) -> None:
        """进程退出前调用：停掉所有活动任务并清空会话。"""
        with self._lock:
            sessions = list(self._by_sid.values())
            self._by_sid.clear()
            self._by_uid.clear()
        for sess in sessions:
            sess.close()

    # ---------- 空闲回收 ----------
    def _reap_loop(self) -> None:
        while True:
            time.sleep(self._reap_interval)
            try:
                self._reap_once()
            except Exception:  # noqa: BLE001
                log().warning("会话回收异常", exc_info=True)

    def _reap_once(self) -> None:
        now = time.time()
        doomed = []
        with self._lock:
            for sess in list(self._by_sid.values()):
                # 有任务在跑：绝不动它；否则超过空闲阈值才回收。
                if not sess.tasks.active and now - sess.last_active > self._idle_ttl:
                    doomed.append(sess)
        for sess in doomed:
            log().info("回收空闲会话（%s）", sess.uid[:3] + "***")
            self.logout(sess)


__all__ = ["LoginConflict", "UserManager", "UserSession", "SID_COOKIE"]
