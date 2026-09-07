"""数据接口被 302 退回选课 SPA 首页（/xsxk/profile/index.html）时的分类回归测试。

真实上游会把 clazz/list 等数据请求 302 到 profile 首页（HTML 而非 JSON），
旧代码把它笼统报成“无法识别的页面”。修复后应按“请求未获准访问该数据接口”
分类：认证态（页面内嵌 var batch）→ election_not_open（retryable）；页面无内嵌
批次（会话已不被接受）→ auth_expired。测试只构造字符串，不请求真实网络。
"""
import os
import tempfile
import unittest
from urllib.parse import urlparse

from kfc_college.client import AuthExpired, ClientError, ElectionClient
from kfc_college.config import ConfigStore

PROFILE = "https://byxk.buaa.edu.cn/xsxk/profile/index.html"
BATCH_A = "9b853aeab0564193a39683893a0be8b0"
BATCH_B = "cbfbe323d2ee4dec83542cd46dba9b65"


def _client(batch_id=""):
    # 指向一个不存在的路径，避免 mkstemp 生成空 JSON 文件触发“读取设置失败”日志。
    cfg = ConfigStore(os.path.join(tempfile.mkdtemp(), "settings.json"))
    c = ElectionClient(cfg)
    c.batch_id = batch_id
    return c


def _auth_html(batch_id=BATCH_A):
    return ('<!DOCTYPE html><html><head><title>学生选课</title></head><body>'
            '<script>browserType(); var batch = '
            '{"code":"%s","name":"2026年秋季学期研选本","schoolTerm":"2026-2027-1"}'
            '</script></body></html>' % batch_id)


class ProfileBounceClassificationTests(unittest.TestCase):
    def test_auth_page_same_batch_is_election_not_open(self):
        exc = _client(BATCH_A)._profile_bounce_error(
            urlparse(PROFILE), _auth_html(BATCH_A))
        self.assertIsNotNone(exc)
        self.assertEqual(exc.code, "election_not_open")
        self.assertTrue(exc.retryable)
        self.assertEqual(exc.http_status, 409)
        self.assertIn("尚未对本账号开放课程列表", exc.message)

    def test_auth_page_batch_changed_hints_rediscover(self):
        exc = _client(BATCH_A)._profile_bounce_error(
            urlparse(PROFILE), _auth_html(BATCH_B))
        self.assertIsNotNone(exc)
        self.assertEqual(exc.code, "election_not_open")
        self.assertIn("批次已更换", exc.message)

    def test_no_embedded_batch_is_auth_expired(self):
        exc = _client(BATCH_A)._profile_bounce_error(
            urlparse(PROFILE),
            '<html><title>学生选课</title><body><div id="app"></div></body></html>')
        self.assertIsInstance(exc, AuthExpired)
        self.assertEqual(exc.code, "auth_expired")

    def test_non_profile_page_falls_through(self):
        # 非 profile 页（例如真正无法识别的内容）不应被该方法吞掉，返回 None，
        # 交给既有的 未开放/维护/无法识别 分支处理。
        c = _client(BATCH_A)
        self.assertIsNone(c._profile_bounce_error(
            urlparse("https://byxk.buaa.edu.cn/xsxk/other/page.html"),
            "<html>garbage</html>"))
        self.assertIsNone(c._profile_bounce_error(
            urlparse("https://sso.buaa.edu.cn/login"), "<html>login</html>"))

    def test_profile_bounce_error_is_raised_by_response_json(self):
        # 端到端：_response_json 收到 profile 页时应抛 election_not_open，
        # 而不是 BadResponse“无法识别的页面”。
        import json
        import requests
        from kfc_college.client import BadResponse
        c = _client(BATCH_A)
        resp = requests.Response()
        resp.status_code = 200
        resp.url = PROFILE
        resp.encoding = "utf-8"
        resp.headers["Content-Type"] = "text/html; charset=UTF-8"
        resp._content = _auth_html(BATCH_A).encode("utf-8")
        try:
            c._response_json(resp, "课程列表")
            self.fail("应当抛出 election_not_open")
        except ClientError as e:
            self.assertEqual(e.code, "election_not_open")
            self.assertNotIsInstance(e, BadResponse)
            self.assertTrue(e.retryable)
        finally:
            # 释放 _content 缓存，避免跨用例共享
            resp.close()
        json.dumps({})  # keep import used for clarity


if __name__ == "__main__":
    unittest.main()
