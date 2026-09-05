"""后台任务：全局单一活动任务，协作式取消，Token 过期自动重登，安全改选状态机。

所有任务线程通过 cancel.wait(timeout) 取代 time.sleep()，以便即时响应停止。
改选在“确认退掉旧课后”进入不可随意中断的临界区，停止请求延后到稳定边界生效。
"""
from __future__ import annotations

import threading
import time
from typing import List, Optional

from .client import (AuthExpired, BadResponse, BatchUnavailable, ClientError,
                     ElectionClient, NetworkError)
from .config import ConfigStore, log
from .courses import capacity_of, old_from_selected_row
from .models import (CourseTarget, SwapPair, TargetState, TaskEvent, TaskMode,
                     TaskRecord, TaskStatus)
from .notifier import EmailNotifier

TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.STOPPED, TaskStatus.FAILED,
            TaskStatus.MANUAL_ATTENTION}
EVENT_CAP = 1200


def _ts() -> str:
    return time.strftime("%H:%M:%S")


class TaskManager:
    def __init__(self, cfg: ConfigStore, client: ElectionClient, notifier: EmailNotifier):
        self.cfg = cfg
        self.client = client
        self.notifier = notifier
        self._lock = threading.RLock()
        self._seq = 0
        self._events: List[TaskEvent] = []
        self._task: Optional[TaskRecord] = None
        self._next_id = 1

    # ---------- 查询 ----------
    @property
    def task(self) -> Optional[TaskRecord]:
        return self._task

    @property
    def active(self) -> bool:
        t = self._task
        return bool(t and t.status not in TERMINAL)

    def _emit(self, level: str, kind: str, message: str) -> None:
        with self._lock:
            self._seq += 1
            self._events.append(TaskEvent(self._seq, _ts(), level, kind, message))
            if len(self._events) > EVENT_CAP:
                self._events = self._events[-EVENT_CAP:]

    def events(self, after: int = 0):
        with self._lock:
            out = [e.to_dict() for e in self._events if e.seq > after]
            last = self._events[-1].seq if self._events else 0
            return out, last

    def states_snapshot(self) -> dict:
        t = self._task
        if not t:
            return {}
        # 先整体拷贝再遍历：任务线程可能正对 t.states 做 setdefault，
        # 直接 .items() 遍历共享字典可能触发 RuntimeError。
        snap = dict(t.states)
        return {k: v.__dict__.copy() for k, v in snap.items()}

    def summary(self) -> Optional[dict]:
        t = self._task
        if not t:
            return None
        return {
            "id": t.id,
            "mode": t.mode,
            "status": t.status,
            "stage": t.stage,
            "created_ts": t.created_ts,
            "started_ts": t.started_ts,
            "ended_ts": t.ended_ts,
            "stop_requested": t.stop_requested,
            "summary": t.summary,
            "error": t.error,
            "last_check": t.last_check,
            "states": self.states_snapshot(),
        }

    # ---------- 生命周期 ----------
    def start(self, mode: str, *, targets: Optional[List[CourseTarget]] = None,
              pairs: Optional[List[SwapPair]] = None,
              student_class: str = "") -> TaskRecord:
        with self._lock:
            if self.active:
                raise RuntimeError("已有任务在运行，请先停止")
            task = TaskRecord(id=self._next_id, mode=mode)
            self._next_id += 1
            task.created_ts = _ts()
            task.student_class = student_class or str(self.cfg.settings.get("student_class", ""))
            task.interval = max(1.0, float(self.cfg.settings.get("poll_interval_sec", 5)))
            task.cancel = threading.Event()
            if mode == TaskMode.SWAP:
                task.swap_pairs = list(pairs or [])
                for p in task.swap_pairs:
                    st = TargetState(key=p.target.key,
                                     label=f"{p.old.display()} → {p.target.display()}",
                                     status="watching")
                    task.states[p.target.key] = st
            else:
                task.targets = list(targets or [])
                for tg in task.targets:
                    task.states[tg.key] = TargetState(key=tg.key, label=tg.display())
            self._task = task
            threading.Thread(target=self._runner, args=(task,), daemon=True,
                             name=f"task-{task.id}").start()
            return task

    def request_stop(self) -> None:
        t = self._task
        if t and t.status not in TERMINAL:
            t.stop_requested = True
            t.cancel.set()
            if t.status != TaskStatus.STOPPING:
                t.status = TaskStatus.STOPPING
            self._emit("info", "stop_request", "已请求停止任务…")

    # ---------- 运行 ----------
    def _runner(self, task: TaskRecord) -> None:
        task.status = TaskStatus.RUNNING
        task.started_ts = _ts()
        try:
            if task.mode == TaskMode.POLL:
                self._run_poll(task)
            elif task.mode == TaskMode.GRAB:
                self._run_grab(task)
            elif task.mode == TaskMode.SWAP:
                self._run_swap(task)
            else:
                task.status = TaskStatus.FAILED
                task.error = f"未知模式 {task.mode}"
        except Exception as e:  # noqa: BLE001
            log().exception("任务异常退出")
            task.status = TaskStatus.FAILED
            task.error = str(e)
            task.stage = "任务异常终止"
            self._emit("error", "error", f"任务异常终止：{e}")
        finally:
            task.ended_ts = task.ended_ts or _ts()

    def _finish(self, task: TaskRecord, status: str, stage: str, summary: str = "") -> None:
        task.status = status
        task.stage = stage
        task.summary = summary or stage
        task.ended_ts = _ts()

    def _wait(self, task: TaskRecord, seconds: float) -> bool:
        """等待期间可响应停止。返回 True=继续，False=已停止应结束。"""
        task.cancel.wait(max(0.1, seconds))
        if task.cancel.is_set():
            self._emit("info", "stopped", "任务已停止。")
            return False
        return True

    # ---------- 认证保障 ----------
    def _ensure_auth(self, task: TaskRecord) -> bool:
        """仅在捕获到 AuthExpired 后被调用：token 已被服务端拒绝，
        authenticated 仍为真（仅代表 token 字符串存在），因此这里一律尝试重登。"""
        self.client.login_ready.clear()
        if self.client.relogin():
            self._emit("success", "relogin", "会话已过期，自动重新登录成功，继续任务。")
            task.status = TaskStatus.RUNNING
            task.stage = ""
            return True
        task.status = TaskStatus.WAITING_LOGIN
        task.stage = "等待在页面重新输入密码以继续"
        self._emit("warn", "auth_wait", "会话已过期且自动重登失败，请在页面重新输入密码后继续。")
        while not task.cancel.is_set():
            if self.client.login_ready.wait(timeout=1.0):
                if self.client.authenticated:
                    task.status = TaskStatus.RUNNING
                    task.stage = ""
                    self._emit("success", "relogin", "已重新登录，继续任务。")
                    return True
        task.status = TaskStatus.STOPPED
        task.stage = "等待登录期间已停止"
        task.ended_ts = _ts()
        self._emit("info", "stopped", "等待登录期间任务被停止。")
        return False

    # ---------- 数据获取帮助 ----------
    def _type_group(self, targets: List[CourseTarget]) -> dict:
        g = {}
        for t in targets:
            g.setdefault(t.class_type, []).append(t)
        return g

    def _fetch_map(self, class_type: str) -> dict:
        rows = self.client.list_classes(class_type)
        return {r.get("JXBID"): r for r in rows if isinstance(r, dict) and r.get("JXBID")}

    def _reconcile_contains(self, jxbid: str) -> bool:
        return jxbid in self.client.selected_jxbid_set()

    def _notify_slot(self, label: str, selected: int, capacity: int) -> None:
        self.notifier.notify(f"[有余量] {label} {selected}/{capacity}",
                             f"课程 {label} 当前 {selected}/{capacity}，出现空余名额。")

    def _slot(self, row: dict, student_class: str):
        cap, sel = capacity_of(row, student_class)
        return cap, sel, cap > sel

    # ---------- 仅监控 ----------
    def _run_poll(self, task: TaskRecord) -> None:
        targets = list(task.targets)
        task.stage = "监控中"
        while True:
            if task.cancel.is_set():
                break
            try:
                self._poll_round(task, targets)
            except AuthExpired:
                if not self._ensure_auth(task):
                    return
                continue
            except (NetworkError, BadResponse, BatchUnavailable, ClientError) as e:
                task.stage = f"接口异常，等待下轮：{e.message}"
                self._emit("warn", "error", e.message)
            if not self._wait(task, task.interval):
                return
        self._finish(task, TaskStatus.STOPPED, "已停止")

    def _poll_round(self, task: TaskRecord, targets: List[CourseTarget]) -> None:
        for type_code, infos in self._type_group(targets).items():
            try:
                index = self._fetch_map(type_code)
            except (NetworkError, BadResponse, BatchUnavailable, ClientError) as e:
                self._emit("warn", "error", f"类型 {type_code} 查询失败：{e.message}")
                continue
            for tg in infos:
                row = index.get(tg.jxbid)
                st = task.states.setdefault(tg.key, TargetState(key=tg.key, label=tg.display()))
                if row is None:
                    st.detail = "未在列表中"
                    st.last_check = _ts()
                    continue
                cap, sel, has = self._slot(row, task.student_class)
                st.capacity = cap
                st.selected = sel
                st.has_slot = has
                st.last_check = _ts()
                if has and not st.notified:
                    st.notified = True
                    st.detail = f"出现余量 {sel}/{cap}"
                    self._emit("success", "slot", f"{tg.display()} 出现余量 {sel}/{cap}，发送通知。")
                    self._notify_slot(tg.display(), sel, cap)
                elif not has:
                    if st.notified:
                        self._emit("info", "full", f"{tg.display()} 已重新满员 {sel}/{cap}。")
                    st.notified = False
                    st.detail = f"{sel}/{cap} 暂无余量"
        task.last_check = _ts()

    # ---------- 抢课 ----------
    def _run_grab(self, task: TaskRecord) -> None:
        active = {t.key: t for t in task.targets}
        task.stage = "抢课中"
        while active:
            if task.cancel.is_set():
                break
            try:
                done = self._grab_round(task, active)
            except AuthExpired:
                if not self._ensure_auth(task):
                    return
                continue
            except (NetworkError, BadResponse, BatchUnavailable, ClientError) as e:
                task.stage = f"接口异常，等待下轮：{e.message}"
                self._emit("warn", "error", e.message)
                done = set()
            for key in done:
                active.pop(key, None)
            if not active:
                self._finish(task, TaskStatus.SUCCEEDED, "全部完成",
                             f"共 {len(task.targets)} 门课程已全部选上")
                self._emit("success", "all_done", "抢课队列中所有课程均已成功选上。")
                self.notifier.notify("[选课完成] 所有课程已选上",
                                     "抢课队列中的所有课程均已成功选上。")
                return
            if not self._wait(task, task.interval):
                return
        if task.status not in TERMINAL:
            succ = sum(1 for st in task.states.values() if st.status == "done")
            self._finish(task, TaskStatus.STOPPED, "已停止",
                         f"已成功 {succ} 门，剩余 {len(active)} 门")
            self._emit("info", "stopped", "抢课任务已停止。")

    def _grab_round(self, task: TaskRecord, active: dict) -> set:
        done = set()
        for type_code, infos in self._type_group(list(active.values())).items():
            index = self._fetch_map(type_code)
            for tg in infos:
                row = index.get(tg.jxbid)
                st = task.states.setdefault(tg.key, TargetState(key=tg.key, label=tg.display()))
                if row is None:
                    st.detail = "未在列表中"
                    continue
                cap, sel, has = self._slot(row, task.student_class)
                st.capacity = cap
                st.selected = sel
                st.last_check = _ts()
                st.has_slot = has
                if not has:
                    st.detail = f"{sel}/{cap} 暂无余量"
                    continue
                st.detail = f"发现余量 {sel}/{cap}，尝试选课"
                self._emit("info", "try_add", f"{tg.display()} 发现余量，尝试选课…")
                secret = row.get("secretVal", "")
                res = self.client.add_class(type_code, tg.jxbid, secret)
                if self._reconcile_contains(tg.jxbid):
                    st.status = "done"
                    st.detail = f"选课成功（{sel}/{cap}）"
                    self._emit("success", "add_ok", f"{tg.display()} 选课成功。")
                    self.notifier.notify(f"[选课成功] {tg.display()}",
                                         f"课程 {tg.display()} 已成功选上。")
                    done.add(tg.key)
                elif res.get("status") == "ok":
                    st.detail = "返回成功但未见已选，继续对账"
                    self._emit("warn", "reconcile", f"{tg.display()} 选课响应未确认，继续监控。")
                else:
                    st.detail = f"选课失败：{res.get('msg', '')}"
                    self._emit("warn", "add_fail", f"{tg.display()} 选课失败：{res.get('msg', '')}")
        return done

    # ---------- 安全改选 ----------
    def _run_swap(self, task: TaskRecord) -> None:
        pairs = list(task.swap_pairs)
        try:
            ok, reason = self._swap_preflight(task, pairs)
        except AuthExpired:
            if not self._ensure_auth(task):
                return
            ok, reason = self._swap_preflight(task, pairs)
        if not ok:
            self._finish(task, TaskStatus.FAILED, "改选预检未通过", reason)
            self._emit("error", "swap_precheck", reason)
            return
        task.stage = "改选监控中"
        while True:
            if task.cancel.is_set():
                break
            watching = [p for p in pairs
                        if task.states.get(p.target.key)
                        and task.states[p.target.key].status in ("watching", "cooling")]
            if not watching:
                self._finish(task, TaskStatus.SUCCEEDED, "全部改选完成")
                self._emit("success", "all_done", "所有改选对均已完成。")
                return
            try:
                self._swap_round(task, watching)
            except AuthExpired:
                if not self._ensure_auth(task):
                    return
                continue
            except (NetworkError, BadResponse, BatchUnavailable, ClientError) as e:
                self._emit("warn", "error", f"改选轮询异常：{e.message}")
            if task.status in TERMINAL:
                return
            if not self._wait(task, task.interval):
                return
        if task.status not in TERMINAL:
            self._finish(task, TaskStatus.STOPPED, "已停止")
            self._emit("info", "stopped", "改选任务已停止。")

    def _swap_preflight(self, task: TaskRecord, pairs: List[SwapPair]) -> tuple:
        rows = self.client.fetch_selected()
        selected = {str(r.get("JXBID")): r for r in rows if r.get("JXBID")}
        for p in pairs:
            r = selected.get(p.old.jxbid)
            if r is None:
                return False, f"原课程 {p.old.display()} 当前不在已选列表中，无法改选。"
            o = old_from_selected_row(r)
            if o:
                p.old = o
            if p.target.jxbid in selected:
                return False, f"目标课程 {p.target.display()} 已在已选列表中，无需改选。"
            if p.old.jxbid == p.target.jxbid:
                return False, f"原课程与目标课程相同：{p.old.display()}。"
        old_keys = [p.old.jxbid for p in pairs]
        if len(set(old_keys)) != len(old_keys):
            return False, "同一门原课程被重复用于多个改选对，已阻止。"
        tkeys = [p.target.key for p in pairs]
        if len(set(tkeys)) != len(tkeys):
            return False, "目标课程重复，已阻止。"
        return True, ""

    def _swap_round(self, task: TaskRecord, watching: List[SwapPair]) -> None:
        targets = [p.target for p in watching]
        maps = {}
        for type_code, infos in self._type_group(targets).items():
            maps[type_code] = self._fetch_map(type_code)
        for p in watching:
            st = task.states[p.target.key]
            row = maps.get(p.target.class_type, {}).get(p.target.jxbid)
            if row is None:
                continue
            cap, sel, has = self._slot(row, task.student_class)
            st.capacity = cap
            st.selected = sel
            st.last_check = _ts()
            if st.status == "cooling":
                # 冷却：须先观察到一次“重新满员”再重新武装，避免回退后立刻再退旧课。
                if not has and not getattr(st, "armed", False):
                    st.armed = True
                    st.status = "watching"
                    st.detail = "已冷却（目标重新满员），重新武装"
                    self._emit("info", "rearm", f"{p.target.display()} 已重新满员，改选对重新武装。")
                elif has and not getattr(st, "armed", False):
                    continue  # 刚回退目标仍有余量 → 本轮跳过，等待满员
                else:
                    st.status = "watching"
            if st.status == "watching" and has:
                self._execute_swap(task, p, row)
                return  # 同一轮只处理一对临界事务
        task.last_check = _ts()

    def _execute_swap(self, task: TaskRecord, pair: SwapPair, row: dict) -> None:
        st = task.states[pair.target.key]
        st.detail = "检测到余量，二次确认中"
        self._emit("info", "swap_found",
                   f"{pair.old.display()} → {pair.target.display()} 出现余量，准备改选。")
        # 二次确认：原课仍在、目标未在、余量仍可用
        try:
            sel_rows = self.client.fetch_selected()
        except (NetworkError, BadResponse, ClientError):
            sel_rows = None
        if sel_rows is not None:
            old_present = any(str(r.get("JXBID")) == pair.old.jxbid for r in sel_rows)
            target_present = any(str(r.get("JXBID")) == pair.target.jxbid for r in sel_rows)
            if target_present:
                st.status = "done"
                st.detail = "目标实际已选，改选完成"
                self._emit("success", "swap_ok", f"{pair.target.display()} 已在已选列表，改选完成。")
                return
            if not old_present:
                st.status = "manual"
                st.detail = "原课程已不在已选列表且目标未选上，请人工核实"
                task.stage = "需要人工处理"
                self._emit("error", "manual_attention",
                           f"改选对 {pair.old.display()} → {pair.target.display()}：原课程已不在已选列表，请人工核实。")
                self._manual_stop(task, st, pair)
                return
        # 确认原课仍在 → 退课
        st.detail = "退掉原课程"
        drop = self.client.drop_class(pair.old.jxbid, pair.old.class_type)
        if drop.get("status") == "unknown":
            self._emit("warn", "drop_unknown", f"退课响应未确认，进行对账：{drop.get('msg')}")
        if not self._drop_reconciled(pair.old.jxbid):
            self._manual_stop(task, st, pair,
                              "无法确认原课程是否已成功退掉，请人工核实。")
            return
        self._emit("success", "drop_ok", f"已退掉 {pair.old.display()}。")
        st.detail = "已退旧课，选目标课程中"

        # 选目标（至多 3 次，逐步对账）
        secret = row.get("secretVal", "")
        for attempt in range(1, 4):
            res = self.client.add_class(pair.target.class_type, pair.target.jxbid, secret)
            try:
                selected_now = self._reconcile_contains(pair.target.jxbid)
            except (NetworkError, BadResponse, ClientError):
                selected_now = False
            if selected_now:
                st.status = "done"
                st.detail = "改选成功"
                self._emit("success", "swap_ok",
                           f"改选成功：{pair.old.display()} → {pair.target.display()}。")
                self.notifier.notify(
                    f"[改选成功] {pair.old.display()}→{pair.target.display()}",
                    f"已成功将 {pair.old.display()} 改选为 {pair.target.display()}。")
                return
            if res.get("status") == "ok":
                self._emit("warn", "reconcile", f"{pair.target.display()} 响应未确认，再次对账。")
            else:
                self._emit("warn", "add_fail",
                           f"{pair.target.display()} 选课失败（{attempt}/3）：{res.get('msg', '')}")
            if attempt < 3:
                task.cancel.wait(0.6)
                if task.cancel.is_set():
                    # 临界区内不中断：仍执行回退以恢复原课程
                    pass
        # 三次仍未确认 → 回退
        self._rollback(task, st, pair)

    def _drop_reconciled(self, old_jxbid: str) -> bool:
        """退课后确认原课程已不在已选列表；网络不确定则尝试一次后保守返回。"""
        for _ in range(2):
            try:
                return not self._reconcile_contains(old_jxbid)
            except AuthExpired:
                if not self.client.relogin():
                    return False
            except (NetworkError, BadResponse, ClientError):
                return False
        return False

    def _rollback(self, task: TaskRecord, st: TargetState, pair: SwapPair) -> None:
        st.detail = "改选失败，回退原课程"
        self._emit("warn", "rollback", f"目标课程未能选上，尝试回退 {pair.old.display()}。")
        old = pair.old
        if old.class_type:
            res = self.client.add_class(old.class_type, old.jxbid, old.secret_val)
            if res.get("status") == "unknown":
                self._emit("warn", "rollback_unknown", "回退响应未确认，进行对账。")
        try:
            rows = self.client.fetch_selected()
        except (NetworkError, BadResponse, ClientError):
            rows = []
        old_present = any(str(r.get("JXBID")) == old.jxbid for r in rows)
        target_present = any(str(r.get("JXBID")) == pair.target.jxbid for r in rows)
        if old_present and not target_present:
            st.armed = False  # 需重新观察到一次“满员”才能再次武装，避免立刻重复退旧课
            st.status = "cooling"
            st.detail = "已回退选回原课程，进入冷却（下次再触发需重新满员）"
            self._emit("success", "rollback_ok", f"已回退选回 {old.display()}，暂停该改选对。")
            self.notifier.notify("[改选回退] 已恢复原课程",
                                 f"改选 {pair.target.display()} 失败，已回退选回 {old.display()}。")
            return
        if target_present and not old_present:
            st.status = "done"
            st.detail = "目标实际选中，改选成功"
            self._emit("success", "swap_ok", f"{pair.target.display()} 实际已选中，改选成功。")
            return
        self._manual_stop(task, st, pair,
                          "回退对账异常：请登录系统人工确认原课程与目标课程状态。")

    def _manual_stop(self, task: TaskRecord, st: TargetState, pair: SwapPair,
                     msg: str) -> None:
        st.status = "manual"
        st.detail = msg
        task.stage = "需要人工处理"
        self._emit("error", "manual_attention", msg)
        self.notifier.notify("[紧急] 改选需要人工处理",
                             f"改选对 {pair.old.display()} → {pair.target.display()}：{msg}")
        self._finish(task, TaskStatus.MANUAL_ATTENTION, "需要人工处理", msg)
