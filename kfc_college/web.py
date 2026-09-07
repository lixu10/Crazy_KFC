"""Flask 页面与 JSON API（多用户版：每个登录会话操作各自的客户端/设置/任务）。

约定：写接口只接受 application/json 且必须同源；所有响应禁用缓存；
任何接口都不返回 SMTP 授权码、代理密码、token、cookie、secretVal。
除 bootstrap / gate / login 外，其余 /api 接口都要求已登录（g.sess 非空）。
"""
from __future__ import annotations

import secrets

from flask import Blueprint, current_app, g, jsonify, render_template, request

from .client import AuthExpired, ClientError, LoginError, NetworkError
from .config import remember_proxy_secret, remember_secret
from .courses import (normalize_section, old_from_selected_row, search_rows,
                      selected_public, selected_public_full)
from .models import (CODE_TO_TYPE, CourseTarget, ID_TO_TYPE, SwapPair,
                     TaskMode, TaskStatus)
from .sessions import LoginConflict

bp = Blueprint("api", __name__)

# 未登录也可访问的 /api 白名单（其余 /api 一律要求登录）。
PUBLIC_API = {"/api/bootstrap", "/api/gate", "/api/auth/login"}


def _user():
    """当前登录会话；未登录返回 None（此时只有 PUBLIC_API 会被放行）。"""
    return getattr(g, "sess", None)


def _manager():
    return current_app.config["USER_MANAGER"]


# ---- 响应工具 ----
def ok(data=None):
    return jsonify({"ok": True, "data": data})


def err(message: str, status: int = 400, *, code: str = "request_error",
        retryable: bool = False):
    return jsonify({"ok": False, "error": {
        "code": code, "message": str(message), "retryable": bool(retryable),
    }}), status


def client_err(exc: ClientError):
    return err(exc.message, exc.http_status, code=exc.code, retryable=exc.retryable)


def _mask_id(u: str) -> str:
    return (u[:3] + "***") if u and len(u) > 3 else ("***" if u else "")


# ---- 中间件 ----
@bp.after_app_request
def no_store(resp):
    if request.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.before_request
def _require_login():
    if request.path.startswith("/static/") or request.path == "/":
        return None
    if request.path in PUBLIC_API:
        return None
    if _user() is None:
        return jsonify({"ok": False, "error": {"code": "unauth", "message": "请先登录。"}}), 401
    return None


# ---- 页面 ----
@bp.get("/")
def index():
    return render_template("index.html")


# ---- Bootstrap ----
def _bootstrap_payload(u) -> dict:
    course_types = [{"id": t.id, "code": t.code, "name": t.name} for t in ID_TO_TYPE.values()]
    if u is None:
        return {
            "auth": {"logged_in": False, "user": "", "password_held": False},
            "batch": {"state": "unavailable", "message": "", "active": None,
                      "choices": [], "id": "", "name": "", "source": "unknown",
                      "validated_ts": "", "mode": "auto", "manual_id": ""},
            "settings": None,
            "course_types": course_types,
            "task": None,
        }
    c = u.client
    s = u.cfg.public()
    logged = c.authenticated
    batch = c.batch_runtime()
    active = batch.get("active") or {}
    batch.update({
        # 保留旧前端字段兼容，但它们只代表已验证活动批次，绝不回退到 manual_id。
        "id": active.get("id", ""),
        "name": active.get("name", ""),
        "source": active.get("source", "unknown"),
        "validated_ts": active.get("validated_ts", ""),
        "mode": s.get("batch_mode"),
        "manual_id": s.get("batch_manual_id"),
    })
    return {
        "auth": {
            "logged_in": logged,
            "user": _mask_id(c.user_id) if c.user_id else "",
            "password_held": c.password_held,
        },
        "batch": batch,
        "settings": s,
        "course_types": course_types,
        "task": u.tasks.summary(),
    }


@bp.get("/api/bootstrap")
def bootstrap():
    return ok(_bootstrap_payload(_user()))


# ---- 访问口令 ----
@bp.post("/api/gate")
def gate():
    token = current_app.config.get("ACCESS_TOKEN")
    if not token:
        return ok({"gated": False})
    body = request.get_json(silent=True) or {}
    given = str(body.get("token", ""))
    if not given or not secrets.compare_digest(given, token):
        return err("访问口令错误。", 403)
    nonce = secrets.token_urlsafe(24)
    current_app.config["GATE_NONCES"].add(nonce)
    resp = ok({"gated": True})
    resp.set_cookie("kfc_gate", nonce,
                    max_age=current_app.config.get("SID_MAX_AGE", 2592000),
                    httponly=True, samesite="Lax",
                    secure=bool(current_app.config.get("COOKIE_SECURE")), path="/")
    return resp


# ---- 设置 ----
@bp.put("/api/settings")
def update_settings():
    u = _user()
    cfg = u.cfg
    tasks = u.tasks
    payload = request.get_json(silent=True) or {}
    running = tasks.active
    # 运行期间禁止修改会改变运行语义的字段
    blocked = [k for k in ("student_class", "batch_mode", "batch_manual_id", "poll_interval_sec")
               if k in payload and running]
    if blocked:
        return err("任务运行期间不能修改：" + "、".join(blocked), 409, code="task_active")
    rejected = cfg.update(payload)
    if rejected:
        return err("设置中有字段不合法：" + "、".join(rejected), 422)
    pw = (payload.get("smtp") or {}).get("password")
    if pw and str(pw).strip():
        remember_secret(str(pw))  # 新写入的授权码立即纳入日志脱敏
    if "proxy" in payload:
        # 让既有会话（含正在跑的请求）立刻使用新代理；并登记新凭据供日志脱敏。
        u.client.apply_proxy()
        remember_proxy_secret((cfg.settings.get("proxy", {}) or {}).get("url", ""))
    return ok(cfg.public())


@bp.post("/api/settings/email/test")
def email_test():
    ok_, msg = _user().notifier.send_test()
    if ok_:
        return ok({"message": msg})
    return err(msg, 422)


# ---- 登录 / 退出 ----
@bp.post("/api/auth/login")
def login():
    body = request.get_json(silent=True) or {}
    user_id = str(body.get("user_id", "")).strip()
    password = str(body.get("password", ""))
    if not user_id or not password:
        return err("请输入学号与密码。", 422)
    proxy = body.get("proxy")
    if not isinstance(proxy, dict):
        proxy = None
    current = _user()
    if current is not None:
        if current.uid != user_id:
            return err("当前浏览器已登录其他账号，请先退出。", 409, code="login_conflict")
        if not current.verify_held_password(password):
            return err("统一认证用户名或密码错误。", 401, code="invalid_credentials")
        payload = _bootstrap_payload(current)
        payload["attached"] = True
        return ok(payload)
    try:
        sess, sid, attached = _manager().login_or_attach(user_id, password, proxy=proxy)
    except LoginConflict as e:
        return err(e.message, 409, code="login_conflict")
    except (LoginError, NetworkError) as e:
        return client_err(e)
    g.sess = sess
    g.sid = sid
    g._set_sid = sid
    payload = _bootstrap_payload(sess)
    payload["attached"] = attached
    return ok(payload)


@bp.post("/api/auth/relogin")
def relogin():
    body = request.get_json(silent=True) or {}
    password = str(body.get("password", ""))
    if not password:
        return err("请输入密码。", 422)
    u = _user()
    c = u.client
    if not c.user_id:
        return err("尚未登录，无法重登。", 401)
    # 手动重登只应在会话失效/任务等待登录时进行：同一上游会话被多个浏览器共享，
    # 任务正常运行中无端重登会替换共享 token 并打断正在执行的任务。
    if u.tasks.active:
        t = getattr(u.tasks, "task", None)
        waiting = t is not None and getattr(t, "status", "") == TaskStatus.WAITING_LOGIN
        if not waiting:
            return err("任务运行中且会话正常，无需手动重登；如需更换密码请先停止任务。",
                       409, code="task_active")
    # 密码“先试后存”：认证成功才替换账号级持有的密码/会话；
    # 失败时 client.relogin 恢复原 token 与会话，避免单个浏览器输错密码
    # 破坏其他浏览器和正在运行的任务。
    if c.relogin(password):
        return ok({"message": "重新登录成功，任务已恢复。", "logged_in": True})
    return err(c.last_login_error or "重新登录失败。", 401)


@bp.post("/api/auth/logout")
def logout():
    try:
        closed = _manager().detach(getattr(g, "sid", None))
    except LoginConflict as e:
        return err(e.message, 409, code="task_active")
    g.sess = None
    g.sid = None
    g._clear_sid = True
    message = "已退出当前浏览器。" if not closed else "已退出登录。"
    return ok({"message": message, "account_closed": closed})


# ---- 批次 ----
def _batch_change_guard(u):
    if u.tasks.active:
        return err("任务运行期间不能发现、验证或切换批次。", 409, code="task_active")
    if not u.client.authenticated:
        return err("请先登录。", 401, code="auth_expired")
    return None


@bp.post("/api/batch/discover")
def discover():
    u = _user()
    blocked = _batch_change_guard(u)
    if blocked:
        return blocked
    try:
        return ok({"batch": u.client.discover_batch()})
    except ClientError as e:
        return client_err(e)


@bp.post("/api/batch/activate")
def activate_batch():
    u = _user()
    blocked = _batch_change_guard(u)
    if blocked:
        return blocked
    batch_id = str((request.get_json(silent=True) or {}).get("batch_id", "")).strip()
    if not batch_id:
        return err("请选择批次。", 422, code="invalid_batch")
    try:
        return ok({"batch": u.client.activate_batch(batch_id)})
    except ClientError as e:
        return client_err(e)


@bp.post("/api/batch/validate")
def validate_manual():
    body = request.get_json(silent=True) or {}
    batch_id = str(body.get("batch_id", "")).strip()
    if not batch_id:
        return err("请输入批次 ID。", 422, code="invalid_batch")
    u = _user()
    blocked = _batch_change_guard(u)
    if blocked:
        return blocked
    c = u.client
    res = c.validate_batch(batch_id)
    if not res["ok"]:
        status = 401 if res.get("reason") == "auth" else (409 if res.get("reason") == "not_open" else 422)
        return err(res.get("msg", "批次校验失败。"), status,
                   code=res.get("code", "invalid_batch"),
                   retryable=res.get("retryable", False))
    # 远端验证成功后才同时更新运行态和持久化手动配置。
    c._apply_batch(batch_id, "manual")
    u.cfg.update({"batch_mode": "manual", "batch_manual_id": batch_id})
    return ok({"batch": c.batch_runtime(), "message": "批次有效，已采用。"})


# ---- 课程 ----
def _require_batch():
    c = _user().client
    if not c.authenticated:
        return None, err("请先登录。", 401, code="auth_expired")
    if not c.batch_id or c.batch_state != "ready":
        code = "election_not_open" if c.batch_state == "not_open" else "batch_required"
        return None, err(c.batch_message or "尚无有效批次，请先发现、选择或手动验证。",
                         409, code=code, retryable=(code == "election_not_open"))
    return c, None


# 搜索/浏览：一次性分页拉全某课程类型的可选教学班，再做本地模糊过滤。
# 每页上限 999（上游分页），最多翻 6 页；结果过多时只返回前 _MAX_SEARCH_RESULTS 条并标记 truncated。
_SEARCH_MAX_RESULTS = 2000


def _load_all_rows(c, code: str, page_size: int = 0,
                   max_pages: int = 0) -> list:
    """把某课程类型全部可选教学班分页拉取并去重（JXBID）。"""
    page_size = page_size or _PAGE_SIZE
    max_pages = max_pages or _MAX_PAGES
    rows: list = []
    seen: set = set()
    for page in range(1, max_pages + 1):
        page_rows = c.list_classes(code, page_size=page_size, page=page)
        for r in page_rows:
            j = str(r.get("JXBID") or "")
            if j:
                if j in seen:
                    continue
                seen.add(j)
            rows.append(r)
        if len(page_rows) < page_size:
            break
    return rows


@bp.post("/api/courses/search")
def search():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    type_id = body.get("class_type_id")
    c, e = _require_batch()
    if e:
        return e
    code = None
    if isinstance(type_id, int) or str(type_id).isdigit():
        ref = ID_TO_TYPE.get(int(type_id))
        code = ref.code if ref else None
    elif type_id:
        code = str(type_id)
    if not code:
        return err("请选择课程类型。", 422)
    student_class = str(_user().cfg.settings.get("student_class", ""))
    try:
        all_rows = _load_all_rows(c, code)
    except ClientError as exc:
        return client_err(exc)
    matched = search_rows(all_rows, name)   # 留空 = 浏览该类型全部
    total = len(matched)
    shown = matched[:_SEARCH_MAX_RESULTS]
    out = [normalize_section(r, code, student_class) for r in shown]
    return ok({"count": total, "truncated": total > len(shown), "sections": out})


@bp.get("/api/courses/selected")
def selected():
    c, e = _require_batch()
    if e:
        return e
    try:
        rows = c.fetch_selected()
    except ClientError as e:
        return client_err(e)
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(selected_public(r))
    return ok({"count": len(out), "sections": out})


# 已选课程详情：以“当前学生班级口径”反查选课列表，尽量补全教师/时间/地点/容量。
_PAGE_SIZE = 999
_MAX_PAGES = 6


def _enrich_selected_detail(c, rows, student_class: str) -> list:
    """返回与前端可读结构一致的已选课程详情列表（含降级缺省字段）。"""
    ordered = []
    want: dict = {}   # jxbid -> class_type（仅已确认的类型代码可反查）
    for r in rows:
        if not isinstance(r, dict):
            continue
        ordered.append(r)
        j = str(r.get("JXBID") or r.get("jxbid") or "")
        code = str(r.get("teachingClassType") or r.get("clazzType") or "")
        if j and code in CODE_TO_TYPE and j not in want:
            want[j] = code
    # 按类型分组反查（分页直到命中或列表取完），找不到的用降级视图。
    by_type: dict = {}
    for j, code in want.items():
        by_type.setdefault(code, []).append(j)
    rich: dict = {}
    for code, jxbids in by_type.items():
        todo = set(jxbids)
        for page in range(1, _MAX_PAGES + 1):
            if not todo:
                break
            try:
                found = c.list_classes(code, page_size=_PAGE_SIZE, page=page)
            except AuthExpired:
                raise
            except ClientError:
                break   # 该类型列表请求失败：剩余项走降级视图
            for lr in found:
                j = str(lr.get("JXBID") or "")
                if j in todo:
                    todo.discard(j)
                    rich[j] = normalize_section(lr, code, student_class)
            # 返回不足一页 = 已是末页；否则继续翻页，直到命中全部或达到页数上限。
            if len(found) < _PAGE_SIZE:
                break
        # todo 中仍缺的项保持降级视图
    out = []
    for r in ordered:
        j = str(r.get("JXBID") or r.get("jxbid") or "")
        detail = rich.get(j)
        out.append(detail if detail is not None else selected_public_full(r))
    return out


@bp.get("/api/courses/selected/detail")
def selected_detail():
    c, e = _require_batch()
    if e:
        return e
    try:
        rows = c.fetch_selected()
    except ClientError as e:
        return client_err(e)
    student_class = str(_user().cfg.settings.get("student_class", ""))
    sections = _enrich_selected_detail(c, rows, student_class)
    return ok({"count": len(sections), "sections": sections})


# ---- 任务 ----
def _target_from_dict(d: dict) -> CourseTarget:
    t = CourseTarget.from_dict(d)
    if not t.jxbid:
        raise ValueError("目标缺少 jxbid")
    return t


@bp.post("/api/tasks")
def create_task():
    u = _user()
    c, e = _require_batch()
    if e:
        return e
    tasks = u.tasks
    if tasks.active:
        return err("已有任务在运行，请先停止。", 409, code="task_active")
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode", ""))
    if mode not in (TaskMode.POLL, TaskMode.GRAB, TaskMode.SWAP):
        return err("未知的任务模式。", 422)

    try:
        if mode == TaskMode.SWAP:
            raw_pairs = body.get("pairs") or []
            if not raw_pairs:
                return err("请至少配置一组“原课程 → 目标课程”改选对。", 422)
            # 服务端以已选课程为准重建原课程字段（含回退所需 secretVal）。
            selected_rows = {str(r.get("JXBID")): r for r in c.fetch_selected() if r.get("JXBID")}
            pairs = []
            for rp in raw_pairs:
                old_d = rp.get("old") or {}
                target_d = rp.get("target") or {}
                tgt = _target_from_dict(target_d)
                sr = selected_rows.get(str(old_d.get("jxbid", "")))
                if sr is None:
                    return err(f"原课程（jxbid={old_d.get('jxbid')}）当前不在已选列表中。", 422)
                old = old_from_selected_row(sr)
                pairs.append(SwapPair(old=old, target=tgt))
            task = tasks.start(TaskMode.SWAP, pairs=pairs,
                               student_class=str(u.cfg.settings.get("student_class", "")))
        else:
            raw_targets = body.get("targets") or []
            if not raw_targets:
                return err("请先在搜索结果中加入目标课程。", 422)
            targets = []
            seen = set()
            for rd in raw_targets:
                t = _target_from_dict(rd)
                if t.key in seen:
                    continue
                seen.add(t.key)
                targets.append(t)
            task = tasks.start(mode, targets=targets,
                               student_class=str(u.cfg.settings.get("student_class", "")))
    except ValueError as ex:
        return err(str(ex), 422)
    except RuntimeError as ex:
        return err(str(ex), 409, code="task_active")
    except ClientError as e:
        return client_err(e)
    return ok(tasks.summary())


@bp.get("/api/tasks/current")
def current_task():
    return ok(_user().tasks.summary())


@bp.get("/api/tasks/events")
def task_events():
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    events, last = _user().tasks.events(after)
    return ok({"events": events, "last_seq": last})


@bp.post("/api/tasks/stop")
def stop_task():
    tasks = _user().tasks
    tasks.request_stop()
    return ok({"message": "已请求停止。", "task": tasks.summary()})
