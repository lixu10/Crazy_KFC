"""应用工厂：装配配置、客户端、通知器与任务管理器。"""
from __future__ import annotations

import os

from flask import Flask, jsonify, request

from .config import BASE_DIR, ConfigStore, setup_logging
from .client import ElectionClient
from .notifier import EmailNotifier, register_secrets
from .tasks import TaskManager


def create_app() -> Flask:
    setup_logging()
    cfg = ConfigStore()
    register_secrets(cfg)

    client = ElectionClient(cfg)
    notifier = EmailNotifier(cfg)
    tasks = TaskManager(cfg, client, notifier)

    # 模板与静态文件位于项目根目录，而不是本包内。
    app = Flask(__name__,
                template_folder=os.path.join(BASE_DIR, "templates"),
                static_folder=os.path.join(BASE_DIR, "static"),
                static_url_path="/static")
    app.config["SERVICES"] = {
        "cfg": cfg,
        "client": client,
        "notifier": notifier,
        "tasks": tasks,
    }
    app.json.ensure_ascii = False

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
