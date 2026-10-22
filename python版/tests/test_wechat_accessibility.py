"""模拟 Win32 读写与互斥量，验证会话恢复；不打开真实进程或客户端。"""

import ctypes
from pathlib import Path
import struct
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import wechat_accessibility as activation


class FakeWin32:
    """所有地址、句柄和模块字节均在测试对象内模拟。"""

    process_handle = 101
    mutex_handle = 202
    base = 0x180000000

    def __init__(self, value=0):
        self.value = value
        self.alive = True
        self.module_moved = False
        self.mutex_result = 0
        self.memory_protect = 0x04
        self.memory_kind = 0x1000000
        self.fail_readback_once = False
        self.readback_failure_pending = False
        self.write_then_report_failure = False
        self.fail_restore = False
        self.writes = []
        self.dll_path = ROOT / "Weixin.dll"
        self.OpenProcess = Mock(return_value=self.process_handle)
        self.CloseHandle = Mock(return_value=True)
        self.CreateMutexW = Mock(return_value=self.mutex_handle)
        self.ReleaseMutex = Mock(return_value=True)
        self.WaitForSingleObject = Mock(side_effect=self._wait)
        self.QueryFullProcessImageNameW = Mock(side_effect=self._query_exe)
        self.EnumProcessModulesEx = Mock(side_effect=self._enum_modules)
        self.GetModuleFileNameExW = Mock(side_effect=self._module_name)
        self.GetModuleInformation = Mock(side_effect=self._module_info)
        self.ReadProcessMemory = Mock(side_effect=self._read)
        self.WriteProcessMemory = Mock(side_effect=self._write)
        self.VirtualQueryEx = Mock(side_effect=self._query_memory)

    @property
    def current_base(self):
        return self.base + (0x1000 if self.module_moved else 0)

    @property
    def address(self):
        return self.base + activation.BUILD["rva"]

    def _wait(self, handle, milliseconds):
        if handle == self.mutex_handle:
            return self.mutex_result
        if handle == self.process_handle:
            return 0x102 if self.alive else 0
        raise AssertionError("测试收到未知句柄")

    def _query_exe(self, handle, flags, name, size):
        name.value = str(ROOT / "Weixin.exe")
        return True

    def _enum_modules(self, handle, modules, length, needed, flags):
        modules[0] = self.current_base
        needed._obj.value = ctypes.sizeof(activation.wintypes.HMODULE)
        return True

    def _module_name(self, handle, module, name, length):
        name.value = str(self.dll_path)
        return len(name.value)

    def _module_info(self, handle, module, info, length):
        info._obj.base = self.current_base
        info._obj.size = activation.BUILD["image_size"]
        info._obj.entry = self.current_base + 0x1000
        return True

    def _read(self, handle, address, buffer, size, count):
        if address == self.base and size == 0x1000:
            header = bytearray(0x1000)
            header[:2] = b"MZ"
            struct.pack_into("<I", header, 0x3C, 0x80)
            header[0x80:0x84] = b"PE\0\0"
            struct.pack_into("<I", header, 0xD0, activation.BUILD["image_size"])
            value = bytes(header)
        elif address == self.base + activation.BUILD["code_rva"]:
            value = activation.BUILD["code"]
        elif address == self.address and size == 1:
            if self.readback_failure_pending:
                self.readback_failure_pending = False
                count._obj.value = 0
                return False
            value = bytes([self.value])
        else:
            raise AssertionError(f"测试读取了未定义地址：{address:#x}")
        self._copy(buffer, value)
        count._obj.value = len(value)
        return True

    @staticmethod
    def _copy(buffer, value):
        ctypes.memmove(buffer, value, len(value))

    def _write(self, handle, address, buffer, size, count):
        assert address == self.address and size == 1
        value = buffer._obj.value
        self.writes.append(value)
        if value == 0 and self.fail_restore:
            count._obj.value = 0
            return False
        self.value = value
        count._obj.value = 1
        if value == 1 and self.fail_readback_once:
            self.fail_readback_once = False
            self.readback_failure_pending = True
        if value == 1 and self.write_then_report_failure:
            return False
        return True

    def _query_memory(self, handle, address, info, length):
        memory = info._obj
        memory.base = address & ~0xFFF
        memory.allocation_base = self.base
        memory.size = 0x1000
        memory.state = 0x1000
        memory.protect = self.memory_protect
        memory.kind = self.memory_kind
        return ctypes.sizeof(memory)


class AccessibilitySessionTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWin32()
        self.logs = []
        self.patchers = [
            patch.object(activation.sys, "platform", "win32"),
            patch.object(activation.ctypes, "WinDLL", return_value=self.api, create=True),
            patch.object(activation.ctypes, "get_last_error", return_value=5, create=True),
            patch.object(activation.ctypes, "WinError", side_effect=lambda code: OSError(code, "模拟 Win32 失败"), create=True),
            patch.object(Path, "read_bytes", return_value=b"mock-verified-dll"),
            patch.object(activation, "locate_accessibility_gate", return_value=SimpleNamespace(
                sha256=activation.BUILD["sha256"], target_rva=activation.BUILD["rva"],
                image_size=activation.BUILD["image_size"],
            )),
        ]
        for patcher in self.patchers:
            patcher.start()
        activation._ACTIVE_PIDS.clear()

    def tearDown(self):
        activation._ACTIVE_PIDS.clear()
        for patcher in reversed(self.patchers):
            patcher.stop()

    def session(self, **kwargs):
        return activation.WechatAccessibilitySession(4321, log=self.logs.append, **kwargs)

    def assert_released(self, session):
        self.assertIsNone(session.handle)
        self.assertIsNone(session.mutex)
        self.assertFalse(session.changed)
        self.assertEqual(session._state, "closed")
        self.assertNotIn(4321, activation._ACTIVE_PIDS)
        self.api.ReleaseMutex.assert_called_once_with(self.api.mutex_handle)
        closed = [call.args[0] for call in self.api.CloseHandle.call_args_list]
        self.assertEqual(closed.count(self.api.process_handle), 1)
        self.assertEqual(closed.count(self.api.mutex_handle), 1)

    def test_enables_zero_and_restores_with_pid_named_mutex(self):
        session = self.session(enable=True)
        with session:
            self.assertEqual(self.api.value, 1)
            self.assertTrue(session.changed)
            self.assertEqual(session.previous, 0)
        self.assertEqual(self.api.writes, [1, 0])
        self.assertEqual(self.api.value, 0)
        self.api.CreateMutexW.assert_called_once_with(None, False, "Global\\WechatYtoForwarder.Accessibility.v1.4321")
        self.assert_released(session)

    def test_original_one_is_neither_written_nor_restored(self):
        self.api.value = 1
        session = self.session(enable=True)
        with session:
            self.assertFalse(session.changed)
            self.assertEqual(session.previous, 1)
        self.assertEqual(self.api.writes, [])
        self.assertEqual(self.api.value, 1)
        self.assert_released(session)

    def test_readonly_probe_has_no_write_rights_and_does_not_modify(self):
        with self.session(enable=False):
            self.assertEqual(self.api.value, 0)
        access = self.api.OpenProcess.call_args.args[0]
        self.assertEqual(access & (0x20 | 0x08), 0)
        self.assertEqual(self.api.writes, [])

    def test_failed_readback_after_successful_write_still_restores(self):
        self.api.fail_readback_once = True
        session = self.session(enable=True)
        with self.assertRaises(OSError):
            session.__enter__()
        self.assertEqual(self.api.writes, [1, 0])
        self.assertEqual(self.api.value, 0)
        self.assert_released(session)

    def test_failed_write_api_after_actual_write_still_restores(self):
        self.api.write_then_report_failure = True
        session = self.session(enable=True)
        with self.assertRaises(OSError):
            session.__enter__()
        self.assertEqual(self.api.writes, [1, 0])
        self.assertEqual(self.api.value, 0)
        self.assert_released(session)

    def test_process_exit_does_not_write_to_reused_pid(self):
        session = self.session(enable=True)
        with session:
            self.api.alive = False
        self.assertEqual(self.api.writes, [1])
        self.api.OpenProcess.assert_called_once()
        self.assertTrue(any("进程已退出" in line for line in self.logs))
        self.assert_released(session)

    def test_module_change_refuses_restore_and_preserves_original_exception(self):
        session = self.session(enable=True)
        original = RuntimeError("原操作失败")
        with self.assertRaises(activation.AccessibilityActivationError) as raised:
            with session:
                self.api.module_moved = True
                raise original
        self.assertEqual(self.api.writes, [1])
        self.assertIs(raised.exception.operation_error, original)
        self.assertIn("模块发生变化", str(raised.exception.restore_error))
        self.assertIs(raised.exception.__cause__, raised.exception.restore_error)
        self.assert_released(session)

    def test_restore_failure_contains_both_failures(self):
        session = self.session(enable=True)
        original = ValueError("业务层异常")
        with self.assertRaises(activation.AccessibilityActivationError) as raised:
            with session:
                self.api.fail_restore = True
                raise original
        self.assertIs(raised.exception.operation_error, original)
        self.assertIsInstance(raised.exception.restore_error, OSError)
        self.assertIn("业务层异常", str(raised.exception))
        self.assertIn("恢复失败", str(raised.exception))
        self.assertEqual(self.api.value, 1)
        self.assert_released(session)

    def test_enter_readback_failure_and_restore_failure_both_survive(self):
        self.api.fail_readback_once = True
        self.api.fail_restore = True
        session = self.session(enable=True)
        with self.assertRaises(activation.AccessibilityActivationError) as raised:
            session.__enter__()
        self.assertIsInstance(raised.exception.operation_error, OSError)
        self.assertIsInstance(raised.exception.restore_error, OSError)
        self.assertEqual(self.api.writes, [1, 0])
        self.assert_released(session)

    def test_non_boolean_value_refuses_write(self):
        self.api.value = 2
        with self.assertRaisesRegex(activation.AccessibilityActivationError, "不是0或1"):
            with self.session(enable=True):
                self.fail("不应进入正文")
        self.assertEqual(self.api.writes, [])

    def test_external_change_is_not_overwritten_on_exit(self):
        with self.assertRaisesRegex(activation.AccessibilityActivationError, "其它来源"):
            with self.session(enable=True):
                self.api.value = 2
        self.assertEqual(self.api.writes, [1])
        self.assertEqual(self.api.value, 2)

    def test_already_restored_byte_is_not_written_again(self):
        with self.session(enable=True):
            self.api.value = 0
        self.assertEqual(self.api.writes, [1])

    def test_same_object_cannot_enter_twice_or_reenter_after_exit(self):
        session = self.session(enable=True)
        with session:
            with self.assertRaisesRegex(activation.AccessibilityActivationError, "只能进入一次"):
                session.__enter__()
            self.assertEqual(self.api.value, 1)
        with self.assertRaisesRegex(activation.AccessibilityActivationError, "只能进入一次"):
            session.__enter__()
        self.assertEqual(self.api.writes, [1, 0])
        self.api.OpenProcess.assert_called_once()

    def test_exit_is_idempotent_before_enter_and_after_close(self):
        session = self.session(enable=True)
        self.assertFalse(session.__exit__(None, None, None))
        with session:
            pass
        self.assertFalse(session.__exit__(None, None, None))
        self.assertEqual(self.api.writes, [1, 0])
        self.assert_released(session)

    def test_same_thread_second_session_cannot_recursively_acquire_mutex(self):
        first, second = self.session(enable=True), self.session(enable=True)
        with first:
            with self.assertRaisesRegex(activation.AccessibilityActivationError, "已有无障碍会话"):
                second.__enter__()
            self.assertEqual(self.api.value, 1)
            self.assertIn(4321, activation._ACTIVE_PIDS)
        self.api.CreateMutexW.assert_called_once()
        self.api.OpenProcess.assert_called_once()
        self.assertEqual(self.api.writes, [1, 0])

    def test_other_process_mutex_owner_rejected_before_opening_process(self):
        self.api.mutex_result = 0x102
        session = self.session(enable=True)
        with self.assertRaisesRegex(activation.AccessibilityActivationError, "另一进程"):
            session.__enter__()
        self.api.OpenProcess.assert_not_called()
        self.api.ReleaseMutex.assert_not_called()
        self.api.CloseHandle.assert_called_once_with(self.api.mutex_handle)
        self.assertNotIn(4321, activation._ACTIVE_PIDS)

    def test_abandoned_mutex_revalidates_and_does_not_claim_original_one(self):
        self.api.mutex_result = 0x80
        self.api.value = 1
        session = self.session(enable=True)
        with session:
            self.assertEqual(session.previous, 1)
        self.assertEqual(self.api.writes, [])
        self.assertTrue(any("上一持有者异常结束" in line for line in self.logs))
        self.assert_released(session)

    def test_mutex_creation_failure_releases_local_pid_registration(self):
        self.api.CreateMutexW.return_value = None
        with self.assertRaises(OSError):
            self.session(enable=True).__enter__()
        self.assertNotIn(4321, activation._ACTIVE_PIDS)
        self.api.OpenProcess.assert_not_called()
        self.api.CloseHandle.assert_not_called()

    def test_open_failure_releases_owned_mutex(self):
        self.api.OpenProcess.return_value = None
        with self.assertRaises(OSError):
            self.session(enable=True).__enter__()
        self.api.ReleaseMutex.assert_called_once_with(self.api.mutex_handle)
        self.api.CloseHandle.assert_called_once_with(self.api.mutex_handle)
        self.assertNotIn(4321, activation._ACTIVE_PIDS)

    def test_failed_or_unknown_mutex_wait_releases_handle_without_claiming_owner(self):
        for result in (0xFFFFFFFF, 7):
            self.api.mutex_result = result
            session = self.session(enable=True)
            with self.subTest(result=result), self.assertRaises((OSError, activation.AccessibilityActivationError)):
                session.__enter__()
            self.assertIsNone(session.mutex)
            self.assertNotIn(4321, activation._ACTIVE_PIDS)
        self.api.OpenProcess.assert_not_called()
        self.api.ReleaseMutex.assert_not_called()
        self.assertEqual(self.api.CloseHandle.call_count, 2)

    def test_process_handle_close_failure_does_not_skip_mutex_release(self):
        self.api.CloseHandle.side_effect = lambda handle: handle != self.api.process_handle
        session = self.session(enable=True)
        with self.assertRaisesRegex(activation.AccessibilityActivationError, "资源释放失败") as raised:
            with session:
                pass
        self.assertEqual(self.api.value, 0)
        self.assertEqual(len(raised.exception.cleanup_errors), 1)
        self.api.ReleaseMutex.assert_called_once_with(self.api.mutex_handle)
        self.assertEqual(self.api.CloseHandle.call_count, 2)
        self.assertNotIn(4321, activation._ACTIVE_PIDS)

    def test_cross_thread_exit_is_rejected_without_releasing_owner_resources(self):
        session = self.session(enable=True)
        failures = []
        with session:
            def exit_elsewhere():
                try:
                    session.__exit__(None, None, None)
                except Exception as error:
                    failures.append(error)
            worker = threading.Thread(target=exit_elsewhere)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIn("原线程", str(failures[0]))
            self.assertEqual(self.api.value, 1)
            self.assertEqual(session._state, "active")
            self.api.ReleaseMutex.assert_not_called()
        self.assert_released(session)

    def test_release_failure_still_closes_handles_and_blocks_new_local_session(self):
        self.api.ReleaseMutex.return_value = False
        session = self.session(enable=True)
        with self.assertRaisesRegex(activation.AccessibilityActivationError, "资源释放失败") as raised:
            with session:
                pass
        self.assertEqual(self.api.value, 0)
        self.assertEqual(len(raised.exception.cleanup_errors), 1)
        self.assertIn(4321, activation._ACTIVE_PIDS)
        self.assertEqual(self.api.CloseHandle.call_count, 2)
        with self.assertRaisesRegex(activation.AccessibilityActivationError, "已有无障碍会话"):
            self.session(enable=True).__enter__()

    def test_invalid_memory_protection_or_type_refuses_write(self):
        for protection, kind in ((0x40, 0x1000000), (0x104, 0x1000000), (0x04, 0x20000)):
            self.api.memory_protect, self.api.memory_kind = protection, kind
            with self.subTest(protect=protection, kind=kind), self.assertRaisesRegex(
                activation.AccessibilityActivationError, "非执行可写映像区"
            ):
                self.session(enable=True).__enter__()
        self.assertEqual(self.api.writes, [])


if __name__ == "__main__":
    unittest.main()
