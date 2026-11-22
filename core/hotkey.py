# -*- coding: utf-8 -*-
"""AutoClick 全局热键：基于 Windows `RegisterHotKey` 的进程级急停热键。

为什么不用 tkinter 的 `bind_all`：那只能在窗口获得焦点时响应。抢购或长时间跑
流程时窗口常被遮挡/最小化，必须**窗口失焦也能停**，因此走系统级热键注册。

实现要点：
- 在独立线程里注册热键并跑自己的消息循环（`GetMessage`），热键消息只投递给
  注册它的线程；
- 回调**不直接操作界面**，调用方应把动作投递到自己的线程安全队列；
- 退出时用 `PostThreadMessage(WM_QUIT)` 结束消息循环并注销热键；
- 非 Windows 平台不加载任何 Win32 API，`GlobalHotkey.start()` 直接返回 False。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

# 消息与修饰符常量
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000  # 按住不重复触发

# 常用虚拟键码
VK_F12 = 0x7B
VK_F11 = 0x7A
VK_F10 = 0x79

if os.name == "nt":  # pragma: no cover - 仅在 Windows 上实际加载
    import ctypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t

    class _MSG(ctypes.Structure):
        """Win32 MSG 结构（POINT 拆成两个 LONG 字段，避免额外嵌套结构）。"""

        _fields_ = [
            ("hWnd", ctypes.c_void_p),
            ("message", ctypes.c_uint),
            ("wParam", WPARAM),
            ("lParam", LPARAM),
            ("time", ctypes.c_uint),
            ("pt_x", ctypes.c_long),
            ("pt_y", ctypes.c_long),
        ]

    _user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                       ctypes.c_uint, ctypes.c_uint]
    _user32.RegisterHotKey.restype = ctypes.c_bool
    _user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.UnregisterHotKey.restype = ctypes.c_bool
    _user32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), ctypes.c_void_p,
                                    ctypes.c_uint, ctypes.c_uint]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.PostThreadMessageW.argtypes = [ctypes.c_uint, ctypes.c_uint,
                                          WPARAM, LPARAM]
    _user32.PostThreadMessageW.restype = ctypes.c_bool
    _kernel32.GetCurrentThreadId.restype = ctypes.c_uint


class GlobalHotkey:
    """在独立线程中注册一个系统级热键，触发时回调指定函数。"""

    HOTKEY_ID = 0xA1  # 本进程内的热键标识，任意固定值即可

    def __init__(self, vk: int, callback: Callable[[], None]) -> None:
        """构造热键对象（不会立即注册，需调用 start）。

        :param vk: 虚拟键码，如 `VK_F12`。
        :param callback: 触发时执行的函数（应尽量轻量、线程安全）。
        """
        self.vk = vk
        self._callback = callback
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self.active = False   # 是否已成功注册并处于监听状态
        self.error = ""       # 注册失败的原因（供界面提示）

    def start(self, timeout: float = 1.0) -> bool:
        """启动监听线程并等待注册结果。

        :param timeout: 等待注册完成的最长秒数。
        :return: 注册成功返回 True；非 Windows 平台或热键被占用返回 False。
        """
        if os.name != "nt":
            self.error = "当前系统不支持全局热键（仅 Windows）"
            return False
        if self._thread is not None:
            return self.active
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline and not self.active and not self.error:
            time.sleep(0.02)
        return self.active

    def _run(self) -> None:
        """监听线程主体：注册热键 → 跑消息循环 → 收到 WM_HOTKEY 时回调。"""
        self._thread_id = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, self.HOTKEY_ID, MOD_NOREPEAT, self.vk):
            self.error = f"热键已被其他程序占用（错误码 {ctypes.get_last_error()}）"
            return
        self.active = True
        msg = _MSG()
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:  # 0 = WM_QUIT，-1 = 出错
                break
            if msg.message == WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                try:
                    self._callback()
                except Exception:
                    pass  # 回调异常不能打断监听循环
        _user32.UnregisterHotKey(None, self.HOTKEY_ID)
        self.active = False

    def stop(self) -> None:
        """注销热键并结束监听线程（程序退出时调用）。

        :return: 无返回值。
        """
        if os.name != "nt" or not self._thread_id:
            return
        _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        self._thread = None
        self._thread_id = 0
        self.active = False
