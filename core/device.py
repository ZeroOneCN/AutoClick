# -*- coding: utf-8 -*-
"""AutoClick 设备层：adb 定位、设备枚举、分辨率查询、持久 shell 通道、
触摸流采集（供坐标拾取与录制复用）、截屏与运行时资源目录定位。

本模块只依赖 Python 标准库，不引用任何 UI 代码，可同时被 GUI 与命令行自测脚本使用。

打包（PyInstaller）要点：
- 内置资源（adb 三件套）位于 EXE 解包目录 `sys._MEIPASS/resources/adb/`；
- 用户数据（flows/templates）位于 EXE 同级目录，保证可写、可备份。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

# 用于与手机端 shell 同步的唯一标记（不会与 input 命令的输出冲突）
MARK = "__AUTOCLICK_TICK__"

# Windows 下隐藏子进程控制台窗口的创建标志（值为 0x08000000；非 Windows 平台退化为 0）。
# PyInstaller 以 --windowed 打包后 GUI 自身没有控制台，但每次调用 adb.exe（枚举设备、
# 截屏、shell、getevent）都会新建一个控制台窗口，表现为黑色 cmd 一闪而过；加上该标志
# 即可让子进程在无窗口模式下运行。所有 subprocess 调用都应带上它。
NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# 常见 Android SDK platform-tools 位置（自动探测用）
_SDK_CANDIDATES = [
    r"D:\CodeDevelop\Env\adb\adb.exe",
    r"D:\CodeDevelop\Env\AndroidSDK\platform-tools\adb.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
    r"C:\platform-tools\adb.exe",
    r"D:\platform-tools\adb.exe",
    r"E:\platform-tools\adb.exe",
]

# 屏幕分辨率缓存：key 为设备序列号，value 为 (宽, 高)
_SCREEN_CACHE: dict[str, tuple[int, int]] = {}


# ---------------------------------------------------------------------------
# 运行时路径定位（源码运行 / PyInstaller 打包均可用）
# ---------------------------------------------------------------------------

def resource_dir() -> str:
    """返回内置只读资源目录（打包后为解包临时目录下的 resources/）。

    :return: 资源目录绝对路径（源码运行时为项目根的 resources/ 目录）。
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "resources")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources")


def app_dir() -> str:
    """返回用户数据根目录（打包后为 EXE 所在目录）。

    :return: 用户数据根目录绝对路径（源码运行时为项目根目录）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_dir(path: str) -> str:
    """确保目录存在（不存在则创建），用于 flows/ 与 templates/ 的运行时初始化。

    :param path: 目标目录路径。
    :return: 传入的目录路径。
    """
    os.makedirs(path, exist_ok=True)
    return path


def bundled_adb() -> Optional[str]:
    """返回打包内置的 adb 路径（若存在）。

    :return: 内置 adb.exe 的绝对路径；未打包或文件不存在时返回 None。
    """
    path = os.path.join(resource_dir(), "adb", "adb.exe")
    return path if os.path.isfile(path) else None


# ---------------------------------------------------------------------------
# adb 定位与设备枚举
# ---------------------------------------------------------------------------

def find_adb(explicit: str | None = None) -> str:
    """定位可用的 adb 可执行文件路径。

    探测顺序：① 用户显式指定 ② 打包内置 ③ ADB_PATH 环境变量
    ④ 常见 Android SDK 路径 ⑤ 系统 PATH。

    :param explicit: 用户显式指定的 adb 路径（优先级最高）。
    :return: adb 可执行文件的绝对路径。
    :raises FileNotFoundError: 所有候选路径均不可用时抛出，并提示用户手动指定。
    """
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise FileNotFoundError(f"指定的 adb 路径不存在: {explicit}")

    candidates = [bundled_adb() or "", os.environ.get("ADB_PATH", "")]
    candidates += _SDK_CANDIDATES
    candidates.append(shutil.which("adb") or "")
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "未找到 adb，请指定 adb 路径，或把 platform-tools 目录加入 PATH / 设置 ADB_PATH 环境变量"
    )


def run_adb(adb: str, args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    """同步执行一次 adb 命令并返回结果（用于设备枚举、分辨率查询等一次性操作）。

    :param adb: adb 可执行文件路径。
    :param args: 传给 adb 的参数列表（不含 adb 自身）。
    :param timeout: 超时秒数，超时抛 subprocess.TimeoutExpired。
    :return: 含 stdout/stderr/returncode 的 CompletedProcess 对象。
    """
    return subprocess.run(
        [adb, *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=NO_WINDOW_FLAGS,
    )


def list_devices(adb: str) -> list[tuple[str, str]]:
    """枚举当前 adb 可见的设备。

    :param adb: adb 可执行文件路径。
    :return: [(序列号, 状态), ...]，状态为 device / unauthorized / offline 等。
    """
    result = run_adb(adb, ["devices"])
    devices: list[tuple[str, str]] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def resolve_serial(adb: str, serial: str | None) -> str:
    """确定要操作的目标设备序列号。

    :param adb: adb 可执行文件路径。
    :param serial: 用户指定的序列号；为 None 时自动选择（仅当恰好一台在线设备）。
    :return: 目标设备序列号。
    :raises RuntimeError: 无设备、设备未授权，或存在多台设备但未指定序列号时抛出。
    """
    devices = list_devices(adb)
    if serial:
        for s, state in devices:
            if s == serial:
                if state != "device":
                    raise RuntimeError(f"设备 {serial} 当前状态为 {state}，请检查 USB 调试授权")
                return s
        raise RuntimeError(f"未找到设备 {serial}，当前可见: {devices or '无'}")
    usable = [s for s, state in devices if state == "device"]
    if not usable:
        detail = f"，当前可见: {devices}" if devices else "（adb 未发现任何设备）"
        raise RuntimeError("没有可用的在线设备" + detail + "；请开启 USB 调试并在手机上同意授权")
    if len(usable) > 1:
        raise RuntimeError(f"检测到多台设备，请指定序列号: {usable}")
    return usable[0]


def get_screen_size(adb: str, serial: str, use_cache: bool = True) -> tuple[int, int]:
    """读取设备当前屏幕分辨率，用于坐标合法性校验与触摸坐标换算。

    :param adb: adb 可执行文件路径。
    :param serial: 目标设备序列号。
    :param use_cache: 是否使用缓存（同一设备重复查询时省去一次 adb 往返）。
    :return: (宽, 高)；解析失败时返回 (0, 0) 表示未知。
    """
    if use_cache and serial in _SCREEN_CACHE:
        return _SCREEN_CACHE[serial]
    result = run_adb(adb, ["-s", serial, "shell", "wm", "size"])
    for line in result.stdout.splitlines():
        if ":" in line:
            size = line.split(":", 1)[1].strip()
            if "x" in size:
                w, h = size.split("x", 1)
                try:
                    value = (int(w), int(h))
                except ValueError:
                    return 0, 0
                _SCREEN_CACHE[serial] = value
                return value
    return 0, 0


def screencap_png(adb: str, serial: str, timeout: float = 20.0) -> bytes:
    """抓取设备当前屏幕截图（PNG 原始字节流）。

    走 `exec-out`（不经过 shell 文本模式），避免 Windows 下换行符破坏 PNG 数据。

    :param adb: adb 可执行文件路径。
    :param serial: 目标设备序列号。
    :param timeout: 超时秒数。
    :return: PNG 文件字节流。
    :raises RuntimeError: 抓取失败或返回内容为空时抛出。
    """
    result = subprocess.run(
        [adb, "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True, timeout=timeout,
        creationflags=NO_WINDOW_FLAGS,
    )
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip() if result.stderr else "无输出"
        raise RuntimeError(f"截屏失败: {detail}")
    return result.stdout


def save_screencap(adb: str, serial: str, target: str) -> str:
    """抓屏并保存为 PNG 文件（供找图模板制作与调试使用）。

    :param adb: adb 可执行文件路径。
    :param serial: 目标设备序列号。
    :param target: 保存到的 PNG 文件路径。
    :return: 实际写入的文件路径。
    """
    data = screencap_png(adb, serial)
    with open(target, "wb") as fp:
        fp.write(data)
    return target


# ---------------------------------------------------------------------------
# 触摸设备探测（getevent -lp 解析）
# ---------------------------------------------------------------------------

def _parse_max_value(line: str) -> int:
    """从 `getevent -lp` 的能力描述行中解析 max 数值。

    :param line: 形如 "ABS_MT_POSITION_X : value 0, min 0, max 12599, ..." 的文本行。
    :return: max 数值；解析失败返回 0。
    """
    match = re.search(r"max\s+(\d+)", line)
    return int(match.group(1)) if match else 0


def find_touch_device(adb: str, serial: str) -> tuple[str, int, int]:
    """从 `getevent -lp` 中找出主触摸屏的设备节点及其原始坐标范围。

    多个候选设备时取坐标范围面积最大者（指纹等小面积设备会被排除）。

    :param adb: adb 可执行文件路径。
    :param serial: 目标设备序列号。
    :return: (设备节点路径, X 原始最大值, Y 原始最大值)。
    :raises RuntimeError: 未找到任何带位置能力的输入设备时抛出。
    """
    result = run_adb(adb, ["-s", serial, "shell", "getevent", "-lp"])
    devices: list[tuple[str, int, int]] = []
    node, x_max, y_max = "", 0, 0
    for line in result.stdout.splitlines():
        text = line.strip()
        if text.startswith("add device"):
            if node and x_max and y_max:
                devices.append((node, x_max, y_max))
            node = text.split(":", 1)[1].strip() if ":" in text else ""
            x_max = y_max = 0
        elif "ABS_MT_POSITION_X" in text:
            x_max = _parse_max_value(text)
        elif "ABS_MT_POSITION_Y" in text:
            y_max = _parse_max_value(text)
    if node and x_max and y_max:
        devices.append((node, x_max, y_max))

    if not devices:
        raise RuntimeError("未找到触摸屏输入设备，可改用开发者选项中的「指针位置」查看坐标")
    return max(devices, key=lambda item: item[1] * item[2])


def _to_int(value: str) -> Optional[int]:
    """把 getevent 输出的值字符串按十六进制转为整数。

    :param value: 形如 "00004d2" 的十六进制字符串。
    :return: 对应整数；非数值（如 DOWN/UP）时返回 None。
    """
    try:
        return int(value, 16)
    except ValueError:
        return None


def _parse_event_line(line: str) -> Optional[tuple[str, str]]:
    """解析 `getevent -l` 的单行输出。

    :param line: 形如 "/dev/input/event6: EV_ABS ABS_MT_POSITION_X 00004d2" 的文本行。
    :return: (事件标签, 原始值字符串)；无法解析时返回 None。
    """
    parts = line.split()
    if len(parts) < 3:
        return None
    return parts[-2], parts[-1]


# ---------------------------------------------------------------------------
# 触摸流采集类（坐标拾取 + 录制复用）
# ---------------------------------------------------------------------------

class TouchStream:
    """持续监听手机触摸屏输入流，把原始触摸坐标换算为屏幕像素后回调输出。

    一期已验证：`getevent` 只能捕获**真实手指触摸**，程序注入的点击不会出现在
    该数据流中，因此本类天然只记录人类操作，不会自激。

    回调 `on_event(kind, x, y, ts)` 中：
    - kind 取值 "down" / "move" / "up"，分别表示手指按下、移动、抬起；
    - x, y 为换算后的屏幕像素坐标；
    - ts 为 PC 侧时间戳（time.time()），用于计算手势之间的相对延时。
    """

    def __init__(self, adb: str, serial: str, width: int, height: int,
                 on_event: Callable[[str, int, int, float], None] | None = None) -> None:
        """初始化触摸流监听器（此时尚未启动）。

        :param adb: adb 可执行文件路径。
        :param serial: 目标设备序列号。
        :param width: 屏幕宽度像素（用于原始坐标换算）。
        :param height: 屏幕高度像素（用于原始坐标换算）。
        :param on_event: 事件回调函数，签名 (kind, x, y, ts)；可为 None。
        """
        self.adb = adb
        self.serial = serial
        self.width = width
        self.height = height
        self.on_event = on_event
        self.node = ""
        self.x_max = 0
        self.y_max = 0

        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._active = False
        self._emitted_down = False
        self._have_x = False
        self._have_y = False
        self._raw_x = 0
        self._raw_y = 0
        self._last_error = ""

    def probe(self) -> tuple[str, int, int]:
        """探测触摸设备节点与其原始坐标范围。

        :return: (设备节点路径, X 原始最大值, Y 原始最大值)。
        :raises RuntimeError: 未找到触摸屏设备时抛出。
        """
        self.node, self.x_max, self.y_max = find_touch_device(self.adb, self.serial)
        return self.node, self.x_max, self.y_max

    def start(self) -> None:
        """启动后台监听线程（先自动探测设备节点，如未探测过）。

        :return: 无返回值。
        :raises RuntimeError: 触摸设备探测失败时抛出。
        """
        if self._running:
            return
        if not self.node:
            self.probe()
        self._proc = subprocess.Popen(
            [self.adb, "-s", self.serial, "shell", "getevent", "-l", self.node],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW_FLAGS,
        )
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """后台线程主体：逐行读取 getevent 输出并解析为触摸事件。

        :return: 无返回值。
        """
        assert self._proc is not None and self._proc.stdout is not None
        while self._running:
            line = self._proc.stdout.readline()
            if line == "":
                if self._running:
                    self._last_error = "触摸事件流已中断，请检查设备连接"
                break
            parsed = _parse_event_line(line)
            if parsed:
                self._handle(parsed[0], parsed[1], time.time())
        self._running = False

    def _handle(self, label: str, value: str, ts: float) -> None:
        """处理单条解析后的事件（维护手势状态并触发回调）。

        :param label: 事件标签，如 ABS_MT_POSITION_X / ABS_MT_TRACKING_ID。
        :param value: 原始值字符串。
        :param ts: 该事件到达时的 PC 侧时间戳。
        :return: 无返回值。
        """
        if label == "ABS_MT_TRACKING_ID":
            if value.lower().startswith("ffffffff"):
                # 手指抬起：结束当前手势
                if self._active and self._have_x and self._have_y:
                    self._emit("up", self._raw_x, self._raw_y, ts)
                self._active = False
                self._emitted_down = False
            else:
                # 新手指按下
                self._active = True
                self._emitted_down = False
        elif label in ("ABS_MT_POSITION_X", "ABS_X"):
            position = _to_int(value)
            if position is not None:
                self._raw_x, self._have_x = position, True
                self._maybe_move(ts)
        elif label in ("ABS_MT_POSITION_Y", "ABS_Y"):
            position = _to_int(value)
            if position is not None:
                self._raw_y, self._have_y = position, True
                self._maybe_move(ts)

    def _maybe_move(self, ts: float) -> None:
        """在坐标齐备时输出 down（首次）或 move（后续）事件。

        :param ts: 事件时间戳。
        :return: 无返回值。
        """
        if not (self._active and self._have_x and self._have_y):
            return
        if not self._emitted_down:
            self._emitted_down = True
            self._emit("down", self._raw_x, self._raw_y, ts)
        else:
            self._emit("move", self._raw_x, self._raw_y, ts)

    def _emit(self, kind: str, raw_x: int, raw_y: int, ts: float) -> None:
        """把原始触摸坐标换算为屏幕像素并调用回调（回调异常不影响监听）。

        :param kind: 事件类型 down / move / up。
        :param raw_x: 原始 X 坐标值。
        :param raw_y: 原始 Y 坐标值。
        :param ts: 事件时间戳。
        :return: 无返回值。
        """
        if not self.on_event:
            return
        pixel_x = round(raw_x * self.width / self.x_max) if self.x_max else raw_x
        pixel_y = round(raw_y * self.height / self.y_max) if self.y_max else raw_y
        try:
            self.on_event(kind, pixel_x, pixel_y, ts)
        except Exception:  # 回调内部异常不应中断监听线程
            pass

    def stop(self) -> None:
        """停止监听：终止 getevent 进程并回收线程。

        :return: 无返回值。
        """
        self._running = False
        if self._proc:
            try:
                self._proc.kill()
            except OSError:
                pass
            try:
                if self._proc.stdout:
                    self._proc.stdout.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._proc = None
        self._thread = None

    def error(self) -> str:
        """返回最近一次监听中断的错误描述。

        :return: 错误信息文本；正常运行时为空字符串。
        """
        return self._last_error


# ---------------------------------------------------------------------------
# 持久 shell 通道
# ---------------------------------------------------------------------------

class ShellChannel:
    """持久化的 adb shell 通道，采用「回显标记」实现 PC 与手机端严格同步。

    每条命令形如 `input tap X Y; echo <MARK>`，只有当读回 MARK 时才认为本次命令
    已被手机执行完毕，从而避免命令在管道中堆积、停止后仍继续点击的问题。
    """

    def __init__(self, adb: str, serial: str, mark: str = MARK) -> None:
        """初始化通道（此时立即启动手机端 shell 进程）。

        :param adb: adb 可执行文件路径。
        :param serial: 目标设备序列号。
        :param mark: 同步标记字符串，需保证不与命令输出冲突。
        """
        self.mark = mark
        self.proc = subprocess.Popen(
            [adb, "-s", serial, "shell", "-T"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # 合并 stderr，便于把 input 的报错（如权限不足）一并捕获
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=NO_WINDOW_FLAGS,
        )

    def _wait_mark(self, timeout: float = 8.0) -> tuple[str, list[str]]:
        """等待手机端回显同步标记。

        :param timeout: 最长等待秒数；超时会终止通道，避免读线程悬挂。
        :return: (状态, 非标记输出行)；状态为 OK / EOF / TIMEOUT。
        """
        holder: dict[str, object] = {}

        def reader() -> None:
            lines: list[str] = []
            while True:
                line = self.proc.stdout.readline()
                if line == "":
                    holder["status"], holder["lines"] = "EOF", lines
                    return
                text = line.rstrip("\r\n")
                if text == self.mark:
                    holder["status"], holder["lines"] = "OK", lines
                    return
                lines.append(text)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.close()
            return "TIMEOUT", []
        return str(holder["status"]), list(holder.get("lines", []))

    def handshake(self, timeout: float = 6.0) -> tuple[str, list[str]]:
        """建立通道后进行握手，确认手机端 shell 可正常应答。

        :param timeout: 握手最长等待秒数。
        :return: (状态, 非标记输出行)，状态为 OK 表示通道可用。
        """
        try:
            self.proc.stdin.write(f"echo {self.mark}\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return "EOF", []
        return self._wait_mark(timeout)

    def command(self, cmd: str) -> tuple[float, str, list[str]]:
        """在手机端执行一条 shell 命令并等待其完成。

        :param cmd: 要执行的命令文本（不含同步标记）。
        :return: (耗时毫秒, 状态, 非标记输出行)。
        """
        started = time.perf_counter()
        try:
            self.proc.stdin.write(f"{cmd}; echo {self.mark}\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            return 0.0, "EOF", []
        status, lines = self._wait_mark()
        return (time.perf_counter() - started) * 1000, status, lines

    def alive(self) -> bool:
        """判断通道进程是否仍在运行。

        :return: 进程存活返回 True，否则 False。
        """
        return self.proc.poll() is None

    def close(self) -> None:
        """关闭通道：关闭 stdin 会令手机端 shell 读到 EOF 后自然退出。

        :return: 无返回值。
        """
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
