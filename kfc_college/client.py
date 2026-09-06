"""选课系统内存会话客户端。

复用了原 main.py 的 SSO/CAS 登录顺序与 /xsxk/* 接口协议，但：
- 会话与 Cookie 由 requests.Session 在内存维护，不再读写 route / jsessionid 文件；
- 所有请求带超时；默认开启 TLS 证书校验（可由设置关闭以兼容特殊网络）；
- 批次支持自动发现 / 上次成功值 / 旧版迁移值 / 手动回退，校验后才启用。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

from .config import ConfigStore, remember_secret, log
from .models import CODE_TO_TYPE

BASE_URL = "https://byxk.buaa.edu.cn"
SSO_LOGIN = "https://sso.buaa.edu.cn/login"
XK_CAS = BASE_URL + "/xsxk/auth/cas"
BYXT_SERVICE = "https://byxt.buaa.edu.cn/jwapp/sys/homeapp/index.do"
LIST_URL = BASE_URL + "/xsxk/elective/buaa/clazz/list"
ADD_URL = BASE_URL + "/xsxk/elective/buaa/clazz/add"
DROP_URL = BASE_URL + "/xsxk/elective/clazz/del"
SELECTED_URL = BASE_URL + "/xsxk/elective/select"

# 旧版源码中手工调整过的批次，仅作为“最后尝试的迁移候选”，不再是源码常量来源。
LEGACY_BATCH_IDS = ["cbfbe323d2ee4dec83542cd46dba9b65"]

UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/127.0.6533.100 Safari/537.36")

# 批次候选的宽松形态，最终以服务端校验为准。
_BATCH_TOKEN = r"[A-Za-z0-9\-_]{8,64}"
_BATCH_RE = re.compile(
    r"(?:batchId|batch_id|batchid)\s*[=:]\s*[\"']?(" + _BATCH_TOKEN + r")",
    re.IGNORECASE,
)


class ClientError(Exception):
    """选课客户端错误基类（message 面向用户的中文提示）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LoginError(ClientError):
    pass


class AuthExpired(ClientError):
    pass


class NetworkError(ClientError):
    pass


class BadResponse(ClientError):
    pass


class BatchUnavailable(ClientError):
    pass


def _now_ts() -> str:
    return time.strftime("%H:%M:%S")


def _host_header(url: str) -> str:
    return urlparse(url).netloc


class ElectionClient:
    """一个实例对应一个登录账号；任务线程与请求线程通过锁共享同一客户端。"""

    def __init__(self, cfg: ConfigStore):
        self.cfg = cfg
        self._lock = threading.RLock()
        self.login_ready = threading.Event()  # 等待重登完成时由 Web 层 set

        self.sso_session: Optional[requests.Session] = None
        self.xk_session: Optional[requests.Session] = None
        self.token: str = ""
        self.user_id: str = ""
        self._password: str = ""

        # 批次运行时状态
        self.batch_id: str = ""
        self.batch_source: str = "unknown"   # login_location / html / last_success / legacy / manual / unknown
        self.batch_validated_ts: str = ""

        # 登录过程捕获到的候选（Location、URL、HTML）
        self._captured: List[str] = []

        # 用于登录阶段展示的最近消息
        self.last_login_error: str = ""

    # ---------- 基础属性 ----------
    @property
    def authenticated(self) -> bool:
        return bool(self.token and self.xk_session is not None)

    @property
    def password_held(self) -> bool:
        return bool(self._password)

    def _verify(self) -> bool:
        return bool(self.cfg.settings.get("tls_verify", True))

    # ---------- HTTP ----------
    def _proxy_url(self) -> str:
        p = self.cfg.settings.get("proxy", {}) or {}
        if p.get("enabled") and p.get("url"):
            return str(p["url"])
        return ""

    def _new_session(self) -> requests.Session:
        """构造一个新的 requests.Session：忽略环境变量代理，只按本用户代理配置。"""
        sess = requests.Session()
        sess.trust_env = False  # 服务器可能配了全局 HTTP_PROXY，绝不能影响某个用户
        url = self._proxy_url()
        sess.proxies = {"http": url, "https": url} if url else {}
        return sess

    def apply_proxy(self) -> None:
        """把当前配置的代理即时写进既有会话，无需重新登录。"""
        url = self._proxy_url()
        proxies = {"http": url, "https": url} if url else {}
        for s in (self.sso_session, self.xk_session):
            if s is not None:
                s.trust_env = False
                s.proxies = proxies

    def _get(self, url: str, *, session=None, **kw) -> requests.Response:
        sess = session or self._new_session()
        kw.setdefault("timeout", 12)
        try:
            resp = sess.get(url, verify=self._verify(), **kw)
        except requests.RequestException as e:
            raise NetworkError(f"网络请求失败：{e}")
        self._capture(resp)
        return resp

    def _post(self, url: str, *, session=None, headers=None, **kw) -> requests.Response:
        sess = session or self.xk_session or self._new_session()
        kw.setdefault("timeout", 15)
        try:
            resp = sess.post(url, headers=headers, verify=self._verify(), **kw)
        except requests.RequestException as e:
            raise NetworkError(f"网络请求失败：{e}")
        self._capture(resp)
        return resp

    def _capture(self, resp: requests.Response) -> None:
        """记录可用于批次发现的 Location / URL / 文本片段。"""
        try:
            loc = resp.headers.get("Location") or resp.url or ""
            if loc:
                self._captured.append(loc)
            text = getattr(resp, "text", "") or ""
            if text and "<" in text[:200]:
                self._captured.append(text)
            elif text:
                self._captured.append(text[:2000])
            # 防止内存无限增长
            self._captured = self._captured[-40:]
        except Exception:
            pass

    # ---------- 登录 ----------
    def login(self, user_id: str, password: str) -> "ElectionClient":
        self.user_id = str(user_id).strip()
        self._password = password
        remember_secret(password)
        try:
            self._do_login()
        except LoginError:
            self._password = ""
            self.login_ready.clear()
            raise
        # 登录成功后做批次解析
        try:
            self.resolve_batch()
        except ClientError as e:
            log().warning("批次解析失败，仍保持登录：%s", e)
        self.login_ready.set()
        return self

    def _sso_headers(self, *, with_referer=None, with_origin=False, castgc=False):
        h = {
            "Host": "sso.buaa.edu.cn",
            "User-Agent": UA_CHROME,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if castgc:
            h["Upgrade-Insecure-Requests"] = "1"
        if with_origin:
            h["Origin"] = "https://sso.buaa.edu.cn"
        if with_referer:
            h["Referer"] = with_referer
        return h

    def _xk_headers(self) -> dict:
        batch = self.batch_id or self.cfg.settings.get("batch_manual_id") or ""
        return {
            "Host": _host_header(BASE_URL),
            "Batchid": batch,
            "Authorization": self.token,
            "User-Agent": UA_CHROME,
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE_URL,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"{BASE_URL}/xsxk/elective/grablessons?batchId={batch}",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
        }

    def _do_login(self) -> None:
        self._captured = []
        sso = self._new_session()
        self.sso_session = sso

        # 1) 取 execution
        try:
            resp = sso.get(SSO_LOGIN, params={"service": BYXT_SERVICE},
                           headers=self._sso_headers(), timeout=12, verify=self._verify(),
                           allow_redirects=False)
        except requests.RequestException as e:
            raise LoginError(f"无法连接统一认证：{e}")
        soup = BeautifulSoup(resp.text, "html.parser")
        node = soup.find("input", {"name": "execution"})
        if node is None or node.get("value") is None:
            raise LoginError("统一认证登录页结构变化，无法获取 execution（可能维护或改版）。")
        execution = node["value"]

        # 2) 提交凭据取 CASTGC
        try:
            resp = sso.post(
                SSO_LOGIN,
                headers=self._sso_headers(with_origin=True,
                                          with_referer=SSO_LOGIN + "?service=" + BYXT_SERVICE),
                data={
                    "username": self.user_id,
                    "password": self._password,
                    "submit": "登录",
                    "type": "username_password",
                    "execution": execution,
                    "_eventId": "submit",
                },
                allow_redirects=False, timeout=12, verify=self._verify(),
            )
        except requests.RequestException as e:
            raise LoginError(f"提交登录信息失败：{e}")
        castgc = sso.cookies.get("CASTGC")
        if not castgc:
            raise LoginError("统一认证未返回会话（用户名或密码错误，或触发验证码）。")

        # 3) 换选课系统 ticket
        sso.cookies.set("CASTGC", castgc, domain="sso.buaa.edu.cn")
        try:
            resp = sso.get(SSO_LOGIN, params={"service": XK_CAS},
                           headers=self._sso_headers(castgc=True),
                           allow_redirects=False, timeout=12, verify=self._verify())
        except requests.RequestException as e:
            raise LoginError(f"获取选课系统跳转失败：{e}")
        location = resp.headers.get("Location") or resp.url or ""
        if not location:
            raise LoginError("统一认证未返回选课系统跳转地址（选课系统可能未开放）。")
        parsed = urlparse(location)
        q = parse_qs(parsed.query)
        ticket = (q.get("ticket") or [""])[0]
        if not ticket:
            # 兼容直接以 = 结尾的旧解析方式
            ticket = location.rstrip().split("=")[-1]
        self._captured.append(location)

        # 4) 换取 token / JSESSIONID / route（内存 Session 自动维护 Cookie）
        xk = self._new_session()
        self.xk_session = xk
        try:
            xk.get(XK_CAS, params={"ticket": ticket},
                   headers=self._xk_headers_without_batch(),
                   allow_redirects=False, timeout=12, verify=self._verify())
            xk.get(XK_CAS, headers=self._xk_headers_without_batch(),
                   allow_redirects=False, timeout=12, verify=self._verify())
        except requests.RequestException as e:
            raise LoginError(f"进入选课系统失败：{e}")
        token = xk.cookies.get("token")
        if not token:
            raise LoginError("选课系统未返回 token（可能选课未开放或账号无权限）。")
        self.token = token
        remember_secret(token)
        log().info("登录成功（账号 %s）", self._mask_user())

    def _xk_headers_without_batch(self):
        h = self._xk_headers()
        h["Batchid"] = ""
        return h

    def _mask_user(self) -> str:
        u = self.user_id
        return (u[:3] + "***") if len(u) > 3 else "***"

    # ---------- 批次 ----------
    def candidates(self) -> List[str]:
        """收集所有候选批次：登录捕获物 + 上次成功 + 旧版迁移 + 手动值。"""
        found = []
        for text in self._captured:
            for m in _BATCH_RE.findall(text):
                found.append(m)
            # URL 查询参数形式的 batchId
            for m in re.findall(r"[\?&](?:batchId|batch_id)[=]([^&\s\"']+)", text, re.IGNORECASE):
                found.append(m)
        s = self.cfg.settings
        if s.get("batch_last_id"):
            found.append(s["batch_last_id"])
        found.extend(LEGACY_BATCH_IDS)
        if s.get("batch_manual_id"):
            found.append(s["batch_manual_id"])
        # 去重保序
        seen, out = set(), []
        for v in found:
            v = v.strip()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def validate_batch(self, batch_id: str) -> dict:
        """用最小课程列表请求校验批次，返回 {ok, reason, msg}。"""
        if not self.authenticated:
            return {"ok": False, "reason": "not_login", "msg": "尚未登录"}
        self.batch_id = batch_id
        headers = self._xk_headers()
        body = {"teachingClassType": "TJKC", "pageNumber": 1, "pageSize": 1,
                "orderBy": "", "campus": "1"}
        try:
            resp = self._post(LIST_URL, headers=headers, json=body)
        except NetworkError as e:
            return {"ok": False, "reason": "network", "msg": e.message}
        except AuthExpired:
            return {"ok": False, "reason": "auth", "msg": "会话已过期，请重新登录"}
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "reason": "bad_response", "msg": "接口返回异常（非 JSON）"}
        if isinstance(data, dict):
            code = data.get("code")
            if code == 200:
                return {"ok": True, "reason": "ok", "msg": "批次有效"}
            if code == 401 or "token" in str(data.get("msg", "")).lower():
                return {"ok": False, "reason": "auth", "msg": "会话已过期，请重新登录"}
            return {"ok": False, "reason": "invalid",
                    "msg": str(data.get("msg", "批次无效或被拒绝"))[:120]}
        return {"ok": False, "reason": "bad_response", "msg": "接口返回结构异常"}

    def resolve_batch(self) -> dict:
        """按设置解析批次：手动模式只验证并采用手动值；自动模式跑发现链。"""
        s = self.cfg.settings
        if s.get("batch_mode") == "manual":
            manual = str(s.get("batch_manual_id") or "").strip()
            if not manual:
                raise BatchUnavailable("未填写手动批次 ID。")
            res = self.validate_batch(manual)
            if res["ok"]:
                self._apply_batch(manual, "manual")
                return {"ok": True, "source": "manual", "batch_id": manual,
                        "msg": "手动批次有效"}
            raise BatchUnavailable(f"手动批次校验失败：{res['msg']}")
        return self.discover_batch()

    def discover_batch(self) -> dict:
        """自动发现：验证登录捕获候选；不足时重新抓取 CAS 落地页；仍不行时要求手动输入。"""
        if not self.authenticated:
            return {"ok": False, "source": "unknown", "batch_id": "",
                    "msg": "请先登录"}
        # 尝试一次额外的落地页抓取以获得更丰富的候选
        try:
            self._get(XK_CAS, session=self.xk_session, headers=self._xk_headers_without_batch(),
                      allow_redirects=True, timeout=12)
        except Exception:
            pass
        for cand in self.candidates():
            res = self.validate_batch(cand)
            if res["ok"]:
                source = self._classify_source(cand)
                self._apply_batch(cand, source)
                return {"ok": True, "source": source, "batch_id": cand, "msg": "自动发现批次成功"}
            if res["reason"] == "auth":
                return {"ok": False, "source": "unknown", "batch_id": "",
                        "msg": "会话已过期，请重新登录后再发现批次"}
        return {"ok": False, "source": "unknown", "batch_id": "",
                "msg": "未能自动发现有效批次，请在设置中手动填写批次 ID 并验证"}

    def _classify_source(self, cand: str) -> str:
        manual = str(self.cfg.settings.get("batch_manual_id") or "")
        last = str(self.cfg.settings.get("batch_last_id") or "")
        if cand == manual:
            return "manual"
        if cand == last:
            return "last_success"
        if cand in LEGACY_BATCH_IDS:
            return "legacy"
        return "login_location"

    def _apply_batch(self, batch_id: str, source: str) -> None:
        self.batch_id = batch_id
        self.batch_source = source
        self.batch_validated_ts = _now_ts()
        s = self.cfg.settings
        if source != "manual" and s.get("batch_last_id") != batch_id:
            s["batch_last_id"] = batch_id
            self.cfg.save()
        log().info("批次已生效（来源=%s）", source)

    def clear_batch(self) -> None:
        self.batch_id = ""
        self.batch_source = "unknown"
        self.batch_validated_ts = ""

    # ---------- 登出 ----------
    def logout(self) -> None:
        with self._lock:
            self.token = ""
            self._password = ""
            self.user_id = ""
            self.xk_session = None
            self.sso_session = None
            self.clear_batch()
            self._captured = []
            self.login_ready.clear()
        log().info("已退出登录")

    def relogin(self) -> bool:
        """用内存密码自动重登（线程安全）。"""
        with self._lock:
            if not self._password or not self.user_id:
                return False
            try:
                self._do_login()
            except LoginError as e:
                self.last_login_error = e.message
                log().warning("自动重登失败：%s", e.message)
                return False
            try:
                self.resolve_batch()
            except ClientError:
                pass
            self.login_ready.set()
            return True

    def set_password(self, password: str) -> None:
        with self._lock:
            self._password = password
            remember_secret(password)

    # ---------- 选课 API ----------
    def _require_batch(self):
        if not self.batch_id:
            raise BatchUnavailable("尚无有效批次：请先自动发现或在设置中手动填写并验证")

    def _detect_auth(self, payload: dict):
        if not isinstance(payload, dict):
            raise BadResponse("接口返回非 JSON 结构")
        code = payload.get("code")
        msg = str(payload.get("msg", ""))
        if code == 401 or "token" in msg.lower():
            raise AuthExpired("会话已过期")

    def list_classes(self, class_type_code: str, page_size: int = 999, page: int = 1) -> List[dict]:
        self._require_batch()
        if class_type_code not in CODE_TO_TYPE:
            raise BadResponse(f"未知课程类型 {class_type_code}")
        body = {"teachingClassType": class_type_code, "pageNumber": page,
                "pageSize": page_size, "orderBy": "", "campus": "1"}
        resp = self._post(LIST_URL, headers=self._xk_headers(), json=body)
        try:
            data = resp.json()
        except ValueError:
            raise BadResponse("课程列表返回非 JSON")
        self._detect_auth(data)
        inner = data.get("data")
        if isinstance(inner, dict):
            return inner.get("rows") or []
        return []

    def add_class(self, class_type_code: str, jxbid: str, secret_val: str) -> dict:
        """返回 {ok, status, msg}；网络/超时/解析异常视为 unknown（需对账）。"""
        self._require_batch()
        headers = self._xk_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = {"clazzType": class_type_code, "clazzId": jxbid, "secretVal": secret_val}
        try:
            resp = self._post(ADD_URL, headers=headers, data=data)
        except NetworkError as e:
            return {"ok": False, "status": "unknown", "msg": f"网络异常（结果未知）：{e.message}"}
        try:
            payload = resp.json()
        except ValueError:
            return {"ok": False, "status": "unknown",
                    "msg": "选课响应非 JSON（结果未知，请以已选为准）"}
        self._detect_auth(payload)
        if payload.get("code") == 200:
            return {"ok": True, "status": "ok", "msg": "选课成功"}
        return {"ok": False, "status": "failed",
                "msg": str(payload.get("msg", "选课失败"))[:120]}

    def drop_class(self, jxbid: str, class_type_code: str = "") -> dict:
        self._require_batch()
        headers = self._xk_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = {"clazzId": jxbid}
        if class_type_code:
            data["clazzType"] = class_type_code
        try:
            resp = self._post(DROP_URL, headers=headers, data=data)
        except NetworkError as e:
            return {"ok": False, "status": "unknown", "msg": f"网络异常（结果未知）：{e.message}"}
        try:
            payload = resp.json()
        except ValueError:
            return {"ok": False, "status": "unknown",
                    "msg": "退课响应非 JSON（结果未知，请以已选为准）"}
        self._detect_auth(payload)
        if payload.get("code") == 200:
            return {"ok": True, "status": "ok", "msg": "退课成功"}
        return {"ok": False, "status": "failed",
                "msg": str(payload.get("msg", "退课失败"))[:120]}

    def fetch_selected(self) -> List[dict]:
        self._require_batch()
        resp = self._post(SELECTED_URL, headers=self._xk_headers())
        try:
            payload = resp.json()
        except ValueError:
            raise BadResponse("已选课程返回非 JSON")
        self._detect_auth(payload)
        data = payload.get("data")
        if isinstance(data, list):
            return data
        return []

    def selected_jxbid_set(self) -> set:
        rows = self.fetch_selected()
        out = set()
        for r in rows:
            j = r.get("JXBID") or r.get("jxbid")
            if j:
                out.add(str(j))
        return out
