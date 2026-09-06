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
from .courses import normalize_section, old_from_selected_row, search_rows, selected_public
from .models import CourseTarget, ID_TO_TYPE, SwapPair, TaskMode
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


def err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": {"message": str(message)}}), status


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
            "batch": {"id": "", "source": "unknown", "validated_ts": "",
                      "mode": "auto", "manual_id": ""},
            "settings": None,
            "course_types": course_types,
            "task": None,
        }
    c = u.client
    s = u.cfg.public()
    logged = c.authenticated
    return {
        "auth": {
            "logged_in": logged,
            "user": _mask_id(c.user_id) if c.user_id else "",
            "password_held": c.password_held,
        },
        "batch": {
            "id": c.batch_id if c.batch_id else (s.get("batch_manual_id") if s.get("batch_mode") == "manual" else ""),
            "source": c.batch_source if c.batch_id else "unknown",
            "validated_ts": c.batch_validated_ts,
            "mode": s.get("batch_mode"),
            "manual_id": s.get("batch_manual_id"),
        },
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
        return err("任务运行期间不能修改：" + "、".join(blocked), 409)
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
    try:
        sess = _manager().login(user_id, password, proxy=proxy)
    except LoginConflict as e:
        return err(e.message, 409)
    except LoginError as e:
        return err(e.message, 401)
    except NetworkError as e:
        return err(e.message, 502)
    g.sess = sess
    g._set_sid = sess.sid
    return ok(_bootstrap_payload(sess))


@bp.post("/api/auth/relogin")
def relogin():
    body = request.get_json(silent=True) or {}
    password = str(body.get("password", ""))
    if not password:
        return err("请输入密码。", 422)
    c = _user().client
    if not c.user_id:
        return err("尚未登录，无法重登。", 401)
    c.set_password(password)
    if c.relogin():
        return ok({"message": "重新登录成功，任务已恢复。", "logged_in": True})
    return err(c.last_login_error or "重新登录失败。", 401)


@bp.post("/api/auth/logout")
def logout():
    u = _user()
    if u.tasks.active:
        return err("任务运行中，请先停止任务再退出登录。", 409)
    _manager().logout(u)
    g.sess = None
    g._clear_sid = True
    return ok({"message": "已退出登录。"})


# ---- 批次 ----
@bp.post("/api/batch/discover")
def discover():
    c = _user().client
    if not c.authenticated:
        return err("请先登录。", 401)
    try:
        result = c.discover_batch()
    except ClientError as e:
        return err(e.message, 502)
    return ok(result)


@bp.post("/api/batch/validate")
def validate_manual():
    body = request.get_json(silent=True) or {}
    batch_id = str(body.get("batch_id", "")).strip()
    if not batch_id:
        return err("请输入批次 ID。", 422)
    u = _user()
    c = u.client
    if not c.authenticated:
        return err("请先登录。", 401)
    # 保存为手动模式与手动值，然后验证；不静默覆盖用户手动输入。
    u.cfg.update({"batch_mode": "manual", "batch_manual_id": batch_id})
    res = c.validate_batch(batch_id)
    if res["ok"]:
        c._apply_batch(batch_id, "manual")
        return ok({"ok": True, "batch_id": batch_id, "source": "manual", "message": "批次有效，已采用。"})
    if res.get("reason") == "auth":
        return err("会话已过期，请重新登录后再验证批次。", 401)
    return err(f"批次校验失败：{res.get('msg', '')}", 422)


# ---- 课程 ----
def _require_batch():
    c = _user().client
    if not c.authenticated:
        return None, err("请先登录。", 401)
    if not c.batch_id:
        return None, err("尚无有效批次：请自动发现或在设置中手动填写并验证。", 409)
    return c, None


@bp.post("/api/courses/search")
def search():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    type_id = body.get("class_type_id")
    if name is None or not name:
        return err("请输入要搜索的课程完整名称或课程代码。", 422)
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
        rows = c.list_classes(code)
    except AuthExpired:
        return err("会话已过期，请重新登录。", 401)
    except ClientError as e:
        return err(e.message, 502)
    matched = search_rows(rows, name)
    out = [normalize_section(r, code, student_class) for r in matched]
    return ok({"count": len(out), "sections": out[:200]})


@bp.get("/api/courses/selected")
def selected():
    c, e = _require_batch()
    if e:
        return e
    try:
        rows = c.fetch_selected()
    except AuthExpired:
        return err("会话已过期，请重新登录。", 401)
    except ClientError as e:
        return err(e.message, 502)
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(selected_public(r))
    return ok({"count": len(out), "sections": out})


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
        return err("已有任务在运行，请先停止。", 409)
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
        return err(str(ex), 409)
    except AuthExpired:
        return err("会话已过期，请重新登录。", 401)
    except ClientError as e:
        return err(e.message, 502)
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
