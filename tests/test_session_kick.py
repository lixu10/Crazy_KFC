"""单点登录互踢/会话失效 与 “轮次未开放” 的判别回归测试。

真实上游：同一账号只允许一个登录。已在本工具（采用 ready 批次）登录后，若再到
官方选课站重新登录，旧会话会被顶下线；随后本工具任一数据接口请求都被 302 退回
选课首页（HTML）。修复前这段被判成“轮次尚未开放”，任务不重登、UI 误报；修复后
ready 态数据请求被退回首页应判 auth_expired（提示重新登录）。测试只构造字符串，
不请求真实网络。
"""
import os
import tempfile
import unittest

import requests

from kfc_college.client import AuthExpired, ClientError, ElectionClient
from kfc_college.config import ConfigStore

PROFILE = "https://byxk.buaa.edu.cn/xsxk/profile/index.html"
ACTIVE_BATCH = "8e6a783d2d5241a2a9a976c2f90f8338"


def _fresh_client(batch_state="unavailable", batch_id=ACTIVE_BATCH):
    cfg = ConfigStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
    c = ElectionClient(cfg)
    c.batch_id = batch_id
    c.batch_state = batch_state
    return c


def _html(batch_id=ACTIVE_BATCH):
    return ('<!DOCTYPE html><html><head><title>学生选课</title></head><body>'
            '<script>var batch = {"code":"%s","name":"2026年秋季学期研选本",'
            '"schoolTerm":"2026-2027-1"}</script></body></html>' % batch_id)


def classify(c, text, url=PROFILE, status=200):
    """对给定 HTML 跑一次分类，返回抛出的 ClientError（或 None）。"""
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.encoding = "utf-8"
    r.headers["Content-Type"] = "text/html; charset=UTF-8"
    r._content = text.encode("utf-8")
    try:
        try:
            c._response_json(r, "课程列表")
        except ClientError as e:
            return e
    finally:
        r.close()
    return None


class SessionKickTests(unittest.TestCase):
    def test_ready_bounce_to_profile_is_auth_expired_not_not_open(self):
        # 已采用 ready 批次后数据接口仍被退回首页 → 会话被顶下线/失效，不是未开放。
        exc = classify(_fresh_client(batch_state="ready"), _html())
        self.assertIsInstance(exc, AuthExpired)
        self.assertEqual(exc.code, "auth_expired")
        self.assertEqual(exc.http_status, 401)
        self.assertIn("顶下线", exc.message)

    def test_ready_bounce_even_when_var_batch_matches_is_auth_expired(self):
        # 页面 var batch 恰好等于本账号活动批次（如研选本账号）时也不能误报未开放。
        exc = classify(_fresh_client(batch_state="ready"), _html(ACTIVE_BATCH))
        self.assertIsInstance(exc, AuthExpired)
        self.assertEqual(exc.code, "auth_expired")

    def test_not_ready_bounce_keeps_election_not_open(self):
        # 发现/验证阶段（未 ready）退回首页仍是原分类，不能被新规则吞掉。
        exc = classify(_fresh_client(batch_state="unavailable"), _html())
        self.assertIsInstance(exc, ClientError)
        self.assertEqual(exc.code, "election_not_open")
        self.assertNotIsInstance(exc, AuthExpired)

    def test_ready_but_non_profile_html_still_uses_word_rules(self):
        # 新规则只针对 profile 落地页；ready 态遇到“维护”HTML 仍按维护分类。
        url = "https://byxk.buaa.edu.cn/xsxk/maintenance/page.html"
        exc = classify(_fresh_client(batch_state="ready"),
                       "<html>系统维护中，请稍后</html>", url=url)
        self.assertIsInstance(exc, ClientError)
        self.assertEqual(exc.code, "upstream_maintenance")


if __name__ == "__main__":
    unittest.main()
