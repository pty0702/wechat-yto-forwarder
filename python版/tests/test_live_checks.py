"""离线验证联调检查的单次发送、取消、恢复与受控报告目录。"""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))

import live_checks
from wechat_forwarder import DEFAULT_CONFIG, SendUncertainError


class FakeAdapters:
    """上下文已代表 prepare；不依赖 UIA 或真实应用。"""

    def __init__(self):
        self.wxsource = Mock(spec=["prepare", "fetch_new"])
        self.wxsink = Mock(spec=["prepare", "send", "send_mention"])
        self.kfsource = Mock(spec=["prepare", "fetch_new"])
        self.kfsink = Mock(spec=["prepare", "send", "send_mention"])
        self.entered = 0
        self.exited = 0
        self.after_prepare = None
        self.prepare_error = None
        self.restore_error = None
        self.log = lambda message: None

    def factory(self, cfg, log):
        self.log = log
        return self

    def __enter__(self):
        self.entered += 1
        self.log("模拟准备适配器")
        if self.prepare_error:
            raise self.prepare_error
        if self.after_prepare:
            self.after_prepare()
        return self.wxsource, self.wxsink, self.kfsource, self.kfsink

    def __exit__(self, exc_type, exc, traceback):
        self.exited += 1
        self.log("模拟恢复适配器")
        if self.restore_error:
            raise self.restore_error
        return False


class LiveCheckTests(unittest.TestCase):
    def setUp(self):
        self.workdir = PROJECT_ROOT / "diagnostics" / (
            "test-live-checks-" + uuid.uuid4().hex
        )
        self.workdir.mkdir(parents=True)
        self.cfg = deepcopy(DEFAULT_CONFIG)
        self.cfg.update(
            mode="live",
            wx_group="联调微信群",
            kf_group="联调客户管家群",
            service_staff="客服小王",
        )
        self.stop = threading.Event()
        self.messages = []
        self.adapters = FakeAdapters()
        self.factory = Mock(side_effect=self.adapters.factory)
        self.factory_patch = patch.object(
            live_checks, "create_live_adapters", self.factory
        )
        self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)

    def tearDown(self):
        # 只删除本测试明确创建的项目内目录，不枚举或删除外部路径。
        target = self.workdir.resolve()
        self.assertEqual(target.parent, (PROJECT_ROOT / "diagnostics").resolve())
        self.assertTrue(target.name.startswith("test-live-checks-"))
        if target.exists():
            shutil.rmtree(target)

    def run_check(self, *, side=None, mention=False, expected_error=None):
        if expected_error is None:
            path = live_checks.run_live_check(
                self.cfg, self.workdir, self.stop, self.messages.append,
                side=side, mention=mention,
            )
        else:
            with self.assertRaises(type(expected_error)) as raised:
                live_checks.run_live_check(
                    self.cfg, self.workdir, self.stop, self.messages.append,
                    side=side, mention=mention,
                )
            self.assertIs(raised.exception, expected_error)
            reports = list(self.workdir.glob("*.json"))
            self.assertEqual(len(reports), 1)
            path = reports[0]
        self.assertIsInstance(path, Path)
        self.assertTrue(path.is_file())
        self.assertTrue(path.resolve().is_relative_to(self.workdir.resolve()))
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        datetime.fromisoformat(report["time"].replace("Z", "+00:00"))
        self.assertEqual(report["side"], side)
        self.assertIs(report["mention"], mention)
        self.assertEqual(report["kind"], "preflight" if side is None else "test_send")
        self.assertEqual(
            report["group"],
            {"wx": self.cfg["wx_group"], "kf": self.cfg["kf_group"]}
            if side is None else self.cfg[side + "_group"],
        )
        self.assertIsInstance(report["logs"], list)
        self.assertTrue(all(isinstance(item, str) for item in report["logs"]))
        self.assertIsInstance(report["errors"], list)
        self.assertIs(type(report["send_started"]), bool)
        self.assertIs(type(report["send_confirmed"]), bool)
        for error in report["errors"]:
            self.assertTrue({"type", "message", "phase"}.issubset(error))
            self.assertTrue(all(isinstance(error[key], str) for key in (
                "type", "message", "phase"
            )))
        self.assertTrue(any(str(path) in str(line) for line in self.messages))
        return report

    def assert_no_send(self):
        for sink in (self.adapters.wxsink, self.adapters.kfsink):
            sink.send.assert_not_called()
            sink.send_mention.assert_not_called()

    def test_preflight_only_enters_prepared_context(self):
        report = self.run_check()
        self.assertEqual((report["status"], report["phase"]), ("success", "complete"))
        self.assertEqual(report["errors"], [])
        self.assertEqual((report["send_started"], report["send_confirmed"]), (False, False))
        self.assertEqual((self.adapters.entered, self.adapters.exited), (1, 1))
        self.factory.assert_called_once()
        self.assert_no_send()
        for item in (self.adapters.wxsource, self.adapters.wxsink,
                     self.adapters.kfsource, self.adapters.kfsink):
            item.prepare.assert_not_called()
        self.assertIn("模拟准备适配器", self.messages)
        self.assertIn("模拟恢复适配器", self.messages)
        self.assertNotIn("模拟准备适配器", report["logs"])
        self.assertNotIn("模拟恢复适配器", report["logs"])

    def test_arbitrary_adapter_log_body_is_displayed_but_not_saved(self):
        body = "私聊正文：模拟客户地址与运单号，不应进入报告"
        self.adapters.after_prepare = lambda: self.adapters.log(body)
        report = self.run_check()
        self.assertIn(body, self.messages)
        self.assertNotIn(body, json.dumps(report, ensure_ascii=False))
        self.assertIn("收到适配器状态日志，原文未写入联调记录", report["logs"])

    def test_wechat_sends_exact_message_once(self):
        report = self.run_check(side="wx")
        self.assertEqual(live_checks.TEST_MESSAGE, "转发工具联调测试，请忽略。")
        self.adapters.wxsink.send.assert_called_once_with(live_checks.TEST_MESSAGE)
        self.adapters.wxsink.send_mention.assert_not_called()
        self.adapters.kfsink.send.assert_not_called()
        self.assertEqual((report["status"], report["phase"]), ("success", "complete"))
        self.assertEqual((report["send_started"], report["send_confirmed"]), (True, True))

    def test_customer_manager_sends_exact_message_once(self):
        report = self.run_check(side="kf")
        self.adapters.kfsink.send.assert_called_once_with(live_checks.TEST_MESSAGE)
        self.adapters.kfsink.send_mention.assert_not_called()
        self.adapters.wxsink.send.assert_not_called()
        self.assertEqual(report["status"], "success")

    def test_wechat_mention_sends_once_without_plain_send(self):
        report = self.run_check(side="wx", mention=True)
        self.adapters.wxsink.send_mention.assert_called_once_with(
            "客服小王", live_checks.TEST_MESSAGE
        )
        self.adapters.wxsink.send.assert_not_called()
        self.adapters.kfsink.send.assert_not_called()
        self.assertEqual(report["status"], "success")

    def test_stop_before_prepare_writes_cancelled_without_adapter_creation(self):
        self.stop.set()
        report = self.run_check(side="wx")
        self.assertEqual((report["status"], report["phase"]), ("cancelled", "cancelled"))
        self.factory.assert_not_called()
        self.assert_no_send()

    def test_stop_after_prepare_restores_without_send(self):
        self.adapters.after_prepare = self.stop.set
        report = self.run_check(side="wx")
        self.assertEqual((report["status"], report["phase"]), ("cancelled", "cancelled"))
        self.assertEqual((self.adapters.entered, self.adapters.exited), (1, 1))
        self.assert_no_send()

    def test_stop_during_send_does_not_replace_success_with_cancelled(self):
        self.adapters.wxsink.send.side_effect = lambda message: self.stop.set()
        report = self.run_check(side="wx")
        self.adapters.wxsink.send.assert_called_once_with(live_checks.TEST_MESSAGE)
        self.assertEqual((report["status"], report["phase"]), ("success", "complete"))
        self.assertEqual(self.adapters.exited, 1)

    def test_uncertain_send_is_recorded_without_retry(self):
        self.adapters.wxsink.send.side_effect = SendUncertainError("发送结果无法确认")
        report = self.run_check(side="wx", expected_error=self.adapters.wxsink.send.side_effect)
        self.assertEqual((report["status"], report["phase"]), ("uncertain", "send"))
        self.assertEqual((report["send_started"], report["send_confirmed"]), (True, False))
        self.assertEqual(report["errors"], [{
            "type": "SendUncertainError", "message": "发送结果无法确认", "phase": "send"
        }])
        self.adapters.wxsink.send.assert_called_once()
        self.assertEqual(self.adapters.exited, 1)

    def test_successful_send_followed_by_restore_failure_is_failed(self):
        self.adapters.restore_error = RuntimeError("模拟恢复失败")
        report = self.run_check(side="kf", expected_error=self.adapters.restore_error)
        self.assertEqual((report["status"], report["phase"]), ("failed", "restore"))
        self.assertEqual((report["send_started"], report["send_confirmed"]), (True, True))
        self.assertEqual(report["errors"], [{
            "type": "RuntimeError", "message": "模拟恢复失败", "phase": "restore"
        }])
        self.adapters.kfsink.send.assert_called_once()

    def test_send_and_restore_failures_are_both_preserved(self):
        self.adapters.wxsink.send.side_effect = SendUncertainError("原发送不确定")
        self.adapters.restore_error = RuntimeError("后续恢复失败")
        report = self.run_check(side="wx", expected_error=self.adapters.restore_error)
        self.assertEqual(report["status"], "uncertain")
        self.assertEqual({(error["type"], error["message"], error["phase"])
                          for error in report["errors"]}, {
            ("SendUncertainError", "原发送不确定", "send"),
            ("RuntimeError", "后续恢复失败", "restore"),
        })
        self.adapters.wxsink.send.assert_called_once()

    def test_prepare_failure_is_recorded_without_send(self):
        self.adapters.prepare_error = RuntimeError("模拟准备失败")
        report = self.run_check(side="wx", expected_error=self.adapters.prepare_error)
        self.assertEqual((report["status"], report["phase"]), ("failed", "prepare"))
        self.assertEqual(report["errors"][0]["message"], "模拟准备失败")
        self.assert_no_send()

    def final_report_failure(self, error):
        original = live_checks._write_report
        snapshots = []

        def write_report(path, record):
            snapshots.append(deepcopy(record))
            if len(snapshots) == 2:
                raise error
            return original(path, record)

        return snapshots, patch.object(live_checks, "_write_report", side_effect=write_report)

    def test_final_report_failure_does_not_replace_original_send_error(self):
        operation_error = RuntimeError("原始发送操作异常")
        self.adapters.wxsink.send.side_effect = operation_error
        snapshots, writer = self.final_report_failure(OSError("模拟报告写入失败"))
        with writer as write_report, self.assertRaises(RuntimeError) as raised:
            live_checks.run_live_check(
                self.cfg, self.workdir, self.stop, self.messages.append, side="wx"
            )
        self.assertIs(raised.exception, operation_error)
        self.assertEqual(write_report.call_count, 2)
        self.assertEqual((snapshots[-1]["send_started"], snapshots[-1]["send_confirmed"]),
                         (True, False))
        self.adapters.wxsink.send.assert_called_once()
        self.assertTrue(any("写入失败" in line for line in self.messages))

    def test_report_failure_after_confirmed_send_is_uncertain_and_not_retried(self):
        snapshots, writer = self.final_report_failure(OSError("模拟磁盘故障"))
        with writer as write_report, self.assertRaises(SendUncertainError) as raised:
            live_checks.run_live_check(
                self.cfg, self.workdir, self.stop, self.messages.append, side="kf"
            )
        self.assertIn("报告", str(raised.exception))
        self.assertIn("勿重试", str(raised.exception))
        self.assertEqual(write_report.call_count, 2)
        self.assertEqual((snapshots[-1]["send_started"], snapshots[-1]["send_confirmed"]),
                         (True, True))
        self.adapters.kfsink.send.assert_called_once()
        self.assertEqual(self.adapters.exited, 1)

    def test_preflight_report_failure_preserves_oserror_without_send(self):
        error = OSError("预检报告无法写入")
        _, writer = self.final_report_failure(error)
        with writer, self.assertRaises(OSError) as raised:
            live_checks.run_live_check(self.cfg, self.workdir, self.stop, self.messages.append)
        self.assertIs(raised.exception, error)
        self.assert_no_send()

    def test_invalid_side_is_rejected_before_adapter_or_report(self):
        for side in ("both", "", True):
            with self.subTest(side=side), self.assertRaises((ValueError, TypeError)):
                live_checks.run_live_check(self.cfg, self.workdir, self.stop, side=side)
        self.factory.assert_not_called()
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_invalid_mention_combinations_are_rejected_early(self):
        for side, mention, staff in ((None, True, "客服"), ("kf", True, "客服"),
                                     ("wx", True, ""), ("wx", True, "   "),
                                     ("wx", 1, "客服"), ("wx", "false", "客服")):
            with self.subTest(side=side, mention=mention, staff=staff):
                cfg = deepcopy(self.cfg)
                cfg["service_staff"] = staff
                with self.assertRaises((ValueError, TypeError)):
                    live_checks.run_live_check(
                        cfg, self.workdir, self.stop, side=side, mention=mention
                    )
        self.factory.assert_not_called()
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_output_outside_project_is_rejected_without_creating_it(self):
        output = PROJECT_ROOT.parent / ("outside-live-checks-" + uuid.uuid4().hex) / "diagnostics"
        self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            live_checks.run_live_check(self.cfg, output, self.stop)
        self.factory.assert_not_called()
        self.assertFalse(output.exists())

    def test_nonignored_output_directory_is_rejected_without_creating_it(self):
        output = PROJECT_ROOT / ("diagnostics-not-ignored-" + uuid.uuid4().hex)
        self.assertFalse(output.exists())
        with self.assertRaises(ValueError):
            live_checks.run_live_check(self.cfg, output, self.stop)
        self.factory.assert_not_called()
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
