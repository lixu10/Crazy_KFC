import tempfile
import threading
import time
import unittest

from kfc_college.client import LoginError
from kfc_college.sessions import LoginConflict, UserManager


class FakeClient:
    login_calls = 0
    login_lock = threading.Lock()

    def __init__(self, cfg):
        self.cfg = cfg
        self._password = ""
        self.user_id = ""
        self.logged_out = False

    def login(self, uid, password):
        with self.login_lock:
            type(self).login_calls += 1
        time.sleep(0.04)
        if password == "wrong":
            raise LoginError("bad credentials")
        self.user_id = uid
        self._password = password
        return self

    def verify_held_password(self, password):
        import secrets
        return bool(self._password) and secrets.compare_digest(self._password, password)

    def logout(self):
        self.logged_out = True
        self._password = ""


class FakeNotifier:
    def __init__(self, cfg):
        self.cfg = cfg


class FakeTasks:
    def __init__(self, cfg, client, notifier):
        self.active = False
        self.stop_calls = 0

    def request_stop(self):
        self.stop_calls += 1
        self.active = False


class SessionTests(unittest.TestCase):
    def setUp(self):
        FakeClient.login_calls = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = UserManager(
            self.tmp.name,
            client_factory=FakeClient,
            notifier_factory=FakeNotifier,
            task_factory=FakeTasks,
            reaper_interval=0,
        )

    def tearDown(self):
        self.manager.shutdown()
        self.tmp.cleanup()

    def test_multiple_sids_share_one_upstream_session(self):
        first, sid1, attached1 = self.manager.login_or_attach("100001", "secret")
        second, sid2, attached2 = self.manager.login_or_attach("100001", "secret")
        self.assertIs(first, second)
        self.assertNotEqual(sid1, sid2)
        self.assertFalse(attached1)
        self.assertTrue(attached2)
        self.assertEqual(FakeClient.login_calls, 1)
        self.assertEqual(self.manager.count(), 1)
        self.assertEqual(self.manager.sid_count(), 2)

    def test_wrong_password_does_not_attach_or_relogin(self):
        session, sid, _ = self.manager.login_or_attach("100001", "secret")
        with self.assertRaises(LoginError):
            self.manager.login_or_attach("100001", "different")
        self.assertEqual(FakeClient.login_calls, 1)
        self.assertEqual(session.local_sids, {sid})

    def test_concurrent_first_login_runs_upstream_once(self):
        results = []
        barrier = threading.Barrier(3)

        def worker():
            barrier.wait()
            results.append(self.manager.login_or_attach("100001", "secret"))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        self.assertEqual(FakeClient.login_calls, 1)
        self.assertEqual(len(results), 2)
        self.assertIs(results[0][0], results[1][0])
        self.assertNotEqual(results[0][1], results[1][1])

    def test_detach_only_closes_last_browser(self):
        session, sid1, _ = self.manager.login_or_attach("100001", "secret")
        _, sid2, _ = self.manager.login_or_attach("100001", "secret")
        self.assertFalse(self.manager.detach(sid1))
        self.assertFalse(session.client.logged_out)
        self.assertTrue(self.manager.detach(sid2))
        self.assertTrue(session.client.logged_out)
        self.assertEqual(self.manager.count(), 0)

    def test_last_sid_with_active_task_is_kept(self):
        session, sid, _ = self.manager.login_or_attach("100001", "secret")
        session.tasks.active = True
        with self.assertRaises(LoginConflict):
            self.manager.detach(sid)
        self.assertIs(self.manager.get_by_sid(sid), session)
        self.assertIn(sid, session.local_sids)

    def test_shutdown_closes_shared_session_once(self):
        session, _, _ = self.manager.login_or_attach("100001", "secret")
        self.manager.login_or_attach("100001", "secret")
        self.manager.shutdown()
        self.assertTrue(session.closed)
        self.assertTrue(session.client.logged_out)
        self.assertEqual(self.manager.count(), 0)
        self.assertEqual(self.manager.sid_count(), 0)


if __name__ == "__main__":
    unittest.main()
