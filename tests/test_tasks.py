import unittest

from kfc_college.client import ClientError
from kfc_college.models import TaskMode, TaskRecord, TaskStatus
from kfc_college.tasks import TaskManager


class _FakeCfg:
    settings = {"poll_interval_sec": 5}


class _FakeClient:
    def __init__(self):
        self.batch_id = "batch000001"


class _FakeNotifier:
    def notify(self, subject, body):  # noqa: ARG002
        pass


class TaskErrorHandlingTests(unittest.TestCase):
    def _manager(self):
        return TaskManager(_FakeCfg(), _FakeClient(), _FakeNotifier())

    def _task(self):
        return TaskRecord(id=1, mode=TaskMode.POLL)

    def test_not_open_keeps_task_alive_and_emits_once(self):
        m = self._manager()
        t = self._task()
        exc = ClientError("尚未开放", code="election_not_open",
                          http_status=409, retryable=True)
        # 尚未开放是暂时状态：任务应继续等待，而不是永久失败。
        self.assertFalse(m._handle_client_error(t, exc))
        self.assertNotEqual(t.status, TaskStatus.FAILED)
        self.assertEqual(len(m._events), 1)
        self.assertEqual(m._events[0].kind, "wait_not_open")
        # 同一状态再次出现时不重复产生事件，避免每秒刷屏。
        self.assertFalse(m._handle_client_error(t, exc))
        self.assertEqual(len(m._events), 1)

    def test_batch_required_terminates_task(self):
        m = self._manager()
        t = self._task()
        exc = ClientError("活动批次已被清除", code="batch_required",
                          http_status=409, retryable=False)
        self.assertTrue(m._handle_client_error(t, exc))
        self.assertEqual(t.status, TaskStatus.FAILED)

    def test_schema_changed_terminates_task(self):
        m = self._manager()
        t = self._task()
        exc = ClientError("上游结构已变化", code="upstream_schema_changed",
                          http_status=502, retryable=False)
        self.assertTrue(m._handle_client_error(t, exc))
        self.assertEqual(t.status, TaskStatus.FAILED)

    def test_start_pins_active_batch(self):
        import time
        client = _FakeClient()
        cfg = _FakeCfg()
        m = TaskManager(cfg, client, _FakeNotifier())
        task = m.start(TaskMode.POLL, targets=[], student_class="2306")
        try:
            self.assertEqual(task.batch_id, "batch000001")
        finally:
            m.request_stop()
            time.sleep(0.3)  # 让 runner 线程在 cancel 上退出，避免跨用例泄漏


if __name__ == "__main__":
    unittest.main()
