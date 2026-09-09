"""#2 自动重登退避封顶 与 #47 非就绪批次自动重检的离线单测。"""
import tempfile
import time
import unittest

from kfc_college.client import (AUTO_RELOGIN_MAX_FAILS, DEFAULT_BATCH_ID,
                                ElectionClient, LoginError)
from kfc_college.config import ConfigStore


class BadLoginClient(ElectionClient):
    """_do_login 恒抛 invalid_credentials：用于测自动重登退避/封顶。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.user_id = "100001"
        self._password = "old-secret"
        self.token = "token-x"
        self.sso_session = object()
        self.xk_session = object()
        self.login_ready.set()
        self._do_login_calls = 0

    def _do_login(self):
        self._do_login_calls += 1
        raise LoginError("统一认证用户名或密码错误。", code="invalid_credentials")


class RecheckClient(ElectionClient):
    """resolve_batch 直接采用默认批次：用于测非就绪状态的自动重检。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.token = "token-x"
        self.user_id = "100001"
        self.xk_session = object()
        self._password = "secret"
        self._resolve_calls = 0

    def resolve_batch(self):
        self._resolve_calls += 1
        self._apply_batch(DEFAULT_BATCH_ID, "account_elective", "补退选含重修")
        return self.batch_runtime()


class ReloginBackoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigStore(self.tmp.name + "/settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_relogin_backs_off_without_hammering_upstream(self):
        client = BadLoginClient(self.cfg)
        self.assertFalse(client.relogin())
        self.assertEqual(client._do_login_calls, 1)
        self.assertEqual(client._auto_relogin_fails, 1)
        self.assertGreater(client._auto_relogin_deadline, time.time())
        # 冷却期内自动重试直接拒绝，不再打上游
        self.assertFalse(client.relogin())
        self.assertEqual(client._do_login_calls, 1)

    def test_manual_relogin_bypasses_backoff_and_is_not_counted(self):
        client = BadLoginClient(self.cfg)
        client._auto_relogin_deadline = time.time() + 99999
        self.assertFalse(client.relogin("typo-password"))
        # 手动路径不受冷却限制，仍真正尝试
        self.assertEqual(client._do_login_calls, 1)
        # 手动输错不计入自动退避计数
        self.assertEqual(client._auto_relogin_fails, 0)

    def test_auto_relogin_cap_clears_held_password_and_stops(self):
        client = BadLoginClient(self.cfg)
        client._auto_relogin_fails = AUTO_RELOGIN_MAX_FAILS - 1
        client._auto_relogin_deadline = 0.0
        self.assertFalse(client.relogin())
        self.assertEqual(client._auto_relogin_fails, AUTO_RELOGIN_MAX_FAILS)
        self.assertFalse(client.password_held)          # 内存密码已清空
        self.assertIn("重新登录", client.last_login_error)
        # 清空后再自动重登，直接因无密码而拒绝，不打上游
        calls = client._do_login_calls
        self.assertFalse(client.relogin())
        self.assertEqual(client._do_login_calls, calls)


class AutoRecheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigStore(self.tmp.name + "/settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_open_is_rechecked_to_ready(self):
        client = RecheckClient(self.cfg)
        client.batch_state = "not_open"
        client.batch_message = "统一认证已成功，但课程服务尚未开放"
        self.assertTrue(client.maybe_auto_recheck())
        self.assertEqual(client._resolve_calls, 1)
        self.assertEqual(client.batch_state, "ready")

    def test_recheck_skips_when_ready_authenticated_missing_or_in_cooldown(self):
        client = RecheckClient(self.cfg)
        # ready 状态不触发
        client.batch_state = "ready"
        self.assertFalse(client.maybe_auto_recheck())
        # 未认证不触发
        client.token = ""
        client.batch_state = "not_open"
        self.assertFalse(client.maybe_auto_recheck())
        # 认证恢复后触发一次
        client.token = "token-x"
        self.assertTrue(client.maybe_auto_recheck())
        # 成功后若又回到未开放，冷却期内不再触发
        client.batch_state = "not_open"
        self.assertFalse(client.maybe_auto_recheck())
        self.assertEqual(client._resolve_calls, 1)


if __name__ == "__main__":
    unittest.main()
