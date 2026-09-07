import tempfile
import unittest

from kfc_college.client import (ClientError, DEFAULT_BATCH_ID,
                                ElectionClient)
from kfc_college.config import ConfigStore


class FakeResponse:
    def __init__(self, payload=None, *, status=200, text=None,
                 url="https://byxk.buaa.edu.cn/xsxk/elective/buaa/clazz/list"):
        self.payload = payload
        self.status_code = status
        self.url = url
        self.headers = {}
        self.history = []
        self.text = text if text is not None else ("{}" if payload is not None else "")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class StubClient(ElectionClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.token = "token-sentinel"
        self.user_id = "100001"
        self.xk_session = object()
        self.responses = []
        self.fixed_choices = None

    def _post(self, url, **kwargs):
        if not self.responses:
            raise AssertionError(f"unexpected POST {url}")
        return self.responses.pop(0)

    def _get(self, url, **kwargs):
        return FakeResponse({}, text="<html></html>", url=url)

    def _collect_choices(self):
        if self.fixed_choices is not None:
            return [dict(item) for item in self.fixed_choices]
        return super()._collect_choices()


class LandingStub(StubClient):
    """可自定义 landing 页 HTML 的桩：用于测“默认批次失效后回退 landing seed”。"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.landing_html = "<html></html>"

    def _get(self, url, **kwargs):
        return FakeResponse({}, text=self.landing_html, url=url)


def not_open_html():
    return FakeResponse(ValueError(), text="<html>选课系统尚未开放</html>")


def student_info_payload(batch_id, name, can_select="1"):
    return {"code": "200", "data": {"student": {
        "electiveBatchList": [{"code": batch_id, "name": name, "canSelect": can_select}],
    }}}


def choice(batch_id, name, **extra):
    out = {
        "id": batch_id,
        "name": name,
        "display_name": name,
        "source": "account_elective",
        "category": "elective",
        "can_select": True,
        "no_select_reason": "",
        "begin_time": "",
        "end_time": "",
        "need_confirm": False,
        "is_confirmed": True,
        "status": "pending",
        "message": "待验证",
    }
    out.update(extra)
    return out


class BatchClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigStore(self.tmp.name + "/settings.json")
        self.client = StubClient(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_student_info_parses_named_normal_and_experimental_batches(self):
        payload = {"data": {"student": {
            "electiveBatchList": [{"code": "normalBatch001", "name": "秋季第一轮", "canSelect": "1"}],
            "expElectiveBatchList": [{"code": "experiment002", "name": "实验批次", "canSelect": "0", "noSelectReason": "暂不可选"}],
            "unrelated": {"code": "courseCode999", "name": "不是批次"},
        }}}
        choices = self.client._student_choices(payload)
        self.assertEqual([x["id"] for x in choices], ["normalBatch001", "experiment002"])
        self.assertEqual(choices[0]["name"], "秋季第一轮")
        self.assertEqual(choices[1]["category"], "experimental")
        self.assertFalse(choices[1]["can_select"])

    def test_inline_parser_only_reads_known_batch_objects(self):
        html = '''<script>
        var course = {"code":"courseCode999","name":"课程"};
        var batch = {"code":"batchInline01","name":"补退选"};
        </script>'''
        choices = self.client._inline_batch_choices(html)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["id"], "batchInline01")
        self.assertEqual(choices[0]["name"], "补退选")

    def test_validation_accepts_string_success_without_mutating_active(self):
        self.client._apply_batch("oldBatch0001", "manual", "旧批次")
        self.client.responses = [FakeResponse({"code": "200", "data": {"rows": []}})]
        result = self.client.validate_batch("newBatch0002")
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.batch_id, "oldBatch0001")
        self.assertEqual(self.client.batch_name, "旧批次")

    def test_not_open_html_is_classified_and_does_not_mutate(self):
        self.client._apply_batch("oldBatch0001", "manual", "旧批次")
        self.client.responses = [FakeResponse(ValueError(), text="<html>选课系统尚未开放</html>")]
        result = self.client.validate_batch("newBatch0002")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "election_not_open")
        self.assertEqual(self.client.batch_id, "oldBatch0001")

    def test_response_classification_covers_rate_limit_and_sso(self):
        with self.assertRaises(ClientError) as rate:
            self.client._response_json(FakeResponse({}, status=429), "课程列表")
        self.assertEqual(rate.exception.code, "upstream_rate_limited")
        with self.assertRaises(ClientError) as auth:
            self.client._response_json(FakeResponse(ValueError(), text='<input name="execution"><input type="password">',
                                                    url="https://sso.buaa.edu.cn/login"), "课程列表")
        self.assertEqual(auth.exception.code, "auth_expired")

    def test_multiple_valid_choices_require_selection_without_history(self):
        self.client.fixed_choices = [choice("validBatch001", "第一轮"), choice("validBatch002", "第二轮")]
        self.client.responses = [
            FakeResponse({"code": 200, "data": {"rows": []}}),
            FakeResponse({"code": 200, "data": {"rows": []}}),
        ]
        runtime = self.client.discover_batch()
        self.assertEqual(runtime["state"], "selection_required")
        self.assertIsNone(runtime["active"])
        self.assertEqual(len([x for x in runtime["choices"] if x["status"] == "valid"]), 2)

    def test_previous_valid_choice_is_restored(self):
        self.cfg.settings["batch_last_id"] = "validBatch002"
        self.cfg.settings["batch_last_name"] = "第二轮"
        self.client.fixed_choices = [choice("validBatch001", "第一轮"), choice("validBatch002", "第二轮")]
        self.client.responses = [
            FakeResponse({"code": 200, "data": {"rows": []}}),
            FakeResponse({"code": 200, "data": {"rows": []}}),
        ]
        runtime = self.client.discover_batch()
        self.assertEqual(runtime["state"], "ready")
        self.assertEqual(runtime["active"]["id"], "validBatch002")
        self.assertEqual(runtime["active"]["name"], "第二轮")

    def test_failed_rediscovery_clears_previous_active_batch(self):
        self.client._apply_batch("oldBatch0001", "manual", "旧批次")
        self.client.fixed_choices = [choice("oldBatch0001", "旧批次")]
        self.client.responses = [
            FakeResponse(ValueError(), text="<html>选课系统尚未开放</html>"),
        ]
        runtime = self.client.discover_batch()
        self.assertEqual(runtime["state"], "not_open")
        self.assertIsNone(runtime["active"])
        self.assertEqual(self.client.batch_id, "")

    def test_required_previous_batch_failure_does_not_select_another(self):
        self.client._apply_batch("oldBatch0001", "manual", "旧批次")
        self.client.fixed_choices = [choice("newBatch0002", "新批次")]
        self.client.responses = [
            FakeResponse({"code": 200, "data": {"rows": []}}),
        ]
        runtime = self.client.discover_batch("oldBatch0001", require_preferred=True)
        self.assertEqual(runtime["state"], "unavailable")
        self.assertIsNone(runtime["active"])
        self.assertEqual(self.client.batch_id, "")

    def test_confirmation_required_is_not_validated(self):
        self.client.fixed_choices = [choice("confirmBatch01", "确认批次", need_confirm=True,
                                            is_confirmed=False, status="confirmation_required")]
        runtime = self.client.discover_batch()
        self.assertEqual(runtime["choices"][0]["status"], "confirmation_required")
        self.assertEqual(self.client.responses, [])

    def test_default_batch_recovers_authoritative_list(self):
        # 未存任何批次时，发现流程应尝试默认批次去读 studentInfo 权威列表，
        # 从而拿到真实名称与 can_select=1，而不是被空 Batchid 退回首页。
        self.client.responses = [
            FakeResponse(student_info_payload(DEFAULT_BATCH_ID, "补退选含重修")),
        ]
        choices = self.client._collect_choices()
        self.assertEqual(choices[0]["id"], DEFAULT_BATCH_ID)
        self.assertEqual(choices[0]["name"], "补退选含重修")
        self.assertIs(choices[0]["can_select"], True)
        self.assertEqual(self.client.responses, [])  # 一次权威请求命中即停止

    def test_default_batch_open_round_is_discovered_and_adopted(self):
        # 端到端：默认批次开放时，discover 应验证并采用它（写 batch_last）。
        self.client.responses = [
            FakeResponse(student_info_payload(DEFAULT_BATCH_ID, "补退选含重修")),
            FakeResponse({"code": 200, "data": {"rows": []}}),   # validate DEFAULT
            not_open_html(),                                     # validate legacy 关闭
        ]
        runtime = self.client.discover_batch()
        self.assertEqual(runtime["state"], "ready")
        self.assertEqual(runtime["active"]["id"], DEFAULT_BATCH_ID)
        self.assertEqual(runtime["active"]["name"], "补退选含重修")
        self.assertEqual(self.client.cfg.settings.get("batch_last_id"), DEFAULT_BATCH_ID)

    def test_landing_seed_is_probed_when_default_batch_not_open(self):
        # 账号真实开放轮次是研选本、默认补退选对其关闭时，应回退 landing 的
        # var batch 作 seed 读权威列表，而不是把账号误判为“尚未开放”。
        client = LandingStub(self.cfg)
        client.landing_html = '<script>var batch = {"code":"batchInline01",' \
                              '"name":"2026年秋季学期研选本"}</script>'
        client.responses = [
            not_open_html(),  # studentInfo(默认批次) -> 退回
            not_open_html(),  # studentInfo(legacy)  -> 退回
            FakeResponse(student_info_payload("batchInline01", "研选本")),  # landing seed
        ]
        choices = client._collect_choices()
        self.assertEqual(choices[0]["id"], "batchInline01")
        self.assertEqual(choices[0]["name"], "研选本")
        self.assertIs(choices[0]["can_select"], True)


if __name__ == "__main__":
    unittest.main()
