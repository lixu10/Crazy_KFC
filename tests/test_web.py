import json
import secrets
import tempfile
import unittest

from kfc_college import create_app


class FakeWebClient:
    login_calls = 0

    def __init__(self, cfg):
        self.cfg = cfg
        self.user_id = ""
        self._password = ""
        self.authenticated = False
        self.password_held = False
        self.batch_id = "batchWeb001"
        self.batch_name = "测试批次"
        self.batch_source = "account_elective"
        self.batch_validated_ts = "12:00:00"
        self.batch_state = "ready"
        self.batch_message = ""
        self.last_login_error = ""
        self.login_ready = type("Ready", (), {"set": lambda self: None, "clear": lambda self: None})()

    def login(self, uid, password):
        type(self).login_calls += 1
        self.user_id = uid
        self._password = password
        self.authenticated = True
        self.password_held = True
        return self

    def verify_held_password(self, password):
        return secrets.compare_digest(self._password, password)

    def batch_runtime(self):
        return {
            "state": self.batch_state,
            "message": self.batch_message,
            "active": {
                "id": self.batch_id,
                "name": self.batch_name,
                "display_name": self.batch_name,
                "source": self.batch_source,
                "validated_ts": self.batch_validated_ts,
            } if self.batch_id else None,
            "choices": [{
                "id": self.batch_id,
                "name": self.batch_name,
                "display_name": self.batch_name,
                "source": self.batch_source,
                "category": "elective",
                "can_select": True,
                "no_select_reason": "",
                "begin_time": "",
                "end_time": "",
                "need_confirm": False,
                "is_confirmed": True,
                "status": "valid",
                "message": "批次有效",
            }],
        }

    def discover_batch(self):
        return self.batch_runtime()

    def activate_batch(self, batch_id):
        if batch_id != self.batch_id:
            raise AssertionError("unknown batch")
        return self.batch_runtime()

    def validate_batch(self, batch_id):
        return {"ok": True, "reason": "ok", "code": "ok", "msg": "批次有效", "retryable": False}

    def _apply_batch(self, batch_id, source, name=""):
        self.batch_id = batch_id
        self.batch_source = source
        self.batch_name = name
        self.batch_state = "ready"

    def apply_proxy(self):
        pass

    def logout(self):
        self.authenticated = False
        self.password_held = False
        self._password = ""

    def set_password(self, password):
        self._password = password

    def relogin(self):
        self.authenticated = True
        return True


class FakeNotifier:
    def __init__(self, cfg):
        self.cfg = cfg

    def send_test(self):
        return True, "发送成功"


class FakeTasks:
    def __init__(self, cfg, client, notifier):
        self.active = False

    def summary(self):
        return None

    def request_stop(self):
        self.active = False

    def events(self, after):
        return [], after


class WebTests(unittest.TestCase):
    def setUp(self):
        FakeWebClient.login_calls = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(
            data_dir=self.tmp.name,
            client_factory=FakeWebClient,
            notifier_factory=FakeNotifier,
            task_factory=FakeTasks,
            reaper_interval=0,
        )
        self.app.testing = True

    def tearDown(self):
        self.app.config["USER_MANAGER"].shutdown()
        self.tmp.cleanup()

    @staticmethod
    def login(client, password="secret-sentinel"):
        return client.post("/api/auth/login", json={"user_id": "100001", "password": password})

    def test_two_browsers_share_account_and_logout_independently(self):
        first = self.app.test_client()
        second = self.app.test_client()
        self.assertEqual(self.login(first).status_code, 200)
        response = self.login(second)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["data"]["attached"])
        self.assertEqual(FakeWebClient.login_calls, 1)
        manager = self.app.config["USER_MANAGER"]
        self.assertEqual(manager.count(), 1)
        self.assertEqual(manager.sid_count(), 2)
        first.put("/api/settings", json={"student_class": "2306"})
        self.assertEqual(second.get("/api/bootstrap").get_json()["data"]["settings"]["student_class"], "2306")
        self.assertEqual(first.post("/api/auth/logout", json={}).status_code, 200)
        self.assertEqual(manager.count(), 1)
        self.assertTrue(second.get("/api/bootstrap").get_json()["data"]["auth"]["logged_in"])
        self.assertEqual(second.post("/api/auth/logout", json={}).status_code, 200)
        self.assertEqual(manager.count(), 0)

    def test_repeated_login_in_same_browser_does_not_orphan_sid(self):
        client = self.app.test_client()
        self.assertEqual(self.login(client).status_code, 200)
        self.assertEqual(self.login(client).status_code, 200)
        manager = self.app.config["USER_MANAGER"]
        self.assertEqual(FakeWebClient.login_calls, 1)
        self.assertEqual(manager.sid_count(), 1)
        client.post("/api/auth/logout", json={})
        self.assertEqual(manager.count(), 0)

    def test_wrong_password_does_not_create_sid(self):
        first = self.app.test_client()
        second = self.app.test_client()
        self.login(first)
        response = self.login(second, "different")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(FakeWebClient.login_calls, 1)
        self.assertEqual(self.app.config["USER_MANAGER"].sid_count(), 1)

    def test_final_browser_cannot_logout_while_task_active(self):
        client = self.app.test_client()
        self.login(client)
        session = self.app.config["USER_MANAGER"].get_by_uid("100001")
        session.tasks.active = True
        response = client.post("/api/auth/logout", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "task_active")
        self.assertTrue(client.get("/api/bootstrap").get_json()["data"]["auth"]["logged_in"])

    def test_bootstrap_does_not_leak_password_or_saved_secrets(self):
        client = self.app.test_client()
        self.login(client)
        client.put("/api/settings", json={
            "proxy": {"enabled": True, "url": "http://user:proxy-secret@localhost:8080"},
            "smtp": {"password": "smtp-secret"},
        })
        raw = json.dumps(client.get("/api/bootstrap").get_json(), ensure_ascii=False)
        self.assertNotIn("secret-sentinel", raw)
        self.assertNotIn("proxy-secret", raw)
        self.assertNotIn("smtp-secret", raw)
        self.assertIn("***", raw)

    def test_batch_changes_are_guarded_while_task_runs(self):
        client = self.app.test_client()
        self.login(client)
        session = self.app.config["USER_MANAGER"].get_by_uid("100001")
        session.tasks.active = True
        for path in ("/api/batch/discover", "/api/batch/activate", "/api/batch/validate"):
            response = client.post(path, json={"batch_id": "batchWeb001"})
            self.assertEqual(response.status_code, 409, path)
            self.assertEqual(response.get_json()["error"]["code"], "task_active")

    def test_logout_all_logs_out_every_browser_of_account(self):
        first = self.app.test_client()
        second = self.app.test_client()
        self.assertEqual(self.login(first).status_code, 200)
        self.assertEqual(self.login(second).status_code, 200)
        manager = self.app.config["USER_MANAGER"]
        self.assertEqual(manager.sid_count(), 2)
        response = first.post("/api/auth/logout-all", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["data"]["browsers_closed"], 2)
        self.assertEqual(manager.count(), 0)
        self.assertEqual(manager.sid_count(), 0)
        boot = second.get("/api/bootstrap").get_json()["data"]
        self.assertFalse(boot["auth"]["logged_in"])

    def test_logout_all_rejected_while_task_active(self):
        client = self.app.test_client()
        self.login(client)
        session = self.app.config["USER_MANAGER"].get_by_uid("100001")
        session.tasks.active = True
        response = client.post("/api/auth/logout-all", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "task_active")
        self.assertTrue(client.get("/api/bootstrap").get_json()["data"]["auth"]["logged_in"])


if __name__ == "__main__":
    unittest.main()
