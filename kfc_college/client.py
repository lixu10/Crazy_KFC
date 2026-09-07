"""选课系统内存会话客户端。

复用真实 BUAA SSO/CAS 与 /xsxk/* 接口；密码、token、Cookie、secretVal
只保存在内存。批次先发现、逐个验证，再明确采用；失败候选不会污染活动批次。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from typing import List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

from .config import ConfigStore, log, remember_secret
from .models import CODE_TO_TYPE

BASE_URL = "https://byxk.buaa.edu.cn"
SSO_LOGIN = "https://sso.buaa.edu.cn/login"
XK_CAS = BASE_URL + "/xsxk/auth/cas"
BYXT_SERVICE = "https://byxt.buaa.edu.cn/jwapp/sys/homeapp/index.do"
STUDENT_INFO_URL = BASE_URL + "/xsxk/web/studentInfo"
ELECTIVE_USER_URL = BASE_URL + "/xsxk/elective/user"
LIST_URL = BASE_URL + "/xsxk/elective/buaa/clazz/list"
ADD_URL = BASE_URL + "/xsxk/elective/buaa/clazz/add"
DROP_URL = BASE_URL + "/xsxk/elective/clazz/del"
SELECTED_URL = BASE_URL + "/xsxk/elective/select"

LEGACY_BATCH_IDS = ["cbfbe323d2ee4dec83542cd46dba9b65"]
MAX_BATCH_CANDIDATES = 12

UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/127.0.6533.100 Safari/537.36")

_BATCH_TOKEN = r"[A-Za-z0-9\-_]{8,64}"
_BATCH_FULL_RE = re.compile(r"^" + _BATCH_TOKEN + r"$")
_BATCH_RE = re.compile(
    r"(?:batchId|batch_id|batchid)\s*[=:]\s*[\"']?(" + _BATCH_TOKEN + r")",
    re.IGNORECASE,
)

_NOT_OPEN_WORDS = (
    "未开放", "尚未开放", "暂未开放", "尚未开始", "未到选课", "不在选课时间",
    "选课时间未到", "选课已结束", "不在开放时间", "不可选课",
)
_MAINTENANCE_WORDS = ("系统维护", "维护中", "暂停服务", "系统升级", "服务维护")
_AUTH_WORDS = ("token失效", "token过期", "登录失效", "登录过期", "重新登录", "未登录")


class ClientError(Exception):
    """选课客户端错误；结构化字段供 API 安全映射。"""

    default_code = "upstream_error"
    default_http_status = 502
    default_retryable = False

    def __init__(self, message: str, *, code: Optional[str] = None,
                 http_status: Optional[int] = None, retryable: Optional[bool] = None):
        super().__init__(message)
        self.message = str(message)
        self.code = code or self.default_code
        self.http_status = self.default_http_status if http_status is None else int(http_status)
        self.retryable = self.default_retryable if retryable is None else bool(retryable)


class LoginError(ClientError):
    default_code = "invalid_credentials"
    default_http_status = 401


class AuthExpired(ClientError):
    default_code = "auth_expired"
    default_http_status = 401


class NetworkError(ClientError):
    default_code = "upstream_unavailable"
    default_http_status = 502
    default_retryable = True


class BadResponse(ClientError):
    default_code = "upstream_bad_response"
    default_http_status = 502
    default_retryable = True


class BatchUnavailable(ClientError):
    default_code = "batch_required"
    default_http_status = 409


def _now_ts() -> str:
    return time.strftime("%H:%M:%S")


def _host_header(url: str) -> str:
    return urlparse(url).netloc


def _code(payload: dict) -> str:
    value = payload.get("code") if isinstance(payload, dict) else ""
    return str(value).strip()


def _text_flag(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes")


def _safe_message(value, fallback: str = "上游服务拒绝了请求。") -> str:
    text = " ".join(str(value or "").split())
    return (text[:160] or fallback)


def _contains_any(text: str, words) -> bool:
    low = str(text or "").lower()
    return any(word.lower() in low for word in words)


def _choice_id(raw: dict) -> str:
    for key in ("code", "batchId", "batch_id", "id"):
        value = str(raw.get(key, "") or "").strip()
        if _BATCH_FULL_RE.fullmatch(value):
            return value
    return ""


def _choice_name(raw: dict) -> str:
    for key in ("name", "batchName", "batch_name"):
        value = " ".join(str(raw.get(key, "") or "").split())
        if value:
            return value[:120]
    return ""


def _display_name(batch_id: str, name: str) -> str:
    if name:
        return name
    suffix = batch_id[-8:] if batch_id else "—"
    return f"批次 …{suffix}（名称未获取）"


def _public_choice(choice: dict) -> dict:
    """只保留可公开给当前账号浏览器的批次字段。"""
    return {
        "id": str(choice.get("id", "")),
        "name": str(choice.get("name", "")),
        "display_name": str(choice.get("display_name", "")),
        "source": str(choice.get("source", "unknown")),
        "category": str(choice.get("category", "fallback")),
        "can_select": choice.get("can_select"),
        "no_select_reason": str(choice.get("no_select_reason", "")),
        "begin_time": str(choice.get("begin_time", "")),
        "end_time": str(choice.get("end_time", "")),
        "need_confirm": bool(choice.get("need_confirm", False)),
        "is_confirmed": bool(choice.get("is_confirmed", False)),
        "status": str(choice.get("status", "pending")),
        "message": str(choice.get("message", "")),
    }


class ElectionClient:
    """一个实例对应一个账号；共享浏览器通过账号级 RLock 串行化上游状态。"""

    def __init__(self, cfg: ConfigStore):
        self.cfg = cfg
        self._lock = threading.RLock()
        self.login_ready = threading.Event()

        self.sso_session: Optional[requests.Session] = None
        self.xk_session: Optional[requests.Session] = None
        self.token = ""
        self.user_id = ""
        self._password = ""

        self.batch_id = ""
        self.batch_name = ""
        self.batch_source = "unknown"
        self.batch_validated_ts = ""
        self.batch_state = "unavailable"
        self.batch_message = "尚未发现有效批次。"
        self.batch_choices: List[dict] = []

        self._captured: List[str] = []
        self._captured_choices: List[dict] = []
        self._discovery_errors: List[ClientError] = []
        self.last_login_error = ""

    # ---------- 基础属性 ----------
    @property
    def authenticated(self) -> bool:
        return bool(self.token and self.xk_session is not None)

    @property
    def password_held(self) -> bool:
        return bool(self._password)

    def verify_held_password(self, password: str) -> bool:
        with self._lock:
            held = self._password
            return bool(held) and secrets.compare_digest(held, str(password or ""))

    def _verify(self) -> bool:
        return bool(self.cfg.settings.get("tls_verify", True))

    # ---------- HTTP ----------
    def _proxy_url(self) -> str:
        p = self.cfg.settings.get("proxy", {}) or {}
        return str(p["url"]) if p.get("enabled") and p.get("url") else ""

    def _new_session(self) -> requests.Session:
        sess = requests.Session()
        sess.trust_env = False
        url = self._proxy_url()
        sess.proxies = {"http": url, "https": url} if url else {}
        return sess

    def apply_proxy(self) -> None:
        with self._lock:
            url = self._proxy_url()
            proxies = {"http": url, "https": url} if url else {}
            for sess in (self.sso_session, self.xk_session):
                if sess is not None:
                    sess.trust_env = False
                    sess.proxies = proxies

    def _get(self, url: str, *, session=None, **kw) -> requests.Response:
        sess = session or self._new_session()
        kw.setdefault("timeout", 12)
        try:
            resp = sess.get(url, verify=self._verify(), **kw)
        except requests.RequestException:
            raise NetworkError("无法连接选课系统，请稍后重试或检查该账号的网络代理。")
        self._capture(resp)
        return resp

    def _post(self, url: str, *, session=None, headers=None, **kw) -> requests.Response:
        sess = session or self.xk_session or self._new_session()
        kw.setdefault("timeout", 15)
        try:
            resp = sess.post(url, headers=headers, verify=self._verify(), **kw)
        except requests.RequestException:
            raise NetworkError("无法连接选课系统，请稍后重试或检查该账号的网络代理。")
        self._capture(resp)
        return resp

    def _capture(self, resp: requests.Response) -> None:
        """只保留 URL 和白名单批次字段，不缓存原始认证 HTML/JSON。"""
        try:
            loc = resp.headers.get("Location") or resp.url or ""
            if loc:
                self._captured.append(str(loc)[:2000])
                self._captured = self._captured[-40:]
            text = getattr(resp, "text", "") or ""
            if text and "batch" in text.lower():
                self._captured_choices = self._merge_choices(
                    self._captured_choices, self._inline_batch_choices(text))
        except Exception:  # discovery evidence is best-effort
            pass

    # ---------- 安全响应分类 ----------
    def _response_json(self, resp: requests.Response, context: str) -> dict:
        status = int(getattr(resp, "status_code", 0) or 0)
        final = urlparse(str(getattr(resp, "url", "") or ""))
        text = str(getattr(resp, "text", "") or "")
        location = str(getattr(resp, "headers", {}).get("Location", "") or "")
        if 300 <= status < 400 and urlparse(location).netloc.lower() == "sso.buaa.edu.cn":
            raise AuthExpired("统一认证会话已过期，请重新登录。")
        if status in (401, 403):
            raise AuthExpired("统一认证会话已过期，请重新登录。")
        if status == 429:
            raise ClientError("选课系统请求过于频繁，请稍后再试。",
                              code="upstream_rate_limited", http_status=429, retryable=True)
        if status >= 500:
            raise ClientError("选课系统暂时不可用或正在维护，请稍后再试。",
                              code="upstream_unavailable", http_status=503, retryable=True)
        if final.netloc.lower() == "sso.buaa.edu.cn":
            raise AuthExpired("统一认证会话已过期，请重新登录。")
        if not text.strip():
            raise BadResponse(f"{context}返回空响应，请稍后重试。")
        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError):
            # 只有无法解析为 JSON 的页面才按整页文案分类；成功 JSON 中的
            # 课程名或某个不可选批次原因可能同样包含“未开放”等词。
            if _contains_any(text, _NOT_OPEN_WORDS):
                raise ClientError("统一认证已成功，但选课系统尚未开放课程服务。",
                                  code="election_not_open", http_status=409, retryable=True)
            if _contains_any(text, _MAINTENANCE_WORDS):
                raise ClientError("选课系统正在维护，请稍后再试。",
                                  code="upstream_maintenance", http_status=503, retryable=True)
            raise BadResponse(f"{context}返回了无法识别的页面，请稍后重试。")
        if not isinstance(payload, dict):
            raise ClientError(f"{context}返回结构已变化。", code="upstream_schema_changed",
                              http_status=502, retryable=False)
        self._detect_auth(payload)
        return payload

    def _detect_auth(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ClientError("上游接口返回结构已变化。", code="upstream_schema_changed")
        msg = str(payload.get("msg", "") or payload.get("message", ""))
        if _code(payload) == "401" or "token" in msg.lower() or _contains_any(msg, _AUTH_WORDS):
            raise AuthExpired("统一认证会话已过期，请重新登录。")

    def _business_error(self, payload: dict, fallback: str) -> ClientError:
        msg = _safe_message(payload.get("msg") or payload.get("message"), fallback)
        if _contains_any(msg, _NOT_OPEN_WORDS):
            return ClientError("统一认证已成功，但选课系统尚未开放课程服务。",
                               code="election_not_open", http_status=409, retryable=True)
        if _contains_any(msg, _MAINTENANCE_WORDS):
            return ClientError("选课系统正在维护，请稍后再试。",
                               code="upstream_maintenance", http_status=503, retryable=True)
        return BadResponse(msg)

    # ---------- 登录 ----------
    def login(self, user_id: str, password: str) -> "ElectionClient":
        with self._lock:
            self.user_id = str(user_id).strip()
            self._password = str(password or "")
            remember_secret(self._password)
            try:
                self._do_login()
            except LoginError:
                self._password = ""
                self.login_ready.clear()
                raise
            try:
                self.resolve_batch()
            except ClientError as exc:
                self._set_batch_failure(exc)
                log().warning("批次解析失败，仍保持登录（%s）", exc.code)
            self.login_ready.set()
            return self

    def _sso_headers(self, *, with_referer=None, with_origin=False, castgc=False):
        headers = {
            "Host": "sso.buaa.edu.cn", "User-Agent": UA_CHROME,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if castgc:
            headers["Upgrade-Insecure-Requests"] = "1"
        if with_origin:
            headers["Origin"] = "https://sso.buaa.edu.cn"
        if with_referer:
            headers["Referer"] = with_referer
        return headers

    def _xk_headers(self, batch_id: Optional[str] = None) -> dict:
        batch = self.batch_id if batch_id is None else str(batch_id or "")
        return {
            "Host": _host_header(BASE_URL), "Batchid": batch,
            "Authorization": self.token, "User-Agent": UA_CHROME,
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json, text/plain, */*", "Origin": BASE_URL,
            "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": f"{BASE_URL}/xsxk/elective/grablessons?batchId={batch}",
            "Accept-Language": "zh-CN,zh;q=0.9", "Connection": "keep-alive",
        }

    def _xk_headers_without_batch(self):
        return self._xk_headers("")

    def _do_login(self) -> None:
        self._captured = []
        self._captured_choices = []
        self.token = ""
        sso = self._new_session()
        self.sso_session = sso
        try:
            resp = sso.get(SSO_LOGIN, params={"service": BYXT_SERVICE},
                           headers=self._sso_headers(), timeout=12, verify=self._verify(),
                           allow_redirects=False)
        except requests.RequestException:
            raise LoginError("无法连接统一认证，请检查网络或该账号的代理设置。",
                             code="upstream_unavailable", http_status=502, retryable=True)
        soup = BeautifulSoup(resp.text, "html.parser")
        node = soup.find("input", {"name": "execution"})
        if node is None or node.get("value") is None:
            raise LoginError("统一认证登录页暂不可用或结构已变化。",
                             code="upstream_bad_response", http_status=502, retryable=True)
        execution = node["value"]
        try:
            resp = sso.post(
                SSO_LOGIN,
                headers=self._sso_headers(with_origin=True,
                                          with_referer=SSO_LOGIN + "?service=" + BYXT_SERVICE),
                data={"username": self.user_id, "password": self._password, "submit": "登录",
                      "type": "username_password", "execution": execution, "_eventId": "submit"},
                allow_redirects=False, timeout=12, verify=self._verify())
        except requests.RequestException:
            raise LoginError("提交统一认证登录信息失败，请检查网络后重试。",
                             code="upstream_unavailable", http_status=502, retryable=True)
        castgc = sso.cookies.get("CASTGC")
        if not castgc:
            raise LoginError("统一认证未返回会话：请检查学号、密码或验证码要求。")
        sso.cookies.set("CASTGC", castgc, domain="sso.buaa.edu.cn")
        try:
            resp = sso.get(SSO_LOGIN, params={"service": XK_CAS},
                           headers=self._sso_headers(castgc=True), allow_redirects=False,
                           timeout=12, verify=self._verify())
        except requests.RequestException:
            raise LoginError("获取选课系统跳转失败，请稍后重试。",
                             code="upstream_unavailable", http_status=502, retryable=True)
        location = resp.headers.get("Location") or resp.url or ""
        if not location:
            raise LoginError("统一认证成功，但选课系统当前未提供登录入口。",
                             code="election_not_open", http_status=409, retryable=True)
        ticket = (parse_qs(urlparse(location).query).get("ticket") or [""])[0]
        if not ticket:
            ticket = location.rstrip().split("=")[-1]
        self._captured.append(location)
        xk = self._new_session()
        self.xk_session = xk
        try:
            first = xk.get(XK_CAS, params={"ticket": ticket},
                           headers=self._xk_headers_without_batch(), allow_redirects=False,
                           timeout=12, verify=self._verify())
            self._capture(first)
            second = xk.get(XK_CAS, headers=self._xk_headers_without_batch(),
                            allow_redirects=False, timeout=12, verify=self._verify())
            self._capture(second)
        except requests.RequestException:
            raise LoginError("进入选课系统失败，请稍后重试。",
                             code="upstream_unavailable", http_status=502, retryable=True)
        token = xk.cookies.get("token")
        if not token:
            raise LoginError("选课系统未返回 token，可能尚未开放或账号当前无权限。",
                             code="election_not_open", http_status=409, retryable=True)
        self.token = token
        remember_secret(token)
        log().info("登录成功（账号 %s）", self._mask_user())

    def _mask_user(self) -> str:
        return (self.user_id[:3] + "***") if len(self.user_id) > 3 else "***"

    # ---------- 批次解析 ----------
    def _choice(self, raw: dict, source: str, category: str) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None
        batch_id = _choice_id(raw)
        if not batch_id:
            return None
        name = _choice_name(raw)
        can_raw = raw.get("canSelect")
        can_select = None if can_raw is None or str(can_raw).strip() == "" else _text_flag(can_raw)
        need_confirm = _text_flag(raw.get("needConfirm"))
        is_confirmed = _text_flag(raw.get("isConfirmed"))
        reason = _safe_message(raw.get("noSelectReason"), "") if raw.get("noSelectReason") else ""
        status = "pending"
        message = "待验证"
        if can_select is False:
            status = "unavailable"
            message = reason or "该账号当前不可选择此批次。"
        elif need_confirm and not is_confirmed:
            status = "confirmation_required"
            message = "需先在官方选课页面确认批次说明。"
        return {
            "id": batch_id, "name": name, "display_name": _display_name(batch_id, name),
            "source": source, "category": category, "can_select": can_select,
            "no_select_reason": reason,
            "begin_time": str(raw.get("beginTime", "") or "")[:40],
            "end_time": str(raw.get("endTime", "") or "")[:40],
            "need_confirm": need_confirm, "is_confirmed": is_confirmed,
            "status": status, "message": message,
        }

    def _fallback_choice(self, batch_id: str, source: str) -> Optional[dict]:
        batch_id = str(batch_id or "").strip()
        if not _BATCH_FULL_RE.fullmatch(batch_id):
            return None
        name = ""
        if batch_id == str(self.cfg.settings.get("batch_last_id", "")):
            name = str(self.cfg.settings.get("batch_last_name", "") or "")[:120]
        return {
            "id": batch_id, "name": name, "display_name": _display_name(batch_id, name),
            "source": source, "category": "fallback", "can_select": None,
            "no_select_reason": "", "begin_time": "", "end_time": "",
            # 回退来源不知道确认状态，不能用 False 覆盖账号元数据中的 True。
            "need_confirm": None, "is_confirmed": None,
            "status": "pending", "message": "待验证",
        }

    def _merge_choices(self, base: List[dict], incoming: List[dict]) -> List[dict]:
        out = [dict(item) for item in base]
        index = {item.get("id"): item for item in out}
        for raw in incoming:
            if not raw or not raw.get("id"):
                continue
            old = index.get(raw["id"])
            if old is None:
                item = dict(raw)
                out.append(item)
                index[item["id"]] = item
                continue
            for key in ("name", "begin_time", "end_time", "no_select_reason"):
                if raw.get(key) and not old.get(key):
                    old[key] = raw[key]
            if old.get("source") in ("legacy", "manual", "last_success", "login_location") \
                    and raw.get("source") not in ("legacy", "manual", "last_success"):
                old["source"] = raw["source"]
                old["category"] = raw.get("category", old.get("category"))
            for key in ("can_select", "need_confirm", "is_confirmed"):
                if raw.get(key) is not None:
                    old[key] = raw[key]
            if raw.get("status") in ("unavailable", "confirmation_required"):
                old["status"] = raw["status"]
                old["message"] = raw.get("message", "")
            old["display_name"] = _display_name(old["id"], old.get("name", ""))
        return out

    def _student_choices(self, payload: dict) -> List[dict]:
        data = payload.get("data") if isinstance(payload, dict) else None
        student = data.get("student") if isinstance(data, dict) else None
        if not isinstance(student, dict) and isinstance(data, dict):
            student = data
        if not isinstance(student, dict):
            return []
        out = []
        for key, source, category in (
            ("electiveBatchList", "account_elective", "elective"),
            ("expElectiveBatchList", "account_experimental", "experimental"),
        ):
            rows = student.get(key)
            if isinstance(rows, list):
                for raw in rows:
                    item = self._choice(raw, source, category)
                    if item:
                        out.append(item)
        current = student.get("currentBatch")
        item = self._choice(current, "account_current", "elective")
        if item:
            out.append(item)
        return out

    def _inline_batch_choices(self, text: str) -> List[dict]:
        out = []
        decoder = json.JSONDecoder()
        for pattern, source in ((r"\bvar\s+batch\s*=", "landing"),
                                (r"\bcurrentBatch\s*[:=]", "landing_current")):
            for match in re.finditer(pattern, text, re.IGNORECASE):
                start = match.end()
                while start < len(text) and text[start].isspace():
                    start += 1
                try:
                    raw, _ = decoder.raw_decode(text[start:])
                except (ValueError, json.JSONDecodeError):
                    continue
                item = self._choice(raw, source, "landing")
                if item:
                    out.append(item)
        return out

    def _url_choices(self) -> List[dict]:
        out = []
        for text in self._captured:
            for value in _BATCH_RE.findall(text):
                item = self._fallback_choice(unquote(value), "login_location")
                if item:
                    out.append(item)
            try:
                query = parse_qs(urlparse(text).query)
            except ValueError:
                query = {}
            for key in ("batchId", "batch_id"):
                for value in query.get(key, []):
                    item = self._fallback_choice(unquote(value), "login_location")
                    if item:
                        out.append(item)
        return out

    def _metadata_request(self) -> List[dict]:
        choices: List[dict] = []
        try:
            resp = self._post(STUDENT_INFO_URL, headers=self._xk_headers_without_batch(), json={})
            payload = self._response_json(resp, "批次信息")
            if _code(payload) == "200":
                choices = self._student_choices(payload)
            elif _code(payload):
                raise self._business_error(payload, "无法读取账号批次信息。")
        except AuthExpired:
            raise
        except ClientError as exc:
            self._discovery_errors.append(exc)
            log().info("账号批次信息暂不可用（%s）", exc.code)
        return choices

    def _user_metadata_request(self, seed: str) -> List[dict]:
        if not seed:
            return []
        try:
            resp = self._post(ELECTIVE_USER_URL, headers=self._xk_headers(seed),
                              json={"batchId": seed})
            payload = self._response_json(resp, "批次上下文")
            if _code(payload) == "200":
                return self._student_choices(payload)
        except AuthExpired:
            raise
        except ClientError as exc:
            self._discovery_errors.append(exc)
            log().info("批次上下文暂不可用（%s）", exc.code)
        return []

    def candidates(self) -> List[str]:
        """兼容旧调用：返回当前可发现候选 ID。"""
        with self._lock:
            return [choice["id"] for choice in self._collect_choices()]

    def _collect_choices(self) -> List[dict]:
        self._discovery_errors = []
        structured = self._metadata_request()
        choices = self._merge_choices([], structured)
        seed = ""
        if choices:
            seed = choices[0]["id"]
        else:
            seed = str(self.batch_id or self.cfg.settings.get("batch_last_id") or "")
        if seed:
            choices = self._merge_choices(choices, self._user_metadata_request(seed))
        try:
            landing = self._get(XK_CAS, session=self.xk_session,
                                headers=self._xk_headers_without_batch(), allow_redirects=True)
            choices = self._merge_choices(choices, self._inline_batch_choices(landing.text or ""))
        except AuthExpired:
            raise
        except ClientError as exc:
            self._discovery_errors.append(exc)
        choices = self._merge_choices(choices, self._captured_choices)
        choices = self._merge_choices(choices, self._url_choices())
        settings = self.cfg.settings
        for value, source in (
            (settings.get("batch_last_id"), "last_success"),
            *((value, "legacy") for value in LEGACY_BATCH_IDS),
            (settings.get("batch_manual_id"), "manual"),
        ):
            item = self._fallback_choice(str(value or ""), source)
            if item:
                choices = self._merge_choices(choices, [item])
        return choices[:MAX_BATCH_CANDIDATES]

    def validate_batch(self, batch_id: str) -> dict:
        """验证候选但绝不修改活动批次。"""
        with self._lock:
            candidate = str(batch_id or "").strip()
            if not self.authenticated:
                return {"ok": False, "reason": "auth", "code": "auth_expired",
                        "msg": "尚未登录", "retryable": False}
            if not _BATCH_FULL_RE.fullmatch(candidate):
                return {"ok": False, "reason": "invalid", "code": "invalid_batch",
                        "msg": "批次 ID 格式不正确。", "retryable": False}
            body = {"teachingClassType": "TJKC", "pageNumber": 1, "pageSize": 1,
                    "orderBy": "", "campus": "1"}
            try:
                resp = self._post(LIST_URL, headers=self._xk_headers(candidate), json=body)
                payload = self._response_json(resp, "课程列表")
                if _code(payload) == "200":
                    return {"ok": True, "reason": "ok", "code": "ok",
                            "msg": "批次有效", "retryable": False}
                exc = self._business_error(payload, "批次无效或当前不可用。")
                return {"ok": False, "reason": "invalid", "code": exc.code,
                        "msg": exc.message, "retryable": exc.retryable}
            except AuthExpired as exc:
                return {"ok": False, "reason": "auth", "code": exc.code,
                        "msg": exc.message, "retryable": exc.retryable}
            except ClientError as exc:
                reason = "not_open" if exc.code == "election_not_open" else "unavailable"
                return {"ok": False, "reason": reason, "code": exc.code,
                        "msg": exc.message, "retryable": exc.retryable}

    def resolve_batch(self) -> dict:
        with self._lock:
            settings = self.cfg.settings
            if settings.get("batch_mode") == "manual":
                manual = str(settings.get("batch_manual_id") or "").strip()
                if not manual:
                    raise BatchUnavailable("未填写手动批次 ID。")
                result = self.validate_batch(manual)
                if result["ok"]:
                    existing = next((c for c in self.batch_choices if c.get("id") == manual), None)
                    self._apply_batch(manual, "manual", (existing or {}).get("name", ""))
                    return self.batch_runtime()
                raise ClientError(result["msg"], code=result.get("code"),
                                  http_status=409 if result.get("reason") == "not_open" else 422,
                                  retryable=result.get("retryable", False))
            return self.discover_batch()

    def discover_batch(self, preferred_id: str = "", require_preferred: bool = False) -> dict:
        with self._lock:
            if not self.authenticated:
                raise AuthExpired("请先登录。")
            choices = self._collect_choices()
            any_not_open = any(exc.code == "election_not_open" for exc in self._discovery_errors)
            any_unavailable = any(exc.code in {"upstream_unavailable", "upstream_maintenance",
                                               "upstream_rate_limited", "upstream_bad_response"}
                                  for exc in self._discovery_errors)
            for choice in choices:
                if choice["status"] in ("unavailable", "confirmation_required"):
                    continue
                result = self.validate_batch(choice["id"])
                choice["status"] = "valid" if result["ok"] else result["reason"]
                choice["message"] = result["msg"]
                if result["reason"] == "auth":
                    self.batch_choices = choices
                    self.batch_state = "auth_expired"
                    self.batch_message = result["msg"]
                    raise AuthExpired(result["msg"])
                any_not_open = any_not_open or result["reason"] == "not_open"
                any_unavailable = any_unavailable or result["reason"] == "unavailable"
            self.batch_choices = choices
            valid = [item for item in choices if item.get("status") == "valid"]
            preferred = str(preferred_id or self.batch_id or
                            self.cfg.settings.get("batch_last_id") or "")
            selected = next((item for item in valid if item["id"] == preferred), None)
            if require_preferred and preferred and selected is None:
                self.clear_batch(keep_choices=True)
                self.batch_state = "unavailable"
                self.batch_message = "重新登录后原批次已不可用，已阻止切换到其他批次。"
                return self.batch_runtime()
            if selected is None and len(valid) == 1:
                selected = valid[0]
            if selected is not None:
                self._apply_batch(selected["id"], selected["source"], selected.get("name", ""))
            else:
                # 本轮没有可采用的已验证批次时，立即撤销旧活动批次，避免
                # bootstrap 继续暴露一个已经失效或尚未开放的批次 ID。
                self.clear_batch(keep_choices=True)
                if len(valid) > 1:
                    self.batch_state = "selection_required"
                    self.batch_message = f"发现 {len(valid)} 个可用批次，请选择。"
                elif any_not_open:
                    self.batch_state = "not_open"
                    self.batch_message = "统一认证已成功，但选课系统尚未开放课程服务。"
                elif any_unavailable:
                    self.batch_state = "unavailable"
                    self.batch_message = "批次服务暂时不可用，请稍后重试。"
                else:
                    self.batch_state = "unavailable"
                    self.batch_message = "未找到可用批次，可稍后重新发现或手动验证。"
            return self.batch_runtime()

    def activate_batch(self, batch_id: str) -> dict:
        with self._lock:
            candidate = str(batch_id or "").strip()
            choice = next((item for item in self.batch_choices if item.get("id") == candidate), None)
            if choice is None:
                raise BatchUnavailable("该批次不在当前发现结果中，请重新发现后再选择。")
            if choice.get("can_select") is False:
                raise BatchUnavailable(choice.get("no_select_reason") or "该批次当前不可选择。")
            if choice.get("status") == "confirmation_required" or (
                    choice.get("need_confirm") and not choice.get("is_confirmed")):
                raise BatchUnavailable("该批次需要先在官方选课页面确认说明。")
            result = self.validate_batch(candidate)
            choice["status"] = "valid" if result["ok"] else result["reason"]
            choice["message"] = result["msg"]
            if not result["ok"]:
                raise ClientError(result["msg"], code=result.get("code"),
                                  http_status=409 if result.get("reason") == "not_open" else 422,
                                  retryable=result.get("retryable", False))
            self._apply_batch(candidate, choice.get("source", "selection"), choice.get("name", ""))
            return self.batch_runtime()

    def _apply_batch(self, batch_id: str, source: str, name: str = "") -> None:
        self.batch_id = str(batch_id)
        self.batch_name = str(name or "")[:120]
        self.batch_source = str(source or "unknown")
        self.batch_validated_ts = _now_ts()
        self.batch_state = "ready"
        self.batch_message = f"当前批次：{_display_name(self.batch_id, self.batch_name)}"
        settings = self.cfg.settings
        changed = (settings.get("batch_last_id") != self.batch_id or
                   settings.get("batch_last_name") != self.batch_name)
        settings["batch_last_id"] = self.batch_id
        settings["batch_last_name"] = self.batch_name
        if changed:
            self.cfg.save()
        log().info("批次已生效（来源=%s）", self.batch_source)

    def _set_batch_failure(self, exc: ClientError) -> None:
        if exc.code == "election_not_open":
            self.batch_state = "not_open"
        elif exc.code == "auth_expired":
            self.batch_state = "auth_expired"
        else:
            self.batch_state = "unavailable"
        self.batch_message = exc.message

    def batch_runtime(self) -> dict:
        with self._lock:
            active = None
            if self.batch_id:
                active = {
                    "id": self.batch_id, "name": self.batch_name,
                    "display_name": _display_name(self.batch_id, self.batch_name),
                    "source": self.batch_source, "validated_ts": self.batch_validated_ts,
                }
            return {
                "state": self.batch_state, "message": self.batch_message,
                "active": active,
                "choices": [_public_choice(choice) for choice in self.batch_choices],
            }

    def clear_batch(self, keep_choices: bool = False) -> None:
        self.batch_id = ""
        self.batch_name = ""
        self.batch_source = "unknown"
        self.batch_validated_ts = ""
        if not keep_choices:
            self.batch_choices = []
            self.batch_state = "unavailable"
            self.batch_message = "尚未发现有效批次。"

    # ---------- 登出 / 重登 ----------
    def logout(self) -> None:
        with self._lock:
            self.token = ""
            self._password = ""
            self.user_id = ""
            self.xk_session = None
            self.sso_session = None
            self.clear_batch()
            self._captured = []
            self._captured_choices = []
            self.login_ready.clear()
        log().info("已退出登录")

    def relogin(self, password: Optional[str] = None) -> bool:
        with self._lock:
            if not self.user_id:
                return False
            candidate_password = self._password if password is None else str(password or "")
            if not candidate_password:
                return False

            # 手动重登密码先试后存。_do_login 会替换共享 token/session；若认证
            # 本身失败，恢复原账号状态，避免一个浏览器输错密码破坏其他浏览器和任务。
            snapshot = {
                "password": self._password,
                "token": self.token,
                "sso_session": self.sso_session,
                "xk_session": self.xk_session,
                "captured": self._captured,
                "captured_choices": self._captured_choices,
                "ready": self.login_ready.is_set(),
            }
            previous_id = self.batch_id
            self._password = candidate_password
            remember_secret(candidate_password)
            try:
                self._do_login()
            except LoginError as exc:
                self._password = snapshot["password"]
                self.token = snapshot["token"]
                self.sso_session = snapshot["sso_session"]
                self.xk_session = snapshot["xk_session"]
                self._captured = snapshot["captured"]
                self._captured_choices = snapshot["captured_choices"]
                if snapshot["ready"]:
                    self.login_ready.set()
                else:
                    self.login_ready.clear()
                self.last_login_error = exc.message
                log().warning("自动重登失败（%s）", exc.code)
                return False
            try:
                if previous_id:
                    result = self.discover_batch(previous_id, require_preferred=True)
                    if not result.get("active") or result["active"].get("id") != previous_id:
                        # 原批次不可恢复时保持“无活动批次”不变量，绝不把旧 ID
                        # 重新塞回运行态，也不静默切换到其他批次。
                        self.clear_batch(keep_choices=True)
                        self.batch_state = "unavailable"
                        self.batch_message = "重新登录后原批次已不可用，已阻止切换到其他批次。"
                        self.last_login_error = self.batch_message
                        return False
                else:
                    self.resolve_batch()
            except ClientError as exc:
                self.last_login_error = exc.message
                self._set_batch_failure(exc)
                return False
            self.last_login_error = ""
            self.login_ready.set()
            return True

    def set_password(self, password: str) -> None:
        with self._lock:
            self._password = str(password or "")
            remember_secret(self._password)

    # ---------- 选课 API ----------
    def _require_batch(self):
        if not self.batch_id or self.batch_state != "ready":
            code = "election_not_open" if self.batch_state == "not_open" else "batch_required"
            status = 409
            raise BatchUnavailable(self.batch_message or "尚无有效批次。", code=code,
                                   http_status=status,
                                   retryable=(self.batch_state == "not_open"))

    def list_classes(self, class_type_code: str, page_size: int = 999,
                     page: int = 1) -> List[dict]:
        with self._lock:
            self._require_batch()
            if class_type_code not in CODE_TO_TYPE:
                raise BadResponse(f"未知课程类型 {class_type_code}",
                                  code="upstream_schema_changed", retryable=False)
            body = {"teachingClassType": class_type_code, "pageNumber": page,
                    "pageSize": page_size, "orderBy": "", "campus": "1"}
            payload = self._response_json(
                self._post(LIST_URL, headers=self._xk_headers(), json=body), "课程列表")
            if _code(payload) != "200":
                raise self._business_error(payload, "课程列表请求失败。")
            inner = payload.get("data")
            if not isinstance(inner, dict) or not isinstance(inner.get("rows"), list):
                raise ClientError("课程列表返回结构已变化。", code="upstream_schema_changed",
                                  http_status=502, retryable=False)
            return inner["rows"]

    def add_class(self, class_type_code: str, jxbid: str, secret_val: str) -> dict:
        with self._lock:
            self._require_batch()
            headers = self._xk_headers()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            try:
                resp = self._post(ADD_URL, headers=headers,
                                  data={"clazzType": class_type_code, "clazzId": jxbid,
                                        "secretVal": secret_val})
                payload = self._response_json(resp, "选课响应")
            except AuthExpired:
                raise
            except ClientError as exc:
                return {"ok": False, "status": "unknown", "msg": f"结果未知：{exc.message}"}
            if _code(payload) == "200":
                return {"ok": True, "status": "ok", "msg": "选课成功"}
            return {"ok": False, "status": "failed",
                    "msg": _safe_message(payload.get("msg"), "选课失败")}

    def drop_class(self, jxbid: str, class_type_code: str = "") -> dict:
        with self._lock:
            self._require_batch()
            headers = self._xk_headers()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = {"clazzId": jxbid}
            if class_type_code:
                data["clazzType"] = class_type_code
            try:
                payload = self._response_json(
                    self._post(DROP_URL, headers=headers, data=data), "退课响应")
            except AuthExpired:
                raise
            except ClientError as exc:
                return {"ok": False, "status": "unknown", "msg": f"结果未知：{exc.message}"}
            if _code(payload) == "200":
                return {"ok": True, "status": "ok", "msg": "退课成功"}
            return {"ok": False, "status": "failed",
                    "msg": _safe_message(payload.get("msg"), "退课失败")}

    def fetch_selected(self) -> List[dict]:
        with self._lock:
            self._require_batch()
            payload = self._response_json(
                self._post(SELECTED_URL, headers=self._xk_headers()), "已选课程")
            if _code(payload) != "200":
                raise self._business_error(payload, "已选课程请求失败。")
            data = payload.get("data")
            if not isinstance(data, list):
                raise ClientError("已选课程返回结构已变化。", code="upstream_schema_changed",
                                  http_status=502, retryable=False)
            return data

    def selected_jxbid_set(self) -> set:
        return {str(row.get("JXBID") or row.get("jxbid")) for row in self.fetch_selected()
                if row.get("JXBID") or row.get("jxbid")}
