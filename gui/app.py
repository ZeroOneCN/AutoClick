# -*- coding: utf-8 -*-
"""AutoClick 图形界面（tkinter/ttk）。

设计要点：
- 界面层不含业务逻辑，所有设备操作都通过 core/ 完成；
- tkinter 非线程安全：耗时任务一律在后台线程执行，通过 queue.Queue + root.after
  轮询把日志/结果回传到主线程，禁止跨线程直接操作控件；
- 关闭窗口时确保后台线程、shell 通道与触摸流监听被正确释放。

本文件按里程碑逐步扩展：M7 设备栏 + 固定坐标连点 + 日志 + 停止；后续将加入
流程编排、录制与找图/找控件。
"""
from __future__ import annotations

import copy
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Callable, Optional

from core import device
from core import flow as flowmod
from core import storage
from core.actions import Actions
from core.device import ShellChannel, TouchStream
from core.recorder import Recorder
from core.vision import Locator

# 界面统一字体：Windows 自带的「微软雅黑」（Microsoft YaHei）。它带 ClearType 字形
# 微调信息，小字号下比点阵渲染的「宋体」清晰得多；日志区再放大 1 号以免长时间阅读吃力。
UI_FONT = "微软雅黑"
UI_FONT_SIZE = 10
LOG_FONT_SIZE = 11

# 定时执行输入框的格式提示（未设定定时时的默认文案）
SCHEDULE_HINT = "格式 10:00:00.000（当天）或 +30（延时秒）"


class AutoClickApp:
    """AutoClick 主窗口应用类。

    维护设备连接状态、后台任务（连点/流程/录制）与界面控件联动。
    """

    POLL_MS = 80  # 队列轮询间隔（毫秒）
    MONITOR_INTERVAL = 3.0  # 设备在线状态检测间隔（秒）

    def __init__(self, root: tk.Tk) -> None:
        """初始化应用：构建界面并启动队列轮询。

        :param root: tkinter 主窗口对象。
        """
        self.root = root
        self.root.title("AutoClick - 安卓自动化工具")
        self.root.geometry("880x660")
        self.root.resizable(False, False)  # 窗口固定 880x660，不允许拉伸
        self._setup_fonts()

        # ---- 设备状态 ----
        self.adb_path: Optional[str] = None   # 已确认可用的 adb 路径
        self.serial: Optional[str] = None     # 已连接设备序列号
        self.screen: tuple[int, int] = (0, 0)  # 屏幕分辨率

        # ---- 任务状态 ----
        self.queue: queue.Queue = queue.Queue()
        self.running = False          # 是否有连点/流程/录制任务在运行
        self.connecting = False       # 是否正在连接设备
        self.stop_event = threading.Event()
        self.monitor_stop = threading.Event()  # 设备在线监测线程的退出信号
        self.schedule_ts: Optional[float] = None  # 已设定的定时执行目标时刻（time.time() 基准）
        self.schedule_label = ""      # 定时目标的文字描述，用于倒计时显示
        self._input_spawn_ms = 0.0    # 设备端 input 进程启动开销估计（毫秒），定时执行时测量
        self.picker: Optional[TouchStream] = None  # 坐标拾取监听器
        self.pick_handler: Optional[Callable[[int, int], None]] = None  # 拾取结果回填函数
        self._pick_buttons: list[tuple[ttk.Button, str]] = []  # (拾取按钮, 常态文案)

        # ---- 流程状态（M8） ----
        self.flow: dict = flowmod.new_flow()
        self.flow_path: Optional[str] = None
        self.param_vars: dict[str, tk.StringVar] = {}

        # ---- 录制状态（M9） ----
        self.recorder: Optional[Recorder] = None   # 正在运行的录制器
        self.recorded_steps: list[dict] = []       # 最近一次录制的步骤
        self._record_shown = 0                     # 录制列表中已显示的行数

        # ---- 界面 ----
        self._build_ui()

        self.root.after(self.POLL_MS, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._prefill_adb()
        self._start_device_monitor()
        self.log("欢迎使用 AutoClick。请先点「刷新设备」并「连接」后再执行任务。")
        self.log("提示：连接安卓手机后需开启 USB 调试并在手机上同意授权。")

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _setup_fonts(self) -> None:
        """把界面全局字体统一设置为微软雅黑（含 ttk 控件、树列表与文本控件）。

        tkinter 的默认命名字体（TkDefaultFont 等）会被 ttk 控件继承，修改它们即可
        一次性覆盖绝大多数控件；Treeview 的行与表头不跟随默认字体，需单独设置样式；
        日志文本控件在创建时显式指定字体，见 `_build_log_area`。

        :return: 无返回值。
        """
        names = ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                 "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont")
        for name in names:
            try:
                font = tkfont.nametofont(name)
            except tk.TclError:
                continue
            font.configure(family=UI_FONT, size=UI_FONT_SIZE)
        style = ttk.Style(self.root)
        # 适当加大行高，避免中文行贴在一起
        style.configure("Treeview", font=(UI_FONT, UI_FONT_SIZE), rowheight=24)
        style.configure("Treeview.Heading", font=(UI_FONT, UI_FONT_SIZE))

    def _build_ui(self) -> None:
        """构建主界面骨架：设备栏 + 分页区 + 底部按钮 + 日志区。

        分页区与日志区放入垂直分割面板（PanedWindow），可拖动分隔条调整高度，
        避免固定比例留下大片空白。

        :return: 无返回值。
        """
        self._build_device_bar()

        self.paned = ttk.PanedWindow(self.root, orient="vertical")
        self.paned.pack(fill="both", expand=True, padx=4, pady=(2, 0))

        self.tabs = ttk.Notebook(self.paned)
        # 分页区与日志区按 2:1 分配，避免分页内容较少时中间留下大片空白
        self.paned.add(self.tabs, weight=2)
        quick = ttk.Frame(self.tabs)
        self.tabs.add(quick, text="固定坐标连点")
        self._build_quick_tab(quick)

        flow_tab = ttk.Frame(self.tabs)
        self.tabs.add(flow_tab, text="流程编排")
        self.flow_tab = flow_tab
        self._build_flow_tab(flow_tab)

        record_tab = ttk.Frame(self.tabs)
        self.tabs.add(record_tab, text="录制回放")
        self.record_tab = record_tab
        self._build_record_tab(record_tab)

        self._build_log_area(self.paned)

        self.status_var = tk.StringVar(value="未连接")
        status = ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief="sunken")
        status.pack(fill="x", side="bottom")

    def _build_device_bar(self) -> None:
        """构建设备栏：adb 路径、设备下拉、分辨率与连接按钮。

        :return: 无返回值。
        """
        frame = ttk.LabelFrame(self.root, text="设备")
        frame.pack(fill="x", padx=4, pady=(2, 1))

        ttk.Label(frame, text="adb 路径:").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=3)
        self.adb_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.adb_var, width=32).grid(
            row=0, column=1, columnspan=3, sticky="we", pady=3
        )
        ttk.Button(frame, text="浏览…", width=7, command=self._browse_adb).grid(
            row=0, column=4, padx=4, pady=3
        )
        ttk.Button(frame, text="自动探测", width=9, command=self._auto_detect_adb).grid(
            row=0, column=5, padx=(0, 6), pady=3
        )

        ttk.Label(frame, text="设备:").grid(row=1, column=0, sticky="w", padx=(8, 4), pady=3)
        self.serial_var = tk.StringVar()
        self.device_combo = ttk.Combobox(frame, textvariable=self.serial_var, state="readonly", width=20)
        self.device_combo.grid(row=1, column=1, sticky="we", pady=3)
        ttk.Button(frame, text="刷新设备", width=9, command=self.refresh_devices).grid(
            row=1, column=2, padx=4, pady=3
        )
        # 连接按钮紧跟「刷新设备」，方便连接后一眼看到
        self.connect_btn = ttk.Button(frame, text="连接", width=8, command=self.connect_device)
        self.connect_btn.grid(row=1, column=3, padx=(4, 6), pady=3)
        ttk.Label(frame, text="分辨率:").grid(row=1, column=4, sticky="e", padx=(6, 4), pady=3)
        self.size_var = tk.StringVar(value="未知")
        ttk.Label(frame, textvariable=self.size_var, width=12, anchor="w").grid(
            row=1, column=5, columnspan=2, sticky="w", pady=3
        )

        frame.columnconfigure(1, weight=1)

    def _build_quick_tab(self, parent: ttk.Frame) -> None:
        """构建「固定坐标连点」页：坐标、拾取、间隔/次数/时长与开始/停止按钮。

        :param parent: 该页的父容器。
        :return: 无返回值。
        """
        # 参数框填满整个分页，避免窗口较大时下方留下大片空白
        box = ttk.LabelFrame(parent, text="点击参数")
        box.pack(fill="both", expand=True, padx=4, pady=(2, 2))

        ttk.Label(box, text="X 坐标:").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=3)
        self.qx_var = tk.StringVar(value="540")
        ttk.Entry(box, textvariable=self.qx_var, width=11).grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(box, text="Y 坐标:").grid(row=0, column=2, sticky="e", padx=(12, 4), pady=3)
        self.qy_var = tk.StringVar(value="1200")
        ttk.Entry(box, textvariable=self.qy_var, width=11).grid(row=0, column=3, sticky="w", pady=3)
        self.pick_btn = ttk.Button(box, text="拾取坐标（手机点一下）", width=20,
                                   command=lambda: self.toggle_pick(self._fill_quick_xy))
        self.pick_btn.grid(row=0, column=4, padx=(12, 6), pady=3)
        self._register_pick_button(self.pick_btn, "拾取坐标（手机点一下）")

        ttk.Label(box, text="间隔(秒):").grid(row=1, column=0, sticky="w", padx=(8, 4), pady=3)
        self.qint_var = tk.StringVar(value="0")
        ttk.Entry(box, textvariable=self.qint_var, width=11).grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(box, text="次数(0=无限):").grid(row=1, column=2, sticky="e", padx=(12, 4), pady=3)
        self.qcnt_var = tk.StringVar(value="0")
        ttk.Entry(box, textvariable=self.qcnt_var, width=11).grid(row=1, column=3, sticky="w", pady=3)
        ttk.Label(box, text="时长(秒，空=不限):").grid(row=1, column=4, sticky="e", padx=(12, 4), pady=3)
        self.qdur_var = tk.StringVar(value="")
        ttk.Entry(box, textvariable=self.qdur_var, width=9).grid(row=1, column=5, sticky="w", pady=3)

        actions = ttk.Frame(parent)
        actions.pack(fill="x", padx=4, pady=(0, 3))
        self.start_btn = ttk.Button(actions, text="开始连点", width=12, command=self.start_click)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(actions, text="停止", width=12, command=self.stop_task, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Label(actions, text="  全速模式约 40 次/秒；间隔 0 表示全速").pack(side="left")

    def _build_log_area(self, parent: ttk.PanedWindow) -> None:
        """构建底部日志区（只读文本 + 滚动条 + 清空按钮）。

        日志区加入垂直分割面板并可随窗口拉伸（`weight=1`），默认可显示约 10 行，
        需要更多日志时向下拖动分隔条即可放大；按钮与文本框之间不留多余空白。

        :param parent: 承载日志区的垂直分割面板（ttk.PanedWindow）。
        :return: 无返回值。
        """
        frame = ttk.LabelFrame(parent, text="日志")

        # 日志用微软雅黑 11 号，并加一点行距，长时间查看更清晰不费眼；
        # wrap="word" 让超长行自动折行为多行显示，无需横向滚动
        self.log_text = tk.Text(frame, height=10, wrap="word", state="disabled",
                                font=(UI_FONT, LOG_FONT_SIZE),
                                spacing1=1, spacing2=2, spacing3=1)
        scroll_y = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll_y.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.log_text.tag_configure("error", foreground="#c62828")
        self.log_text.tag_configure("warn", foreground="#ef6c00")
        self.log_text.tag_configure("ok", foreground="#2e7d32")

        # 清空按钮改为竖排文字（自上而下读），与日志区共用网格，不单独占一行
        btn_bar = ttk.Frame(frame)
        btn_bar.grid(row=0, column=2, sticky="ns", padx=(2, 0))
        ttk.Button(btn_bar, text="清\n空\n日\n志", width=4, command=self._clear_log).pack(side="top")

        parent.add(frame, weight=1)

    # ------------------------------------------------------------------
    # 日志与队列
    # ------------------------------------------------------------------

    def log(self, text: str, level: str = "info") -> None:
        """线程安全地输出一条日志（实际写入由主线程轮询完成）。

        :param text: 日志正文。
        :param level: 级别，info / warn / error / ok，用于着色。
        :return: 无返回值。
        """
        self.queue.put(("log", (text, level)))

    def _append_log(self, text: str, level: str) -> None:
        """主线程内把日志追加到文本框（带时间戳）。

        :param text: 日志正文。
        :param level: 日志级别。
        :return: 无返回值。
        """
        stamp = time.strftime("%H:%M:%S")
        tag = "" if level == "info" else level
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self) -> None:
        """清空日志区内容。

        :return: 无返回值。
        """
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _poll_queue(self) -> None:
        """主线程周期性消费队列中的消息并更新界面。

        :return: 无返回值。
        """
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                self._handle_message(kind, payload)
        except queue.Empty:
            pass
        if self.recorder is not None:
            try:
                self._sync_record_tree()
            except tk.TclError:
                return
        try:
            self.root.after(self.POLL_MS, self._poll_queue)
        except tk.TclError:
            pass

    def _handle_message(self, kind: str, payload: object) -> None:
        """处理来自后台线程的单条消息。

        :param kind: 消息类型。
        :param payload: 消息负载。
        :return: 无返回值。
        """
        if kind == "log":
            text, level = payload  # type: ignore[misc]
            self._append_log(text, level)
        elif kind == "devices":
            self._apply_devices(payload)  # type: ignore[arg-type]
        elif kind == "connect_done":
            self._apply_connect_result(payload)  # type: ignore[arg-type]
        elif kind == "device_state":
            self._apply_device_state(payload)  # type: ignore[arg-type]
        elif kind == "pick":
            x, y = payload  # type: ignore[misc]
            handler, self.pick_handler = self.pick_handler, None
            if handler:
                handler(x, y)
            self._stop_pick()
            self._append_log(f"已拾取坐标: ({x}, {y})", "ok")
        elif kind == "task_end":
            self._finish_task(str(payload))
        elif kind == "template_saved":
            self._apply_template_name(payload)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # 设备：探测与连接
    # ------------------------------------------------------------------

    def _prefill_adb(self) -> None:
        """启动时尝试自动探测 adb 路径并预填到输入框。

        :return: 无返回值。
        """
        try:
            self.adb_var.set(device.find_adb())
        except FileNotFoundError:
            self.adb_var.set("")

    def _browse_adb(self) -> None:
        """通过文件对话框选择 adb 可执行文件。

        :return: 无返回值。
        """
        path = filedialog.askopenfilename(
            title="选择 adb.exe", filetypes=[("adb", "adb.exe"), ("可执行文件", "*.exe"), ("全部", "*.*")]
        )
        if path:
            self.adb_var.set(path)

    def _auto_detect_adb(self) -> None:
        """手动触发一次 adb 路径自动探测。

        :return: 无返回值。
        """
        try:
            path = device.find_adb()
            self.adb_var.set(path)
            self.log(f"已探测到 adb: {path}", "ok")
        except FileNotFoundError as exc:
            self.adb_var.set("")
            self.log(str(exc), "error")

    def refresh_devices(self) -> None:
        """刷新设备列表（后台线程执行 adb devices）。

        :return: 无返回值。
        """
        if self.running or self.connecting:
            return
        self.connecting = True
        self._update_widget_states()
        self.log("正在枚举设备…")
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        """后台线程：定位 adb 并枚举设备。

        :return: 无返回值。
        """
        try:
            adb = device.find_adb(self.adb_var.get().strip() or None)
            self.adb_path = adb
            self.queue.put(("devices", device.list_devices(adb)))
        except FileNotFoundError as exc:
            self.queue.put(("log", (str(exc), "error")))
            self.queue.put(("devices", []))
        except Exception as exc:  # 设备枚举失败（如 adb server 异常）
            self.queue.put(("log", (f"枚举设备失败: {exc}", "error")))
            self.queue.put(("devices", []))

    def _apply_devices(self, devices: list[tuple[str, str]]) -> None:
        """把设备列表填充到下拉框（主线程）。

        :param devices: [(序列号, 状态), ...]。
        :return: 无返回值。
        """
        values = [f"{serial} [{state}]" for serial, state in devices]
        self.device_combo.configure(values=values)
        if values and not self.serial_var.get():
            self.device_combo.current(0)
        if devices:
            self.log(f"发现 {len(devices)} 台设备: {values}", "ok")
        else:
            self.log("未发现设备，请检查 USB 连接与 USB 调试开关。", "warn")
        self.connecting = False
        self._update_widget_states()

    def connect_device(self) -> None:
        """连接（校验）当前选中的设备并读取分辨率。

        :return: 无返回值。
        """
        if self.running or self.connecting:
            return
        self.connecting = True
        self._update_widget_states()
        self.log("正在连接设备…")
        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self) -> None:
        """后台线程：解析 adb、确定序列号并读取分辨率。

        :return: 无返回值。
        """
        try:
            adb = device.find_adb(self.adb_var.get().strip() or None)
            serial = device.resolve_serial(adb, self._selected_serial())
            width, height = device.get_screen_size(adb, serial, use_cache=False)
            if not width or not height:
                raise RuntimeError("无法获取屏幕分辨率（wm size 无有效输出）")
            self.queue.put(("connect_done", (True, adb, serial, (width, height), "")))
        except FileNotFoundError as exc:
            self.queue.put(("connect_done", (False, "", "", (0, 0), str(exc))))
        except RuntimeError as exc:
            self.queue.put(("connect_done", (False, "", "", (0, 0), str(exc))))
        except Exception as exc:
            self.queue.put(("connect_done", (False, "", "", (0, 0), f"{type(exc).__name__}: {exc}")))

    def _apply_connect_result(self, payload: tuple) -> None:
        """应用连接结果（主线程）。

        :param payload: (成功?, adb, serial, (宽,高), 错误信息)。
        :return: 无返回值。
        """
        ok, adb, serial, size, error = payload
        self.connecting = False
        if ok:
            self.adb_path, self.serial, self.screen = adb, serial, size
            self.size_var.set(f"{size[0]}x{size[1]}")
            self.status_var.set(f"已连接  {serial}  {size[0]}x{size[1]}")
            self.log(f"连接成功：{serial} | 分辨率 {size[0]}x{size[1]} | adb {adb}", "ok")
        else:
            self.adb_path = self.serial = None
            self.screen = (0, 0)
            self.size_var.set("未知")
            self.status_var.set("未连接")
            self.log(f"连接失败：{error}", "error")
        self._update_widget_states()

    def _selected_serial(self) -> Optional[str]:
        """从设备下拉框解析出纯序列号（去掉状态后缀）。

        :return: 序列号；未选择时返回 None。
        """
        text = self.serial_var.get().strip()
        return text.split(" ")[0] if text else None

    # ------------------------------------------------------------------
    # 设备在线监测（实时测活）
    # ------------------------------------------------------------------

    def _start_device_monitor(self) -> None:
        """启动后台线程，周期性检测已连接设备是否仍然在线。

        掉线 / 恢复都会即时写日志并刷新状态栏，避免设备中断了却毫无察觉；
        未连接设备时空转等待，不产生任何 adb 调用。

        :return: 无返回值。
        """
        threading.Thread(target=self._device_monitor_worker, daemon=True).start()

    def _device_monitor_worker(self) -> None:
        """后台线程：每 3 秒枚举一次 adb 设备，比对当前设备的在线状态。

        仅在已连接（self.serial 有值）时才执行检测；adb 瞬时异常（如 server
        正在重启）静默跳过等下一轮，不误报掉线。状态变化通过队列交给主线程提示。

        :return: 无返回值。
        """
        watched: Optional[str] = None
        online = True
        while not self.monitor_stop.wait(self.MONITOR_INTERVAL):
            adb, serial = self.adb_path, self.serial
            if not (adb and serial):
                continue
            try:
                devices = device.list_devices(adb)
            except Exception:
                continue
            if serial != watched:  # 刚连接（或换了一台设备）：重置基线，由本轮结果修正
                watched, online = serial, True
            now_online = any(s == serial and st == "device" for s, st in devices)
            if now_online != online:
                online = now_online
                self.queue.put(("device_state", (serial, now_online)))

    def _apply_device_state(self, payload: tuple) -> None:
        """主线程：处理设备在线状态变化（掉线 / 恢复），写日志并刷新状态栏。

        :param payload: (序列号, 是否在线)。
        :return: 无返回值。
        """
        serial, online = payload
        if online:
            size = f"  {self.screen[0]}x{self.screen[1]}" if self.screen[0] else ""
            self.status_var.set(f"已连接  {serial}{size}")
            self.log(f"设备已恢复连接：{serial}", "ok")
        else:
            self.status_var.set(f"设备已断开  {serial}")
            self.log(f"检测到设备连接断开：{serial}，请检查 USB 线与授权状态", "error")

    # ------------------------------------------------------------------
    # 固定坐标连点（M7 核心能力）
    # ------------------------------------------------------------------

    def start_click(self) -> None:
        """读取参数并启动连点任务（后台线程）。

        :return: 无返回值。
        """
        if self.running or self.connecting:
            return
        if not self._require_connected():
            return
        try:
            x = int(self.qx_var.get().strip())
            y = int(self.qy_var.get().strip())
        except ValueError:
            messagebox.showerror("参数错误", "X / Y 坐标必须是整数")
            return
        width, height = self.screen
        if not (0 <= x < width and 0 <= y < height):
            messagebox.showerror("参数错误", f"坐标 ({x}, {y}) 超出屏幕范围 {width}x{height}")
            return
        try:
            interval = float(self.qint_var.get().strip() or "0")
            count = int(self.qcnt_var.get().strip() or "0")
            duration_text = self.qdur_var.get().strip()
            duration = float(duration_text) if duration_text else None
        except ValueError:
            messagebox.showerror("参数错误", "间隔 / 次数 / 时长必须是数字（时长可留空）")
            return
        if interval < 0 or count < 0 or (duration is not None and duration <= 0):
            messagebox.showerror("参数错误", "间隔与次数不能为负，时长必须大于 0")
            return

        self.stop_event.clear()
        self.running = True
        self._update_widget_states()
        plan = f"持续 {duration:g} 秒" if duration is not None else (
            f"共 {count} 次" if count else "不限次数（点「停止」结束）")
        pace = f"间隔 {interval:g}s" if interval > 0 else "全速"
        self.log(f"开始连点：目标 ({x}, {y}) | {plan} | {pace}")
        threading.Thread(
            target=self._click_worker, args=(x, y, interval, count, duration), daemon=True
        ).start()

    def _click_worker(self, x: int, y: int, interval: float, count: int, duration: Optional[float]) -> None:
        """后台线程：建立通道并按配置循环点击。

        :param x: 点击 X 坐标。
        :param y: 点击 Y 坐标。
        :param interval: 每次点击间隔秒数，0 表示全速。
        :param count: 点击次数，0 表示不限次数（设置 duration 时忽略）。
        :param duration: 持续秒数，None 表示不限制时长。
        :return: 无返回值。
        """
        reason = "完成计划次数"
        done = 0
        started = time.perf_counter()
        channel: Optional[ShellChannel] = None
        try:
            assert self.adb_path and self.serial
            channel = ShellChannel(self.adb_path, self.serial)
            status, lines = channel.handshake(6.0)
            if status != "OK":
                self.log(f"无法与手机端 shell 建立通道（{status}），请重新插拔 USB 或重启 adb", "error")
                self.queue.put(("task_end", "通道建立失败"))
                return
            for line in lines:
                self.log(f"  手机端: {line}", "warn")

            actions = Actions(channel)
            effective_count = 0 if duration is not None else count
            last_report = started
            warned = False
            while not self.stop_event.is_set():
                if effective_count and done >= effective_count:
                    break
                if duration is not None and (time.perf_counter() - started) >= duration:
                    reason = "达到设定时长"
                    break

                tick_started = time.perf_counter()
                _, st, out = actions.tap(x, y)
                if st != "OK":
                    reason = f"通道异常({st})"
                    break
                done += 1

                if out and not warned:
                    warned = True
                    self.log(f"手机端返回信息（可能权限不足或坐标无效）: {out[:5]}", "warn")

                if interval > 0:
                    rest = interval - (time.perf_counter() - tick_started)
                    if rest > 0 and self.stop_event.wait(rest):
                        reason = "用户停止"
                        break

                now = time.perf_counter()
                if now - last_report >= 1.0:
                    last_report = now
                    elapsed = now - started
                    self.log(f"  已点击 {done} 次 | {elapsed:.1f}s | {done / elapsed:.1f} 次/秒")
        except Exception as exc:
            reason = f"异常: {type(exc).__name__}: {exc}"
            self.log(f"连点任务异常：{reason}", "error")
        finally:
            if channel is not None:
                channel.close()

        elapsed = time.perf_counter() - started
        rate = done / elapsed if elapsed > 0 else 0.0
        self.log(f"连点结束：{reason} | 共 {done} 次 | 耗时 {elapsed:.2f}s | 平均 {rate:.1f} 次/秒",
                 "ok" if done else "warn")
        self.queue.put(("task_end", reason))

    # ------------------------------------------------------------------
    # 坐标拾取（复用 core.device.TouchStream）
    # ------------------------------------------------------------------

    def _register_pick_button(self, button: ttk.Button, normal_text: str) -> None:
        """登记一个「拾取坐标」按钮，便于统一刷新其文案（拾取中 → 停止拾取）。

        :param button: 拾取按钮控件。
        :param normal_text: 未拾取时的常态文案。
        :return: 无返回值。
        """
        self._pick_buttons.append((button, normal_text))

    def toggle_pick(self, handler: Optional[Callable[[int, int], None]] = None) -> None:
        """切换坐标拾取：未拾取时启动监听，拾取中则停止。

        :param handler: 拾取到坐标后的回填函数 (x, y)；为空时回填到「固定坐标连点」页。
        :return: 无返回值。
        """
        if self.picker:
            self._stop_pick()
            return
        if self.running:
            messagebox.showwarning("提示", "任务运行中，无法同时拾取坐标")
            return
        if self.recorder:
            messagebox.showwarning("提示", "录制进行中，无法同时拾取坐标")
            return
        if not self._require_connected():
            return
        width, height = self.screen
        picker = TouchStream(self.adb_path or "", self.serial or "", width, height,
                             on_event=self._on_pick_event)
        try:
            node, x_max, y_max = picker.probe()
            picker.start()
        except RuntimeError as exc:
            messagebox.showerror("拾取失败", str(exc))
            self.log(f"坐标拾取失败：{exc}", "error")
            return
        self.picker = picker
        self.pick_handler = handler or self._fill_quick_xy
        for button, _text in self._pick_buttons:
            try:
                button.configure(text="停止拾取")
            except tk.TclError:
                pass
        self.log(f"坐标拾取已开启（触摸设备 {node}，原始范围 {x_max}x{y_max}）。"
                 f"请在手机上点击目标位置，坐标会自动回填。", "ok")

    def _on_pick_event(self, kind: str, x: int, y: int, ts: float) -> None:
        """触摸流回调（运行在监听线程）：手指抬起时把坐标投递到队列。

        :param kind: 事件类型 down / move / up。
        :param x: 屏幕 X 坐标。
        :param y: 屏幕 Y 坐标。
        :param ts: 事件时间戳。
        :return: 无返回值。
        """
        if kind == "up":
            self.queue.put(("pick", (x, y)))

    def _stop_pick(self) -> None:
        """停止坐标拾取并恢复所有拾取按钮的文案。

        :return: 无返回值。
        """
        picker, self.picker = self.picker, None
        self.pick_handler = None
        if picker:
            picker.stop()
            error = picker.error()
            if error:
                self.log(f"坐标拾取中断：{error}", "warn")
            else:
                self.log("坐标拾取已停止。")
        for button, text in self._pick_buttons:
            try:
                button.configure(text=text)
            except tk.TclError:
                pass

    def _fill_quick_xy(self, x: int, y: int) -> None:
        """把拾取到的坐标回填到「固定坐标连点」页的 X / Y 输入框。

        :param x: 屏幕 X 坐标。
        :param y: 屏幕 Y 坐标。
        :return: 无返回值。
        """
        self.qx_var.set(str(x))
        self.qy_var.set(str(y))

    # ------------------------------------------------------------------
    # 任务状态与控件联动
    # ------------------------------------------------------------------

    def stop_task(self) -> None:
        """请求停止当前任务（连点/流程/录制）。

        :return: 无返回值。
        """
        if not self.running:
            return
        self.stop_event.set()
        self.log("已请求停止，等待当前动作收尾…", "warn")

    def _finish_task(self, reason: str) -> None:
        """任务结束后恢复界面状态（主线程）。

        :param reason: 结束原因（仅用于日志）。
        :return: 无返回值。
        """
        self.running = False
        self.stop_event.clear()
        self._reset_schedule_state()
        self._update_widget_states()

    def _reset_schedule_state(self) -> None:
        """清除定时执行状态与倒计时显示（主线程）。

        任务结束（含被取消）时调用，使「定时执行」按钮重新可用、「取消定时」置灰。

        :return: 无返回值。
        """
        self.schedule_ts = None
        self.schedule_label = ""
        state_var = getattr(self, "schedule_state_var", None)
        if state_var is not None:
            state_var.set(SCHEDULE_HINT)

    def _update_widget_states(self) -> None:
        """根据运行/连接状态统一刷新控件可用性。

        :return: 无返回值。
        """
        blocked = self.running or self.connecting or self.recorder is not None
        state = "disabled" if blocked else "normal"
        widgets = [self.connect_btn, self.start_btn, self.pick_btn,
                   getattr(self, "flow_start_btn", None), getattr(self, "flow_step_btn", None),
                   getattr(self, "flow_from_btn", None), getattr(self, "schedule_btn", None),
                   getattr(self, "record_btn", None)]
        for widget in widgets:
            if widget is None:
                continue
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        self.stop_btn.configure(state="normal" if self.running else "disabled")
        schedule_cancel = getattr(self, "schedule_cancel_btn", None)
        if schedule_cancel is not None:
            # 仅在已设定定时、等待倒计时期间可取消
            schedule_cancel.configure(
                state="normal" if self.schedule_ts is not None and not self.connecting else "disabled")
        for name in ("flow_stop_btn", "record_stop_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(state="disabled")
        record_stop = getattr(self, "record_stop_btn", None)
        if record_stop is not None:
            record_stop.configure(state="normal" if self.recorder else "disabled")
        flow_stop = getattr(self, "flow_stop_btn", None)
        if flow_stop is not None:
            flow_stop.configure(state="normal" if self.running else "disabled")

    def _require_connected(self) -> bool:
        """检查设备是否已连接。

        :return: 已连接返回 True；否则提示并返回 False。
        """
        if self.adb_path and self.serial and self.screen[0]:
            return True
        messagebox.showwarning("提示", "请先点「刷新设备」并「连接」完成设备连接")
        return False

    # ------------------------------------------------------------------
    # 流程编排（M8）
    # ------------------------------------------------------------------

    def _build_flow_tab(self, parent: ttk.Frame) -> None:
        """构建「流程编排」页：流程信息栏 + 步骤列表 + 参数编辑区 + 执行按钮。

        :param parent: 该页的父容器。
        :return: 无返回值。
        """
        self._param_pick_buttons: list[ttk.Button] = []

        info = ttk.LabelFrame(parent, text="流程信息")
        info.pack(fill="x", padx=4, pady=(2, 1))
        ttk.Label(info, text="名称:").grid(row=0, column=0, sticky="w", padx=(8, 4), pady=3)
        self.flow_name_var = tk.StringVar(value=self.flow.get("name", "未命名流程"))
        ttk.Entry(info, textvariable=self.flow_name_var, width=16).grid(
            row=0, column=1, sticky="we", pady=3)
        ttk.Label(info, text="重复(0=∞):").grid(row=0, column=2, sticky="e", padx=(10, 4), pady=3)
        self.flow_repeat_var = tk.StringVar(
            value=str((self.flow.get("control") or {}).get("repeat", 1)))
        ttk.Entry(info, textvariable=self.flow_repeat_var, width=5).grid(
            row=0, column=3, sticky="w", pady=3)
        self.flow_stop_var = tk.BooleanVar(
            value=bool((self.flow.get("control") or {}).get("stop_on_error", True)))
        ttk.Checkbutton(info, text="出错即停止", variable=self.flow_stop_var).grid(
            row=0, column=4, padx=(10, 6), pady=3)
        ttk.Button(info, text="新建", width=7, command=self.new_flow).grid(
            row=0, column=5, padx=3, pady=3)
        ttk.Button(info, text="保存", width=7, command=self.save_flow).grid(
            row=0, column=6, padx=3, pady=3)
        ttk.Button(info, text="加载", width=7, command=self.load_flow).grid(
            row=0, column=7, padx=(3, 8), pady=3)

        # 到点自动触发：适用于抢购/准点类场景——提前把通道预热好并测出往返延迟，
        # 到设定时刻立即执行，把"人的反应时间"换成机器等待。
        ttk.Label(info, text="定时:").grid(row=1, column=0, sticky="w", padx=(8, 4), pady=3)
        self.schedule_var = tk.StringVar(value="")
        ttk.Entry(info, textvariable=self.schedule_var, width=16).grid(
            row=1, column=1, sticky="we", pady=3)
        self.schedule_btn = ttk.Button(info, text="定时执行", width=9, command=self.arm_schedule)
        self.schedule_btn.grid(row=1, column=2, padx=(10, 3), pady=3)
        self.schedule_cancel_btn = ttk.Button(info, text="取消定时", width=9,
                                              command=self.cancel_schedule, state="disabled")
        self.schedule_cancel_btn.grid(row=1, column=3, padx=3, pady=3)
        self.schedule_state_var = tk.StringVar(value=SCHEDULE_HINT)
        ttk.Label(info, textvariable=self.schedule_state_var, anchor="w").grid(
            row=1, column=4, columnspan=4, sticky="we", padx=(10, 8), pady=3)
        info.columnconfigure(1, weight=1)  # 窗口变窄时优先压缩流程名输入框

        # 底部「开始执行/停止」栏先用 side="bottom" 占位，再铺步骤编辑区，
        # 保证内容再多按钮也始终可见（修复开始按钮被挤出可视区的问题）
        bottom = ttk.Frame(parent)
        bottom.pack(side="bottom", fill="x", padx=4, pady=(0, 3))
        self.flow_start_btn = ttk.Button(bottom, text="开始执行", width=10, command=self.start_flow)
        self.flow_start_btn.pack(side="left", padx=(0, 4))
        # 调试用：单步试跑 / 从选中步骤开始执行，避免改一处就要从头跑整条流程
        self.flow_step_btn = ttk.Button(bottom, text="试跑本步", width=9,
                                        command=self.run_selected_step)
        self.flow_step_btn.pack(side="left", padx=4)
        self.flow_from_btn = ttk.Button(bottom, text="从选中开始", width=11,
                                        command=self.run_from_selected)
        self.flow_from_btn.pack(side="left", padx=4)
        self.flow_stop_btn = ttk.Button(bottom, text="停止", width=8, command=self.stop_task,
                                        state="disabled")
        self.flow_stop_btn.pack(side="left", padx=(4, 6))
        ttk.Label(bottom, text="循环请成对使用「循环开始 / 循环结束」。").pack(side="left")

        body = ttk.Panedwindow(parent, orient="horizontal")
        body.pack(fill="both", expand=True, padx=4, pady=(2, 2))

        left = ttk.Frame(body)
        bar = ttk.Frame(left)
        bar.pack(fill="x")
        add_btn = ttk.Menubutton(bar, text="添加步骤 ▾", width=11)
        add_menu = tk.Menu(add_btn, tearoff=False)
        for step_type in flowmod.STEP_TYPES:
            label = flowmod.STEP_LABELS.get(step_type, step_type)
            add_menu.add_command(label=f"{label}（{step_type}）",
                                 command=lambda t=step_type: self.add_step(t))
        add_btn.configure(menu=add_menu)
        add_btn.pack(side="left", padx=(0, 6))
        for text, command in (("删除", self.delete_step),
                              ("复制", self.duplicate_step),
                              ("上移", lambda: self.move_step(-1)),
                              ("下移", lambda: self.move_step(1))):
            ttk.Button(bar, text=text, width=5, command=command).pack(side="left", padx=2)

        tree_wrap = ttk.Frame(left)
        tree_wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.step_tree = ttk.Treeview(tree_wrap, columns=("no", "type", "summary"),
                                      show="headings", height=8, selectmode="browse")
        self.step_tree.heading("no", text="#")
        self.step_tree.column("no", width=32, anchor="center", stretch=False)
        self.step_tree.heading("type", text="类型")
        self.step_tree.column("type", width=92, stretch=False)
        self.step_tree.heading("summary", text="参数摘要")
        self.step_tree.column("summary", width=200)
        tree_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.step_tree.yview)
        self.step_tree.configure(yscrollcommand=tree_scroll.set)
        self.step_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.step_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_step_select())

        right = ttk.Frame(body)
        self.param_box = ttk.LabelFrame(right, text="步骤参数")
        self.param_box.pack(fill="both", expand=True)
        self.param_hint = ttk.Label(self.param_box, text="在左侧选择一个步骤后编辑参数",
                                    foreground="#666666", justify="left", wraplength=230)
        self.param_hint.pack(anchor="w", padx=8, pady=(4, 0))
        self.param_frame = ttk.Frame(self.param_box)
        self.param_frame.pack(fill="both", expand=True, padx=8, pady=4)

        body.add(left, weight=3)
        body.add(right, weight=2)

    def _sync_flow_from_ui(self) -> None:
        """把界面上的流程级参数（名称、整体重复、出错即停止）写回流程字典。

        :return: 无返回值。
        """
        self.flow["name"] = self.flow_name_var.get().strip() or "未命名流程"
        try:
            repeat = int(self.flow_repeat_var.get().strip() or "0")
        except ValueError:
            repeat = 1
        self.flow["control"] = {"repeat": max(0, repeat),
                                "stop_on_error": bool(self.flow_stop_var.get())}

    def _apply_flow_to_ui(self) -> None:
        """把流程字典的流程级参数刷新到界面，并重建步骤列表。

        :return: 无返回值。
        """
        self.flow_name_var.set(self.flow.get("name", "未命名流程"))
        control = self.flow.get("control") or {}
        self.flow_repeat_var.set(str(control.get("repeat", 1)))
        self.flow_stop_var.set(bool(control.get("stop_on_error", True)))
        self._refresh_step_tree()
        self._clear_param_fields()
        self.param_hint.configure(text="在左侧选择一个步骤后编辑参数")

    def _refresh_step_tree(self) -> None:
        """按当前流程重建步骤列表，并尽量保持原选中项。

        :return: 无返回值。
        """
        selected = self._selected_index()
        self.step_tree.delete(*self.step_tree.get_children())
        steps = self.flow.get("steps") or []
        for index, step in enumerate(steps):
            kind = step.get("type", "?")
            self.step_tree.insert("", "end", iid=str(index),
                                  values=(index + 1, flowmod.STEP_LABELS.get(kind, kind),
                                          flowmod.step_summary(step)))
        if selected is not None and 0 <= selected < len(steps):
            self.step_tree.selection_set(str(selected))
            self._on_step_select()

    def _selected_index(self) -> Optional[int]:
        """获取步骤列表中当前选中项的下标。

        :return: 选中下标；未选中返回 None。
        """
        selection = self.step_tree.selection()
        return int(selection[0]) if selection else None

    def _on_step_select(self) -> None:
        """列表选中变化回调：重建右侧参数编辑区。

        :return: 无返回值。
        """
        index = self._selected_index()
        self._clear_param_fields()
        steps = self.flow.get("steps") or []
        if index is None or index >= len(steps):
            self.param_hint.configure(text="在左侧选择一个步骤后编辑参数")
            return
        step = steps[index]
        kind = step.get("type", "?")
        self.param_hint.configure(text=f"第 {index + 1} 步：{flowmod.STEP_LABELS.get(kind, kind)}"
                                       + ("（该步骤无需参数）" if not flowmod.STEP_FIELDS.get(kind) else ""))
        self._build_param_fields(step)

    def _clear_param_fields(self) -> None:
        """清空右侧参数编辑区，并注销其中登记过的拾取按钮。

        :return: 无返回值。
        """
        self.param_vars.clear()
        param_buttons = getattr(self, "_param_pick_buttons", [])
        if param_buttons:
            keep = set(id(b) for b in param_buttons)
            self._pick_buttons = [(w, t) for w, t in self._pick_buttons if id(w) not in keep]
            param_buttons.clear()
        for child in self.param_frame.winfo_children():
            child.destroy()

    def _field_value(self, step: dict, key: str) -> str:
        """读取步骤字段用于显示（swipe 的 x1/y1/x2/y2 映射到 from/to 数组）。

        :param step: 步骤字典。
        :param key: 字段名。
        :return: 字段的字符串形式。
        """
        if key in ("x1", "y1", "x2", "y2"):
            src = step.get("from") or [0, 0]
            dst = step.get("to") or [0, 0]
            source = {"x1": 0, "y1": 1, "x2": 0, "y2": 1}[key]
            pair = src if key in ("x1", "y1") else dst
            value = pair[source] if isinstance(pair, (list, tuple)) and len(pair) > source else 0
            return str(value)
        value = step.get(key, "")
        return "" if value is None else str(value)

    def _build_param_fields(self, step: dict) -> None:
        """根据步骤类型动态生成参数输入控件（含拾取按钮与「应用修改」）。

        :param step: 需要编辑的步骤字典。
        :return: 无返回值。
        """
        fields = flowmod.STEP_FIELDS.get(step.get("type"), [])
        if not fields:
            ttk.Label(self.param_frame, text="该步骤无需参数",
                      foreground="#666666").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        for row, (key, label, value_type) in enumerate(fields):
            ttk.Label(self.param_frame, text=f"{label}:").grid(
                row=row, column=0, sticky="w", padx=4, pady=3)
            var = tk.StringVar(value=self._field_value(step, key))
            self.param_vars[key] = var
            if value_type.startswith("choice:"):
                options = value_type.split(":", 1)[1].split("|")
                widget: ttk.Widget = ttk.Combobox(self.param_frame, textvariable=var,
                                                  values=options, state="readonly", width=17)
            else:
                widget = ttk.Entry(self.param_frame, textvariable=var, width=20)
            widget.grid(row=row, column=1, sticky="w", pady=3, padx=4)

        kind = step.get("type")
        button_row = len(fields)
        if kind in ("tap", "long_press"):
            btn = ttk.Button(self.param_frame, text="拾取坐标", width=12,
                             command=lambda: self.toggle_pick(self._fill_param_xy))
            btn.grid(row=button_row, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 2))
            self._track_param_pick_button(btn, "拾取坐标")
        elif kind == "swipe":
            btn_start = ttk.Button(self.param_frame, text="拾取起点", width=12,
                                   command=lambda: self.toggle_pick(self._fill_param_start))
            btn_start.grid(row=button_row, column=0, sticky="w", padx=4, pady=(8, 2))
            self._track_param_pick_button(btn_start, "拾取起点")
            btn_end = ttk.Button(self.param_frame, text="拾取终点", width=12,
                                 command=lambda: self.toggle_pick(self._fill_param_end))
            btn_end.grid(row=button_row, column=1, sticky="w", padx=4, pady=(8, 2))
            self._track_param_pick_button(btn_end, "拾取终点")
        elif kind == "find_click":
            btn_file = ttk.Button(self.param_frame, text="选择模板…", width=12,
                                  command=self._pick_template_file)
            btn_file.grid(row=button_row, column=0, sticky="w", padx=4, pady=(8, 2))
            btn_cap = ttk.Button(self.param_frame, text="抓屏存为模板…", width=14,
                                 command=self._capture_template)
            btn_cap.grid(row=button_row, column=1, sticky="w", padx=4, pady=(8, 2))
        ttk.Button(self.param_frame, text="应用修改", width=12, command=self.apply_params).grid(
            row=button_row + 1, column=0, columnspan=2, sticky="w", padx=4, pady=(6, 2))

    def _track_param_pick_button(self, button: ttk.Button, normal_text: str) -> None:
        """登记参数区中的拾取按钮（拾取进行中时直接显示为「停止拾取」）。

        :param button: 拾取按钮。
        :param normal_text: 常态文案。
        :return: 无返回值。
        """
        self._register_pick_button(button, normal_text)
        self._param_pick_buttons.append(button)
        if self.picker:
            button.configure(text="停止拾取")

    def apply_params(self) -> None:
        """把参数区的输入写回当前选中的步骤，并刷新列表摘要。

        :return: 无返回值。
        """
        index = self._selected_index()
        steps = self.flow.get("steps") or []
        if index is None or index >= len(steps):
            messagebox.showwarning("提示", "请先在左侧选择一个步骤")
            return
        step = steps[index]
        values: dict = {}
        for key, label, value_type in flowmod.STEP_FIELDS.get(step.get("type"), []):
            raw = self.param_vars[key].get().strip()
            try:
                if value_type == "int":
                    values[key] = int(raw or "0")
                elif value_type == "float":
                    values[key] = float(raw or "0")
                else:
                    values[key] = raw
            except ValueError:
                messagebox.showerror("参数错误", f"「{label}」的值不合法：{raw}")
                return
        if step.get("type") == "swipe":
            step["from"] = [values.pop("x1", 0), values.pop("y1", 0)]
            step["to"] = [values.pop("x2", 0), values.pop("y2", 0)]
            step["duration"] = values.pop("duration", 300)
            for legacy in ("x1", "y1", "x2", "y2"):
                step.pop(legacy, None)
        else:
            step.update(values)
        self._refresh_step_tree()
        self.log(f"已更新第 {index + 1} 步参数", "ok")

    def add_step(self, step_type: str) -> None:
        """在流程末尾追加一个指定类型的步骤并选中它。

        :param step_type: 步骤类型。
        :return: 无返回值。
        """
        self._sync_flow_from_ui()
        steps = self.flow.setdefault("steps", [])
        steps.append(flowmod.new_step(step_type))
        self._refresh_step_tree()
        index = len(steps) - 1
        self.step_tree.selection_set(str(index))
        self.step_tree.see(str(index))
        self._on_step_select()
        self.log(f"已添加步骤：{flowmod.STEP_LABELS.get(step_type, step_type)}")

    def delete_step(self) -> None:
        """删除当前选中的步骤。

        :return: 无返回值。
        """
        index = self._selected_index()
        steps = self.flow.get("steps") or []
        if index is None or index >= len(steps):
            messagebox.showwarning("提示", "请先在左侧选择一个步骤")
            return
        removed = steps.pop(index)
        self._refresh_step_tree()
        self._on_step_select()
        self.log(f"已删除第 {index + 1} 步：{flowmod.step_summary(removed)}")

    def duplicate_step(self) -> None:
        """复制当前选中的步骤并插入到其后。

        :return: 无返回值。
        """
        index = self._selected_index()
        steps = self.flow.get("steps") or []
        if index is None or index >= len(steps):
            messagebox.showwarning("提示", "请先在左侧选择一个步骤")
            return
        steps.insert(index + 1, dict(steps[index]))
        self._refresh_step_tree()
        self.step_tree.selection_set(str(index + 1))
        self._on_step_select()

    def move_step(self, delta: int) -> None:
        """把当前选中步骤上移或下移一个位置。

        :param delta: -1 上移，1 下移。
        :return: 无返回值。
        """
        index = self._selected_index()
        steps = self.flow.get("steps") or []
        if index is None:
            return
        target = index + delta
        if not (0 <= target < len(steps)):
            return
        steps[index], steps[target] = steps[target], steps[index]
        self._refresh_step_tree()
        self.step_tree.selection_set(str(target))
        self._on_step_select()

    def _fill_param_xy(self, x: int, y: int) -> None:
        """把拾取到的坐标回填到当前步骤的 X / Y 参数框。

        :param x: 屏幕 X 坐标。
        :param y: 屏幕 Y 坐标。
        :return: 无返回值。
        """
        if "x" in self.param_vars:
            self.param_vars["x"].set(str(x))
        if "y" in self.param_vars:
            self.param_vars["y"].set(str(y))

    def _fill_param_start(self, x: int, y: int) -> None:
        """把拾取到的坐标回填到 swipe 步骤的起点参数框。

        :param x: 屏幕 X 坐标。
        :param y: 屏幕 Y 坐标。
        :return: 无返回值。
        """
        if "x1" in self.param_vars:
            self.param_vars["x1"].set(str(x))
        if "y1" in self.param_vars:
            self.param_vars["y1"].set(str(y))

    def _fill_param_end(self, x: int, y: int) -> None:
        """把拾取到的坐标回填到 swipe 步骤的终点参数框。

        :param x: 屏幕 X 坐标。
        :param y: 屏幕 Y 坐标。
        :return: 无返回值。
        """
        if "x2" in self.param_vars:
            self.param_vars["x2"].set(str(x))
        if "y2" in self.param_vars:
            self.param_vars["y2"].set(str(y))

    def _pick_template_file(self) -> None:
        """打开文件对话框选择找图模板，并把结果回填到 target 参数框。

        模板位于 `templates/` 目录内时只回填文件名（便于随流程保存与搬迁）。

        :return: 无返回值。
        """
        var = self.param_vars.get("target")
        if var is None:
            return
        folder = storage.templates_dir()
        path = filedialog.askopenfilename(
            title="选择找图模板", initialdir=folder,
            filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")])
        if not path:
            return
        base = os.path.dirname(os.path.abspath(path))
        value = os.path.basename(path) if os.path.normcase(base) == os.path.normcase(
            os.path.abspath(folder)) else path
        var.set(value)
        self.log(f"已选择模板：{value}", "ok")

    def _capture_template(self) -> None:
        """抓取当前屏幕保存为找图模板（在后台线程执行，避免阻塞界面）。

        :return: 无返回值。
        """
        if not self._require_connected():
            return
        var = self.param_vars.get("target")
        if var is None:
            return
        name = var.get().strip()
        if not name:
            from tkinter import simpledialog
            name = simpledialog.askstring("保存模板", "请输入模板文件名（含扩展名）:", initialvalue="目标.png")
            if not name:
                return
        if not name.lower().endswith(".png"):
            name += ".png"
        target = name
        self.log(f"正在抓屏保存模板：{target} …")
        threading.Thread(target=self._capture_template_worker, args=(target,), daemon=True).start()

    def _capture_template_worker(self, target: str) -> None:
        """后台线程：抓屏并写入模板文件，结果通过队列回传主线程。

        :param target: 模板文件名或路径。
        :return: 无返回值。
        """
        try:
            path = Locator(self.adb_path or "", self.serial or "", self.screen[0],
                           self.screen[1]).capture_template(target)
            self.queue.put(("template_saved", (True, target, path, "")))
        except Exception as exc:
            self.queue.put(("template_saved", (False, target, "", f"{type(exc).__name__}: {exc}")))

    def _apply_template_name(self, payload: tuple) -> None:
        """主线程：根据抓屏结果回填模板名并输出日志。

        :param payload: (成功?, 模板名, 绝对路径, 错误信息)。
        :return: 无返回值。
        """
        ok, name, path, error = payload
        if ok:
            var = self.param_vars.get("target")
            if var is not None and self._selected_index() is not None:
                var.set(name)
            self.log(f"模板已保存：{path}", "ok")
        else:
            self.log(f"保存模板失败：{error}", "error")

    def new_flow(self) -> None:
        """新建一条空流程（不保存，需用户点「保存」）。

        :return: 无返回值。
        """
        self.flow = flowmod.new_flow(screen=self.screen)
        self.flow_path = None
        self._apply_flow_to_ui()
        self.log("已新建空流程")

    def save_flow(self) -> None:
        """把当前流程保存为 JSON（首次保存时弹出文件对话框）。

        :return: 无返回值。
        """
        self._sync_flow_from_ui()
        if self.screen[0]:
            self.flow["screen"] = {"width": self.screen[0], "height": self.screen[1]}
        path = self.flow_path
        if not path:
            path = filedialog.asksaveasfilename(
                title="保存流程", initialdir=storage.flows_dir(),
                initialfile=f"{self.flow['name']}.json", defaultextension=".json",
                filetypes=[("流程 JSON", "*.json")])
            if not path:
                return
        try:
            storage.save_flow(path, self.flow)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self.flow_path = path
        self.log(f"已保存流程：{path}", "ok")

    def load_flow(self) -> None:
        """从 JSON 文件加载流程并刷新界面。

        :return: 无返回值。
        """
        path = filedialog.askopenfilename(
            title="加载流程", initialdir=storage.flows_dir(),
            filetypes=[("流程 JSON", "*.json"), ("全部文件", "*.*")])
        if not path:
            return
        try:
            flow = storage.load_flow(path)
        except (FileNotFoundError, ValueError) as exc:
            messagebox.showerror("加载失败", str(exc))
            return
        self.flow = flow
        self.flow_refresh_warn(path)
        self._apply_flow_to_ui()
        count = len(flow.get("steps") or [])
        self.log(f"已加载流程：{path}（{count} 步）", "ok")

    def flow_refresh_warn(self, path: str) -> None:
        """加载流程后，若制作分辨率与当前设备不一致则给出提示。

        :param path: 流程文件路径（仅用于日志）。
        :return: 无返回值。
        """
        self.flow_path = path
        screen = self.flow.get("screen") or {}
        if self.screen[0] and screen.get("width") and \
                (screen.get("width"), screen.get("height")) != self.screen:
            self.log(f"注意：该流程按 {screen.get('width')}x{screen.get('height')} 制作，"
                     f"当前设备为 {self.screen[0]}x{self.screen[1]}，坐标可能需要等比换算", "warn")

    def start_flow(self) -> None:
        """校验并启动流程执行（后台线程）。

        :return: 无返回值。
        """
        if self.running or self.connecting:
            return
        if not self._require_connected():
            return
        self._sync_flow_from_ui()
        steps = self.flow.get("steps") or []
        if not steps:
            messagebox.showwarning("提示", "流程还没有步骤，请先「添加步骤」")
            return
        problems = flowmod.validate_flow(self.flow, self.screen)
        if problems:
            for item in problems[:8]:
                self.log(f"校验：{item}", "error")
            messagebox.showerror("流程校验失败", "\n".join(problems[:8]))
            return
        self.stop_event.clear()
        self.running = True
        self._update_widget_states()
        repeat = (self.flow.get("control") or {}).get("repeat", 1)
        self.log(f"开始执行流程「{self.flow.get('name')}」：{len(steps)} 步，整体重复 "
                 f"{'无限' if not repeat else repeat} 次")
        self._launch_flow(self.flow)

    def run_selected_step(self) -> None:
        """试跑选中的单个步骤（单步调试）。

        只校验并执行该步骤本身，流程其它位置的问题不会阻塞本次试跑，便于"改一处验一处"。

        :return: 无返回值。
        """
        index = self._prepare_debug_run(single=True)
        if index is None:
            return
        self._launch_flow(self.flow, single=index)

    def run_from_selected(self) -> None:
        """从选中的步骤开始执行到流程末尾（调试用，仅执行 1 轮）。

        与「开始执行」的区别：不从头跑、也不按流程的整体重复次数循环，
        只把选中的这一步及其后续步骤走一遍。

        :return: 无返回值。
        """
        index = self._prepare_debug_run()
        if index is None:
            return
        depth = self._loop_depth_before(index)
        if depth:
            self.log(f"提示：第 {index + 1} 步位于循环体内（未闭合循环 {depth} 层），"
                     f"从中间起跑时循环体只走一次；需要完整循环请用「开始执行」", "warn")
        self.log(f"试跑：从第 {index + 1} 步开始执行到流程末尾（仅 1 轮）")
        self._launch_flow(self.flow, begin=index)

    def _prepare_debug_run(self, single: bool = False) -> Optional[int]:
        """试跑前的公共准备：状态检查、参数回写、选中步骤与针对性校验。

        :param single: 是否为单步试跑；单步模式只校验该步骤，中途起跑校验其后所有步骤。
        :return: 通过校验的选中步骤下标；不满足条件时返回 None。
        """
        if self.running or self.connecting:
            return None
        if not self._require_connected():
            return None
        self._sync_flow_from_ui()
        steps = self.flow.get("steps") or []
        index = self._selected_index()
        if index is None:
            messagebox.showwarning("提示", "请先在左侧选择一个步骤")
            return None
        if index >= len(steps):
            messagebox.showwarning("提示", "选中的步骤已不存在，请重新选择")
            return None
        problems = self._problems_for_run(
            flowmod.validate_flow(self.flow, self.screen), index, single)
        if problems:
            for item in problems[:8]:
                self.log(f"校验：{item}", "error")
            messagebox.showerror("步骤校验失败", "\n".join(problems[:8]))
            return None
        return index

    @staticmethod
    def _problems_for_run(problems: list[str], index: int, single: bool) -> list[str]:
        """从整条流程的校验结果中筛出与本次试跑有关的问题。

        整条流程校验会连带报出被跳过部分的问题（例如前半段还有没调完的坐标），
        这些不应该挡住"从中途开始调试"，所以只保留涉及试跑范围内步骤的问题；
        全局性问题（如循环未闭合）交由执行引擎在运行时自行提示。

        :param problems: `validate_flow` 返回的问题列表。
        :param index: 试跑起始步骤下标（从 0 开始）。
        :param single: 是否为单步试跑（只保留该步骤自身的问题）。
        :return: 过滤后的问题列表。
        """
        kept: list[str] = []
        for item in problems:
            if not item.startswith("第 "):
                continue
            number = item[2:].split(" 步", 1)[0]
            if not number.isdigit():
                continue
            step_index = int(number) - 1
            if (step_index == index) if single else (step_index >= index):
                kept.append(item)
        return kept

    def _loop_depth_before(self, index: int) -> int:
        """统计指定步骤之前尚未闭合的循环层数。

        :param index: 步骤下标（从 0 开始）。
        :return: 未闭合的循环层数；0 表示该步骤不在循环体内。
        """
        depth = 0
        for step in (self.flow.get("steps") or [])[:index]:
            kind = (step or {}).get("type")
            if kind == "loop":
                depth += 1
            elif kind == "endloop" and depth:
                depth -= 1
        return depth

    # ------------------------------------------------------------------
    # 定时执行（到点自动触发）
    # ------------------------------------------------------------------

    def arm_schedule(self) -> None:
        """校验流程与定时输入，设定到点自动执行（后台线程）。

        与「开始执行」共用同一套流程校验；定时输入支持 `HH:MM:SS[.毫秒]`（当天该时刻）
        与 `+秒数`（从现在起延时）两种写法。

        :return: 无返回值。
        """
        if self.running or self.connecting:
            return
        if not self._require_connected():
            return
        self._sync_flow_from_ui()
        steps = self.flow.get("steps") or []
        if not steps:
            messagebox.showwarning("提示", "流程还没有步骤，请先「添加步骤」")
            return
        problems = flowmod.validate_flow(self.flow, self.screen)
        if problems:
            for item in problems[:8]:
                self.log(f"校验：{item}", "error")
            messagebox.showerror("流程校验失败", "\n".join(problems[:8]))
            return
        parsed = self._parse_schedule_time(self.schedule_var.get())
        if parsed is None:
            messagebox.showwarning("提示", f"定时格式不正确。{SCHEDULE_HINT}")
            return
        target, label = parsed
        now = time.time()
        if target <= now:
            messagebox.showwarning("提示", "该时刻已经过去，请设置一个未来的时间")
            return
        self.schedule_ts = target
        self.schedule_label = label
        self.stop_event.clear()
        self.running = True
        self._update_widget_states()
        self.log(f"已设定定时执行：{label}（还有 {target - now:.3f} 秒），正在预热通道…")
        # 深拷贝一份流程快照：等待期间用户若继续改步骤也不会影响本次执行
        threading.Thread(target=self._schedule_worker,
                         args=(copy.deepcopy(self.flow), target), daemon=True).start()
        self._schedule_tick()

    def cancel_schedule(self) -> None:
        """取消已经设定的定时执行（通过停止信号让等待线程退出）。

        :return: 无返回值。
        """
        if self.schedule_ts is None:
            return
        self.stop_event.set()
        self.log("已请求取消定时执行…", "warn")

    def _schedule_tick(self) -> None:
        """主线程：定时等待期间刷新倒计时显示。

        :return: 无返回值。
        """
        if self.schedule_ts is None:
            return
        remain = self.schedule_ts - time.time()
        if remain > 0:
            self.schedule_state_var.set(f"已设定 {self.schedule_label}，剩余 {remain:.1f} 秒")
            self.root.after(100, self._schedule_tick)

    def _schedule_worker(self, flow: dict, target: float) -> None:
        """后台线程：预热通道 → 测出往返延迟 → 高精度等到目标时刻 → 立即执行流程。

        抢购/准点类场景的胜负取决于「动作落在目标时刻」，而不是「点得多快」。
        因此这里先把通道建好并测出往返延迟，再在 `目标时刻 - 往返延迟` 处下发命令，
        使手机端真正动手的时刻尽量贴合用户设定的时刻（补偿传输开销）。

        :param flow: 流程快照（已深拷贝）。
        :param target: 目标时刻（time.time() 基准，秒）。
        :return: 无返回值。
        """
        channel = None
        try:
            assert self.adb_path and self.serial
            channel = ShellChannel(self.adb_path, self.serial)
            status, lines = channel.handshake(6.0)
            if status != "OK":
                self.log(f"定时执行：无法建立通道（{status}），请重新插拔 USB 或重启 adb", "error")
                self.queue.put(("task_end", "通道建立失败"))
                return
            for line in lines:
                self.log(f"  手机端: {line}", "warn")
            rtt_ms = self._measure_channel_rtt(channel)
            self.log(f"通道已预热：传输延迟约 {rtt_ms:.1f} ms，"
                     f"设备端进程启动约 {self._input_spawn_ms:.1f} ms")
            fire_at = target - rtt_ms / 1000.0
            if fire_at > time.time():
                self.log(f"将在 {self._format_stamp(fire_at)} 下发命令（提前 {rtt_ms:.1f} ms 补偿传输）")
                if not self._wait_until(fire_at):
                    self.log("定时执行已取消", "warn")
                    self.queue.put(("task_end", "已取消定时"))
                    return
            else:
                self.log("剩余时间已不足以补偿通道延迟，立即下发", "warn")
            deviation_ms = (time.time() - target) * 1000
            self.log(f"开始执行（下发时刻与目标偏差 {deviation_ms:+.1f} ms；"
                     f"手机端首步动作预计再晚约 {self._input_spawn_ms:.0f} ms）", "ok")
            actions = Actions(channel)
            vision = Locator(self.adb_path, self.serial, self.screen[0], self.screen[1],
                             on_log=self.log)
            runner = flowmod.FlowRunner(flow, actions, on_log=self.log,
                                        stop_event=self.stop_event, vision=vision)
            ok, reason = runner.run()
            self.log(f"流程结束：{reason}", "ok" if ok else "warn")
        except Exception as exc:
            self.log(f"定时执行异常：{type(exc).__name__}: {exc}", "error")
        finally:
            if channel is not None:
                channel.close()
        self.queue.put(("task_end", "定时任务结束"))

    def _measure_channel_rtt(self, channel: ShellChannel, samples: int = 5) -> float:
        """测量通道往返延迟的中位数（毫秒），用于定时执行的提前量补偿。

        用 `true` 这类无副作用命令测量：它只包含 adb 传输与 shell 命令框架开销，
        不含设备端 `input` 进程的启动时间——后者由 `_measure_input_spawn` 单独测。

        :param channel: 已握手的 shell 通道。
        :param samples: 采样次数；取中位数以避开偶发抖动。
        :return: 往返延迟毫秒数；全部采样失败时返回 0.0。
        """
        values: list[float] = []
        for _ in range(samples):
            elapsed, status, _ = channel.command("true")
            if status != "OK":
                break
            values.append(elapsed)
        self._input_spawn_ms = 0.0
        if not values:
            return 0.0
        values.sort()
        median = values[len(values) // 2]
        self._input_spawn_ms = self._measure_input_spawn(channel, median)
        return median

    @staticmethod
    def _measure_input_spawn(channel: ShellChannel, baseline_ms: float) -> float:
        """估算设备端启动一次 `input` 命令的额外开销（毫秒）。

        用无副作用的 `input keyevent 0`（0 号键码无动作）代替真实点击来测量：
        它与 `input tap` 走同一条进程启动路径，只比 `true` 多出进程启动时间。
        若设备不支持该命令，则退化为 0（不额外提示）。

        :param channel: 已握手的 shell 通道。
        :param baseline_ms: 无副作用命令的往返延迟（毫秒），用作扣减基线。
        :return: 额外启动开销毫秒数；无法测量时返回 0.0。
        """
        values: list[float] = []
        for _ in range(3):
            elapsed, status, lines = channel.command("input keyevent 0")
            if status != "OK" or any("Error" in line or "error" in line for line in lines):
                return 0.0
            values.append(elapsed)
        if not values:
            return 0.0
        values.sort()
        return max(0.0, values[len(values) // 2] - baseline_ms)

    def _wait_until(self, target: float) -> bool:
        """高精度等待到指定时间戳。

        先粗睡、再细睡，最后约 2ms 自旋，规避系统 sleep 粒度带来的十几毫秒误差；
        等待期间持续响应「停止 / 取消定时」。

        :param target: 目标时间戳（time.time() 基准，秒）。
        :return: 到达目标返回 True；被取消返回 False。
        """
        while True:
            if self.stop_event.is_set():
                return False
            remain = target - time.time()
            if remain <= 0:
                return True
            if remain > 0.05:
                time.sleep(min(remain - 0.02, 0.05))
            elif remain > 0.002:
                time.sleep(0.001)
            else:
                while time.time() < target:
                    if self.stop_event.is_set():
                        return False

    @staticmethod
    def _parse_schedule_time(text: str) -> Optional[tuple[float, str]]:
        """解析定时输入文本。

        支持 `HH:MM:SS[.毫秒]`（当天该时刻）与 `+秒数`（从现在起延时）两种写法。

        :param text: 用户输入的文本。
        :return: (目标时间戳, 可读描述)；格式不正确时返回 None。
        """
        text = (text or "").strip()
        if not text:
            return None
        if text.startswith("+"):
            try:
                seconds = float(text[1:])
            except ValueError:
                return None
            if seconds <= 0:
                return None
            return time.time() + seconds, f"延时 {seconds:g} 秒"
        parts = text.split(":")
        if len(parts) not in (2, 3):
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
            second = float(parts[2]) if len(parts) == 3 else 0.0
        except ValueError:
            return None
        if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
            return None
        now = time.localtime()
        whole = int(second)
        stamp = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, whole,
                             now.tm_wday, now.tm_yday, -1)) + (second - whole)
        return stamp, f"{hour:02d}:{minute:02d}:{second:06.3f}"

    @staticmethod
    def _format_stamp(stamp: float) -> str:
        """把时间戳格式化为 `HH:MM:SS.mmm`。

        :param stamp: time.time() 基准的时间戳。
        :return: 形如 `10:00:00.250` 的字符串。
        """
        return time.strftime("%H:%M:%S", time.localtime(stamp)) + f".{int(stamp % 1 * 1000):03d}"

    def _launch_flow(self, flow: dict, begin: int = 0, single: int = -1) -> None:
        """把给定流程交给后台线程执行（含任务状态切换）。

        :param flow: 要执行的流程字典。
        :param begin: 起始步骤下标（从 0 开始）；大于 0 表示调试用的中途起跑。
        :param single: 单步试跑的步骤下标；为 -1 表示正常执行流程。
        :return: 无返回值。
        """
        self.stop_event.clear()
        self.running = True
        self._update_widget_states()
        threading.Thread(target=self._flow_worker, args=(flow, begin, single), daemon=True).start()

    def _flow_worker(self, flow: dict, begin: int = 0, single: int = -1) -> None:
        """后台线程：建立通道并运行流程。

        :param flow: 要执行的流程字典。
        :param begin: 起始步骤下标（从 0 开始）。
        :param single: 单步试跑的步骤下标；为 -1 表示正常执行流程。
        :return: 无返回值。
        """
        channel = None
        try:
            assert self.adb_path and self.serial
            channel = ShellChannel(self.adb_path, self.serial)
            status, lines = channel.handshake(6.0)
            if status != "OK":
                self.log(f"无法与手机端 shell 建立通道（{status}），请重新插拔 USB 或重启 adb", "error")
                self.queue.put(("task_end", "通道建立失败"))
                return
            for line in lines:
                self.log(f"  手机端: {line}", "warn")
            actions = Actions(channel)
            vision = Locator(self.adb_path, self.serial, self.screen[0], self.screen[1],
                             on_log=self.log)
            runner = flowmod.FlowRunner(flow, actions, on_log=self.log,
                                        stop_event=self.stop_event, vision=vision, begin=begin)
            if single >= 0:
                ok, reason = runner.run_single(single)
                self.log(f"单步试跑结束：{reason}", "ok" if ok else "warn")
            else:
                ok, reason = runner.run()
                self.log(f"流程结束：{reason}", "ok" if ok else "warn")
        except Exception as exc:
            self.log(f"流程执行异常：{type(exc).__name__}: {exc}", "error")
        finally:
            if channel is not None:
                channel.close()
        self.queue.put(("task_end", "流程结束"))

    # ------------------------------------------------------------------
    # 录制回放（M9）
    # ------------------------------------------------------------------

    def _build_record_tab(self, parent: ttk.Frame) -> None:
        """构建「录制回放」页：录制控制按钮 + 录制结果列表。

        :param parent: 该页的父容器。
        :return: 无返回值。
        """
        info = ttk.LabelFrame(parent, text="录制")
        info.pack(fill="x", padx=4, pady=(2, 1))
        self.record_btn = ttk.Button(info, text="开始录制", width=12, command=self.start_record)
        self.record_btn.grid(row=0, column=0, padx=(8, 6), pady=3)
        self.record_stop_btn = ttk.Button(info, text="停止录制", width=12,
                                          command=self.stop_record, state="disabled")
        self.record_stop_btn.grid(row=0, column=1, padx=6, pady=3)
        ttk.Button(info, text="清空", width=7, command=self.clear_record).grid(
            row=0, column=2, padx=6, pady=3)
        ttk.Button(info, text="应用到流程页", width=13, command=self.apply_record_to_flow).grid(
            row=0, column=3, padx=6, pady=3)
        ttk.Button(info, text="立即回放", width=10, command=self.replay_record).grid(
            row=0, column=4, padx=(6, 8), pady=3)

        ttk.Label(parent, text="说明：录制只记录真实手指操作（点击 / 滑动），程序注入的点击"
                               "不会进入录制流；相邻操作的间隔会自动生成「等待」步骤。",
                  foreground="#666666", justify="left", wraplength=880).pack(
            anchor="w", padx=8, pady=(0, 1))

        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True, padx=4, pady=(0, 3))
        self.record_tree = ttk.Treeview(wrap, columns=("no", "type", "summary"),
                                        show="headings", height=6)
        self.record_tree.heading("no", text="#")
        self.record_tree.column("no", width=32, anchor="center", stretch=False)
        self.record_tree.heading("type", text="类型")
        self.record_tree.column("type", width=92, stretch=False)
        self.record_tree.heading("summary", text="参数摘要")
        self.record_tree.column("summary", width=300)
        record_scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.record_tree.yview)
        self.record_tree.configure(yscrollcommand=record_scroll.set)
        self.record_tree.pack(side="left", fill="both", expand=True)
        record_scroll.pack(side="right", fill="y")

    def start_record(self) -> None:
        """开始录制：建立触摸监听并把真实手势实时转换为步骤。

        :return: 无返回值。
        """
        if self.recorder:
            return
        if self.running or self.connecting:
            messagebox.showwarning("提示", "任务运行中，无法开始录制")
            return
        if not self._require_connected():
            return
        self.clear_record()
        recorder = Recorder(self.adb_path or "", self.serial or "",
                            self.screen[0], self.screen[1], on_log=self.log)
        try:
            recorder.start()
        except RuntimeError as exc:
            messagebox.showerror("录制失败", str(exc))
            self.log(f"录制失败：{exc}", "error")
            return
        self.recorder = recorder
        self._update_widget_states()

    def stop_record(self) -> None:
        """停止录制，并固化完整的步骤列表。

        :return: 无返回值。
        """
        recorder = self.recorder
        if not recorder:
            return
        recorder.stop()
        self._sync_record_tree()
        self.recorder = None
        self.recorded_steps = recorder.steps()
        self.log(f"录制结束：共 {len(self.recorded_steps)} 个步骤", "ok")
        self._update_widget_states()

    def _sync_record_tree(self) -> None:
        """把录制器新产生的步骤增量追加到录制列表。

        :return: 无返回值。
        """
        recorder = self.recorder
        if not recorder:
            return
        steps = recorder.steps()
        while self._record_shown < len(steps):
            step = steps[self._record_shown]
            kind = step.get("type", "?")
            self.record_tree.insert("", "end", iid=str(self._record_shown),
                                    values=(self._record_shown + 1,
                                            flowmod.STEP_LABELS.get(kind, kind),
                                            flowmod.step_summary(step)))
            self._record_shown += 1
        if steps:
            self.record_tree.see(str(len(steps) - 1))

    def clear_record(self) -> None:
        """清空录制结果（录制进行中时不允许清空）。

        :return: 无返回值。
        """
        if self.recorder:
            messagebox.showwarning("提示", "录制进行中，请先停止录制")
            return
        self.record_tree.delete(*self.record_tree.get_children())
        self._record_shown = 0
        self.recorded_steps = []
        self.log("已清空录制结果")

    def apply_record_to_flow(self) -> None:
        """把录制结果写入「流程编排」页，便于编辑与保存。

        :return: 无返回值。
        """
        if self.recorder:
            messagebox.showwarning("提示", "录制进行中，请先停止录制")
            return
        if not self.recorded_steps:
            messagebox.showwarning("提示", "还没有录制的步骤")
            return
        self.flow = {"name": "录制流程",
                     "screen": {"width": self.screen[0], "height": self.screen[1]},
                     "steps": [dict(step) for step in self.recorded_steps],
                     "control": {"repeat": 1, "stop_on_error": True}}
        self.flow_path = None
        self._apply_flow_to_ui()
        self.tabs.select(self.flow_tab)
        self.log(f"已把 {len(self.recorded_steps)} 个录制步骤写入流程页（尚未保存）", "ok")

    def replay_record(self) -> None:
        """直接回放最近一次录制的步骤。

        :return: 无返回值。
        """
        if self.recorder:
            messagebox.showwarning("提示", "录制进行中，请先停止录制")
            return
        if self.running or self.connecting:
            return
        if not self._require_connected():
            return
        if not self.recorded_steps:
            messagebox.showwarning("提示", "还没有录制的步骤")
            return
        flow = {"name": "录制回放",
                "screen": {"width": self.screen[0], "height": self.screen[1]},
                "steps": [dict(step) for step in self.recorded_steps],
                "control": {"repeat": 1, "stop_on_error": True}}
        problems = flowmod.validate_flow(flow, self.screen)
        if problems:
            for item in problems[:8]:
                self.log(f"校验：{item}", "error")
            messagebox.showerror("流程校验失败", "\n".join(problems[:8]))
            return
        self.log(f"开始回放录制结果：{len(flow['steps'])} 步")
        self._launch_flow(flow)

    # ------------------------------------------------------------------
    # 关闭与清理
    # ------------------------------------------------------------------

    def on_close(self) -> None:
        """窗口关闭回调：停止任务与拾取，释放通道后销毁窗口。

        :return: 无返回值。
        """
        if self.running and not messagebox.askokcancel("退出", "任务正在运行，确定退出吗？"):
            return
        self.monitor_stop.set()  # 通知在线监测线程退出
        self.stop_event.set()
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
        self._stop_pick()
        self.root.after(150, self.root.destroy)
