"""特定微信构建的临时 UIA 激活试验；仅改内存中的已核对状态字节。

不修改磁盘、不注入代码、不扫描聊天数据。构建哈希和指令地址均固定核对，
未知版本拒绝执行。持有原进程句柄，正常退出及可控异常时恢复本次改动。
"""

import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import threading
import time

from accessibility_gate import locate_accessibility_gate


# 只允许经过本机反汇编核对的构建。不能将扫描结果自动加入此表。
BUILD = {
    "sha256": "e0428a7b691853de496532523c2c09aa095f90941c37c9f4cfb3f137f3c66556",
    "rva": 0x0B39C9D8,
    "code_rva": 0x864AF2,
    "code": bytes.fromhex("4885c90f84f7060000803dd67eb30a000f84ea060000"),
    "image_size": 204795904,
}

_ACTIVE_PIDS = set()
_ACTIVE_PIDS_LOCK = threading.Lock()


class AccessibilityActivationError(RuntimeError):
    pass


class ModuleInfo(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("size", wintypes.DWORD), ("entry", ctypes.c_void_p)]


class MemoryInfo(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("allocation_base", ctypes.c_void_p),
                ("allocation_protect", wintypes.DWORD), ("partition", wintypes.WORD),
                ("size", ctypes.c_size_t), ("state", wintypes.DWORD),
                ("protect", wintypes.DWORD), ("kind", wintypes.DWORD)]


def _declare(library, name, result, arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


class WechatAccessibilitySession:
    """仅支持64位Python；必须与实际使用控件的会话拥有相同生命周期。"""

    def __init__(self, pid, enable=False, log=print):
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise AccessibilityActivationError("需要64位 Windows 和64位 Python")
        self.pid = int(pid)
        self.enable = enable
        self.log = log
        self.handle = None
        self.changed = False
        self.previous = None
        self.module = None
        self.path = None
        self.mutex = None
        self._mutex_owned = False
        self._pid_registered = False
        self._owner_thread = None
        self._state = "new"
        self._lifecycle_lock = threading.Lock()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        pointer = ctypes.c_void_p
        size_pointer = ctypes.POINTER(ctypes.c_size_t)
        self.open = _declare(kernel, "OpenProcess", wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
        self.close = _declare(kernel, "CloseHandle", wintypes.BOOL, [wintypes.HANDLE])
        self.wait = _declare(kernel, "WaitForSingleObject", wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD])
        self.create_mutex = _declare(kernel, "CreateMutexW", wintypes.HANDLE,
                                     [pointer, wintypes.BOOL, wintypes.LPCWSTR])
        self.release_mutex = _declare(kernel, "ReleaseMutex", wintypes.BOOL, [wintypes.HANDLE])
        self.query_exe = _declare(kernel, "QueryFullProcessImageNameW", wintypes.BOOL,
                                  [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)])
        self.read_memory = _declare(kernel, "ReadProcessMemory", wintypes.BOOL,
                                    [wintypes.HANDLE, pointer, pointer, ctypes.c_size_t, size_pointer])
        self.write_memory = _declare(kernel, "WriteProcessMemory", wintypes.BOOL,
                                     [wintypes.HANDLE, pointer, pointer, ctypes.c_size_t, size_pointer])
        self.query_memory = _declare(kernel, "VirtualQueryEx", ctypes.c_size_t,
                                     [wintypes.HANDLE, pointer, ctypes.POINTER(MemoryInfo), ctypes.c_size_t])
        self.enum_modules = _declare(psapi, "EnumProcessModulesEx", wintypes.BOOL,
                                     [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), wintypes.DWORD])
        self.module_name = _declare(psapi, "GetModuleFileNameExW", wintypes.DWORD,
                                    [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD])
        self.module_info = _declare(psapi, "GetModuleInformation", wintypes.BOOL,
                                    [wintypes.HANDLE, wintypes.HMODULE, ctypes.POINTER(ModuleInfo), wintypes.DWORD])

    def _acquire_mutex(self):
        # Windows Mutex 允许同一线程递归获取，进程内登记另行禁止嵌套会话。
        with _ACTIVE_PIDS_LOCK:
            if self.pid in _ACTIVE_PIDS:
                raise AccessibilityActivationError("该微信进程已有无障碍会话，请先停止原会话")
            _ACTIVE_PIDS.add(self.pid)
            self._pid_registered = True
        self.mutex = self.create_mutex(None, False, f"Global\\WechatYtoForwarder.Accessibility.v1.{self.pid}")
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        result = self.wait(self.mutex, 0)
        if result in (0, 0x80):
            self._mutex_owned = True
            if result == 0x80:
                self.log("[兼容] 上一持有者异常结束；重新核验当前状态，原值为1时不会认领或恢复")
        elif result == 0x102:
            raise AccessibilityActivationError("另一进程正在使用该微信的无障碍会话，请先停止原会话")
        elif result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        else:
            raise AccessibilityActivationError(f"互斥量等待返回异常状态：0x{result:X}")

    def _release_resources(self):
        """逐项释放资源；某一项失败也要继续，返回所有清理错误。"""
        errors = []
        if self.handle:
            try:
                if not self.close(self.handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            except BaseException as error:
                errors.append(error)
            finally:
                self.handle = None
        if self.mutex:
            try:
                if self._mutex_owned:
                    if not self.release_mutex(self.mutex):
                        raise ctypes.WinError(ctypes.get_last_error())
                    self._mutex_owned = False
            except BaseException as error:
                errors.append(error)
            finally:
                try:
                    if not self.close(self.mutex):
                        raise ctypes.WinError(ctypes.get_last_error())
                except BaseException as error:
                    errors.append(error)
                self.mutex = None
        # 若 ReleaseMutex 失败，保留本进程登记以阻止继续开启相同 PID。
        if self._pid_registered and not self._mutex_owned:
            with _ACTIVE_PIDS_LOCK:
                _ACTIVE_PIDS.discard(self.pid)
            self._pid_registered = False
        return errors

    def _read(self, address, size):
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.read_memory(self.handle, address, buffer, size, ctypes.byref(count)) or count.value != size:
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw

    def _find_module(self):
        modules = (wintypes.HMODULE * 2048)()
        needed = wintypes.DWORD()
        if not self.enum_modules(self.handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 3):
            raise ctypes.WinError(ctypes.get_last_error())
        if needed.value > ctypes.sizeof(modules):
            raise AccessibilityActivationError("进程模块数量超出检查上限")
        matches = []
        for module in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
            name = ctypes.create_unicode_buffer(32768)
            if not self.module_name(self.handle, module, name, len(name)):
                continue
            if Path(name.value).name.lower() == "weixin.dll":
                info = ModuleInfo()
                if not self.module_info(self.handle, module, ctypes.byref(info), ctypes.sizeof(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                matches.append((Path(name.value).resolve(), info))
        if len(matches) != 1:
            raise AccessibilityActivationError("当前进程没有唯一的 Weixin.dll 模块")
        return matches[0]

    def _verify_memory(self):
        if self.wait(self.handle, 0) != 0x102:
            raise AccessibilityActivationError("微信进程已经退出或句柄失效")
        path, module = self._find_module()
        if path != self.path or module.base != self.module.base or module.size != BUILD["image_size"]:
            raise AccessibilityActivationError("微信模块发生变化，拒绝访问原地址")
        # 映像头及完整条件分支都必须与审核构建一致。
        header = self._read(module.base, 0x1000)
        pe = int.from_bytes(header[0x3C:0x40], "little")
        if header[:2] != b"MZ" or pe + 84 > len(header) or header[pe:pe + 4] != b"PE\0\0":
            raise AccessibilityActivationError("内存 PE 头不匹配")
        if int.from_bytes(header[pe + 80:pe + 84], "little") != BUILD["image_size"]:
            raise AccessibilityActivationError("内存映像大小不匹配")
        if self._read(module.base + BUILD["code_rva"], len(BUILD["code"])) != BUILD["code"]:
            raise AccessibilityActivationError("内存中的门控指令与已审核代码不同")
        address = module.base + BUILD["rva"]
        memory = MemoryInfo()
        if self.query_memory(self.handle, address, ctypes.byref(memory), ctypes.sizeof(memory)) != ctypes.sizeof(memory):
            raise ctypes.WinError(ctypes.get_last_error())
        if (memory.allocation_base != module.base or memory.state != 0x1000 or memory.kind != 0x1000000
                or memory.protect not in (0x04, 0x08) or not memory.base <= address < memory.base + memory.size):
            raise AccessibilityActivationError("候选地址不在本模块已提交的非执行可写映像区")
        return address

    def _write_byte(self, address, value):
        buffer = ctypes.c_ubyte(value)
        count = ctypes.c_size_t()
        if not self.write_memory(self.handle, address, ctypes.byref(buffer), 1, ctypes.byref(count)) or count.value != 1:
            raise ctypes.WinError(ctypes.get_last_error())
        if self._read(address, 1) != bytes([value]):
            raise AccessibilityActivationError("状态字节写入后回读不一致")

    def __enter__(self):
        with self._lifecycle_lock:
            if self._state != "new":
                raise AccessibilityActivationError("无障碍上下文只能进入一次，请创建新的会话对象")
            if not 0 < self.pid <= 0xFFFFFFFF:
                raise AccessibilityActivationError("微信进程 PID 必须为有效的正整数")
            self._state = "entering"
            self._owner_thread = threading.get_ident()
        try:
            self._acquire_mutex()
            access = 0x100000 | 0x400 | 0x10
            if self.enable:
                access |= 0x20 | 0x08
            self.handle = self.open(access, False, self.pid)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            name = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(name))
            if not self.query_exe(self.handle, 0, name, ctypes.byref(size)) or Path(name.value).name.lower() != "weixin.exe":
                raise AccessibilityActivationError("指定进程不是 Weixin.exe")
            self.path, self.module = self._find_module()
            candidate = locate_accessibility_gate(self.path.read_bytes())
            if (candidate.sha256 != BUILD["sha256"] or candidate.target_rva != BUILD["rva"]
                    or candidate.image_size != BUILD["image_size"]):
                raise AccessibilityActivationError("微信构建未经过本项目验证，拒绝套用其它版本地址")
            address = self._verify_memory()
            self.previous = self._read(address, 1)[0]
            if self.previous not in (0, 1):
                raise AccessibilityActivationError("候选运行值不是0或1，拒绝修改")
            if self.enable and self.previous == 0:
                # 提前认领可能的写入，以便WriteProcessMemory成功但回读异常时仍尝试恢复。
                self.changed = True
                self._write_byte(address, 1)
                self.log("[兼容] 已临时开启当前微信的无障碍状态，退出本次会话时恢复")
            self._state = "active"
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type, exc, traceback):
        with self._lifecycle_lock:
            if self._state in ("new", "closed"):
                return False
            if self._owner_thread != threading.get_ident():
                raise AccessibilityActivationError("必须在进入会话的原线程退出，不能跨线程释放无障碍互斥量")
            if self._state == "closing":
                raise AccessibilityActivationError("无障碍上下文正在退出，不能重复触发恢复")
            self._state = "closing"
        restore_error = None
        try:
            if self.changed:
                if self.wait(self.handle, 0) == 0:
                    self.log("[兼容] 微信进程已退出，原内存随进程释放，无需恢复")
                else:
                    address = self._verify_memory()
                    current = self._read(address, 1)[0]
                    if current == 1:
                        self._write_byte(address, self.previous)
                    elif current != self.previous:
                        raise AccessibilityActivationError("状态已由其它来源改动，拒绝覆盖")
                    self.log("[兼容] 本次无障碍状态改动已恢复")
        except BaseException as error:
            restore_error = error
        finally:
            self.changed = False
            cleanup_errors = self._release_resources()
            self._state = "closed"
        if restore_error or cleanup_errors:
            details = f"恢复失败：{restore_error}" if restore_error else "会话资源释放失败"
            if cleanup_errors:
                details += "；清理错误：" + "；".join(str(error) for error in cleanup_errors)
            failure = AccessibilityActivationError(
                f"{details}；原操作异常：{exc!r}。请退出并重启微信和转发工具后重试"
            )
            failure.operation_error = exc
            failure.restore_error = restore_error
            failure.cleanup_errors = tuple(cleanup_errors)
            raise failure from (restore_error or cleanup_errors[0])
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="指定构建微信的临时无障碍兼容试验，默认只读检查")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--probe-seconds", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.probe_seconds <= 300:
        parser.error("试验时长只能为0～300秒")
    with WechatAccessibilitySession(args.pid, enable=args.probe_seconds > 0, log=lambda s: print(s, flush=True)) as session:
        print(json.dumps({"pid": session.pid, "dll": str(session.path), "previous": session.previous,
                          "changed": session.changed, "rva": hex(BUILD["rva"])}, ensure_ascii=False), flush=True)
        if args.probe_seconds:
            time.sleep(args.probe_seconds)
