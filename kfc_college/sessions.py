"""多用户会话层：每账号一套上游会话，可由多个本站浏览器共享。

- 一个统一认证学号只建立一个 UserSession，并持有一套 client/cfg/notifier/tasks。
- 每个浏览器获得独立随机 sid；多个 sid 可同时映射到同一个 UserSession。
- 已在线账号再次登录时仅比较进程内密码，不重复请求统一认证。
- 每用户设置落在 data/users/<学号>/settings.json；首登时可从旧版设置迁移。
- 后台仅回收“无运行任务且长期空闲”的账号级会话。
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

DEFAULT_IDLE_TTL = 7200
DEFAULT_REAP_INTERVAL = 60
SID_COOKIE = "kfc_sid"


class LoginConflict(Exception):
    """本站账号会话状态不允许完成当前登录/退出操作。"""

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
    """一个账号的服务端状态对象，可绑定多个浏览器 sid。"""

    def __init__(self, uid: str, cfg: ConfigStore, client: ElectionClient,
                 notifier: EmailNotifier, tasks: TaskManager):
        self.uid = uid
        self.cfg = cfg
        self.client = client
        self.notifier = notifier
        self.tasks = tasks
        self.local_sids: set[str] = set()
        self.created_at = time.time()
        self.last_active = time.time()
        self._close_lock = threading.Lock()
        self._closed = False

    def touch(self) -> None:
        self.last_active = time.time()

    @property
    def closed(self) -> bool:
        return self._closed

    def verify_held_password(self, password: str) -> bool:
        """常量时间比较内存密码；不触发任何上游请求。"""
        verifier = getattr(self.client, "verify_held_password", None)
        if callable(verifier):
            return bool(verifier(password))
        held = str(getattr(self.client, "_password", "") or "")
        return bool(held) and secrets.compare_digest(held, str(password or ""))

    def stop_tasks(self, wait: float = 0.6) -> None:
        """安全停止本账号正在运行的任务。"""
        if self.tasks.active:
            self.tasks.request_stop()
            time.sleep(wait)

    def close(self) -> None:
        """停止任务并清空内存会话（幂等）。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.stop_tasks()
        except Exception:  # noqa: BLE001
            log().warning("停止任务时出错", exc_info=True)
        try:
            self.client.logout()
        except Exception:  # noqa: BLE001
            log().warning("清理会话时出错", exc_info=True)
        self.local_sids.clear()


class UserManager:
    """维护浏览器 sid 与账号级 UserSession 的映射。"""

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
        self._by_sid: dict[str, UserSession] = {}
        self._by_uid: dict[str, UserSession] = {}
        self._login_pending: dict[str, threading.Event] = {}
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
        register_secrets(cfg)
        remember_proxy_secret((cfg.settings.get("proxy", {}) or {}).get("url", ""))
        return cfg

    # ---------- 查询 ----------
    def get_by_sid(self, sid: Optional[str]):
        if not sid:
            return None
        with self._lock:
            sess = self._by_sid.get(sid)
            return None if sess is not None and sess.closed else sess

    def get_by_uid(self, uid: str):
        with self._lock:
            sess = self._by_uid.get(uid)
            return None if sess is not None and sess.closed else sess

    def all(self) -> List[UserSession]:
        with self._lock:
            return list(self._by_uid.values())

    def count(self) -> int:
        """当前逻辑账号会话数。"""
        with self._lock:
            return len(self._by_uid)

    def sid_count(self) -> int:
        with self._lock:
            return len(self._by_sid)

    # ---------- 登录 ----------
    def login_or_attach(self, uid: str, password: str,
                        proxy: Optional[dict] = None) -> tuple[UserSession, str, bool]:
        """首次登录账号，或为已在线账号附加浏览器 sid。

        返回 ``(session, sid, attached)``；attached=True 表示复用了已有上游会话。
        同 UID 的并发首次登录由 reservation 串行化，等待者随后只做内存密码比较。
        """
        user_id = str(uid or "").strip()
        password = str(password or "")
        if not user_id or not password:
            raise LoginError("请输入学号与密码。")

        while True:
            with self._lock:
                existing = self._by_uid.get(user_id)
                if existing is not None and not existing.closed:
                    if not existing.verify_held_password(password):
                        raise LoginError("统一认证用户名或密码错误。")
                    sid = self._attach_locked(existing)
                    log().info("浏览器已连接现有账号会话（%s）", self._mask_uid(user_id))
                    return existing, sid, True

                pending = self._login_pending.get(user_id)
                if pending is None:
                    pending = threading.Event()
                    self._login_pending[user_id] = pending
                    break
            pending.wait()

        try:
            cfg = self._cfg_for(user_id)
            if isinstance(proxy, dict) and proxy:
                cfg.update({"proxy": proxy})
                remember_proxy_secret((cfg.settings.get("proxy", {}) or {}).get("url", ""))

            client = self._client_factory(cfg)
            client.login(user_id, password)
            notifier = self._notifier_factory(cfg)
            tasks = self._task_factory(cfg, client, notifier)
            sess = UserSession(user_id, cfg, client, notifier, tasks)
            with self._lock:
                self._by_uid[user_id] = sess
                sid = self._attach_locked(sess)
            log().info("用户登录成功（%s）", self._mask_uid(user_id))
            return sess, sid, False
        finally:
            with self._lock:
                done = self._login_pending.pop(user_id, None)
                if done is not None:
                    done.set()

    def login(self, uid: str, password: str, proxy: Optional[dict] = None) -> UserSession:
        """兼容旧调用；新 Web 层应使用 login_or_attach 取得本次 sid。"""
        sess, _sid, _attached = self.login_or_attach(uid, password, proxy=proxy)
        return sess

    def _attach_locked(self, sess: UserSession) -> str:
        sid = _new_sid()
        while sid in self._by_sid:
            sid = _new_sid()
        sess.local_sids.add(sid)
        sess.touch()
        self._by_sid[sid] = sess
        return sid

    # ---------- 退出/关闭 ----------
    def detach(self, sid: Optional[str]) -> bool:
        """解绑一个浏览器 sid；返回是否同时关闭了账号级会话。

        若它是仍有活动任务账号的最后一个 sid，则拒绝并保持原绑定。
        """
        if not sid:
            return False
        sess = None
        close_account = False
        with self._lock:
            sess = self._by_sid.get(sid)
            if sess is None:
                return False
            if len(sess.local_sids) == 1 and sess.tasks.active:
                raise LoginConflict("任务运行中，请先停止任务再退出最后一个浏览器。")
            self._by_sid.pop(sid, None)
            sess.local_sids.discard(sid)
            if not sess.local_sids:
                if self._by_uid.get(sess.uid) is sess:
                    self._by_uid.pop(sess.uid, None)
                close_account = True
        if close_account:
            sess.close()
            log().info("账号会话已注销（%s）", self._mask_uid(sess.uid))
        else:
            log().info("浏览器已退出账号会话（%s）", self._mask_uid(sess.uid))
        return close_account

    def logout_account(self, sess: UserSession) -> int:
        """一键注销账号在选课网站的全部登录：下线该账号绑定的所有浏览器 sid。

        与 detach（只退出当前浏览器）不同，即便仍有其他浏览器在线也一并下线；
        账号仍有运行任务时拒绝（抛 LoginConflict）。下线后需重新登录才能再用。
        """
        if sess is None:
            return 0
        with self._lock:
            if sess.tasks.active:
                raise LoginConflict("任务运行中，请先停止任务再退出选课站登录。")
            closed_sids = len(sess.local_sids)
            self._remove_locked(sess)
        sess.close()
        log().info("账号已退出选课站登录（%s），下线 %d 个浏览器",
                   self._mask_uid(sess.uid), closed_sids)
        return closed_sids

    def logout(self, sess: UserSession) -> None:
        """强制关闭一个账号级会话，供管理/兼容代码使用。"""
        if sess is None:
            return
        with self._lock:
            self._remove_locked(sess)
        sess.close()
        log().info("账号会话已注销（%s）", self._mask_uid(sess.uid))

    def _remove_locked(self, sess: UserSession) -> None:
        if self._by_uid.get(sess.uid) is sess:
            self._by_uid.pop(sess.uid, None)
        for sid in tuple(sess.local_sids):
            if self._by_sid.get(sid) is sess:
                self._by_sid.pop(sid, None)
        sess.local_sids.clear()

    def shutdown(self) -> None:
        """进程退出前调用：每个账号只关闭一次。"""
        with self._lock:
            sessions = list(self._by_uid.values())
            self._by_sid.clear()
            self._by_uid.clear()
            for sess in sessions:
                sess.local_sids.clear()
        for sess in sessions:
            sess.close()

    # ---------- 空闲回收 ----------
    def _reap_loop(self) -> None:
        while True:
            time.sleep(self._reaper_interval)
            try:
                self._reap_once()
            except Exception:  # noqa: BLE001
                log().warning("会话回收异常", exc_info=True)

    def _reap_once(self) -> None:
        now = time.time()
        doomed = []
        with self._lock:
            for sess in list(self._by_uid.values()):
                if not sess.tasks.active and now - sess.last_active > self._idle_ttl:
                    self._remove_locked(sess)
                    doomed.append(sess)
        for sess in doomed:
            log().info("回收空闲账号会话（%s）", self._mask_uid(sess.uid))
            sess.close()

    @staticmethod
    def _mask_uid(uid: str) -> str:
        return uid[:3] + "***" if len(uid) > 3 else "***"


__all__ = ["LoginConflict", "UserManager", "UserSession", "SID_COOKIE"]
