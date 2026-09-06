"""应用工厂：多用户会话装配、可选访问口令、sid Cookie 中间件。

原先的进程级 SERVICES 单例（cfg/client/notifier/tasks）已删除——
登录后每个用户拥有自己的一套会话状态，由 sessions.UserManager 管理。
"""
from __future__ import annotations

import os

from flask import Flask, g, jsonify, request

from .config import BASE_DIR, DATA_DIR, setup_logging
from .sessions import SID_COOKIE, UserManager

_SID_MAX_AGE = int(os.getenv("SID_MAX_AGE", "2592000"))   # 30 天；本地/服务器通用
_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def create_app(data_dir: str = None,
               access_token: str = None,
               client_factory=None,
               notifier_factory=None,
               task_factory=None,
               idle_ttl=None,
               reaper_interval=None) -> Flask:
    setup_logging()
    if data_dir is None:
        data_dir = os.getenv("DATA_DIR") or DATA_DIR
    access_token = (access_token if access_token is not None
                    else (os.getenv("ACCESS_TOKEN") or None))

    users = UserManager(
        data_dir,
        client_factory=client_factory,
        notifier_factory=notifier_factory,
        task_factory=task_factory,
        idle_ttl=idle_ttl,
        reaper_interval=reaper_interval,
    )

    # 模板与静态文件位于项目根目录，而不是本包内。
    app = Flask(__name__,
                template_folder=os.path.join(BASE_DIR, "templates"),
                static_folder=os.path.join(BASE_DIR, "static"),
                static_url_path="/static")
    app.config.update(
        DATA_DIR=data_dir,
        ACCESS_TOKEN=access_token or None,
        USER_MANAGER=users,
        # 已通过口令校验的浏览器名单（进程内存；重启后需重新输入）
        GATE_NONCES=set(),
        SID_MAX_AGE=_SID_MAX_AGE,
        COOKIE_SECURE=_COOKIE_SECURE,
    )
    app.json.ensure_ascii = False

    # ---- 会话绑定 + 访问口令闸门 ----
    @app.before_request
    def _bind_request():
        g.sess = None
        g._set_sid = None
        g._clear_sid = False

        token = app.config.get("ACCESS_TOKEN")
        if token:
            path = request.path
            allow = (path == "/" or path.startswith("/static/") or path == "/api/gate")
            if not allow and request.cookies.get("kfc_gate") not in app.config["GATE_NONCES"]:
                return jsonify({"ok": False, "error": {
                    "code": "gate_required", "message": "需要访问口令。"}}), 403

        sid = request.cookies.get(SID_COOKIE)
        if sid:
            sess = users.get_by_sid(sid)
            if sess is not None:
                sess.touch()
                g.sess = sess
        return None

    @app.after_request
    def _cookies(resp):
        if getattr(g, "_set_sid", None):
            resp.set_cookie(SID_COOKIE, g._set_sid, max_age=app.config.get("SID_MAX_AGE", 0),
                            httponly=True, samesite="Lax",
                            secure=bool(app.config.get("COOKIE_SECURE")), path="/")
        if getattr(g, "_clear_sid", False):
            resp.set_cookie(SID_COOKIE, "", max_age=0, httponly=True, samesite="Lax",
                            secure=bool(app.config.get("COOKIE_SECURE")), path="/")
        return resp

    from . import web  # noqa: PLC0415
    app.register_blueprint(web.bp)

    # 全局 404（蓝图级错误处理器不会捕获路由未命中），/api 一律返回 JSON 信封。
    @app.errorhandler(404)
    def _not_found(_e):
        if request.path.startswith("/api"):
            return jsonify({"ok": False, "error": {"message": "接口不存在。"}}), 404
        return jsonify({"ok": False, "error": {"message": "页面不存在。"}}), 404

    return app


__all__ = ["create_app"]
