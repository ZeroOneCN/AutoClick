# -*- coding: utf-8 -*-
"""AutoClick 识别层：找控件（默认，零依赖）与找图（可选，依赖 OpenCV）。

两种定位方式（见 DESIGN 4.5），可切换、可自动降级：

| 方式 | 实现 | 依赖 | 适用 |
|---|---|---|---|
| A. control（默认） | `uiautomator dump` 导出界面 XML → 解析节点 `bounds`，按 `text`/`resource-id`/`content-desc` 定位 | **零依赖** | 标准控件、按钮、输入框 |
| B. image | 截屏 + OpenCV 模板匹配 | 需 `opencv-python` + `numpy` | 游戏画面、非控件图片、位置漂移 |

对外只暴露一件事：`Locator.locate(step, timeout, stop_event) -> (x, y) | None`，
命中后返回屏幕像素坐标，由 `core/flow.py` 的 `_do_find_click` 交给 `actions.tap` 执行。

`step` 中与识别相关的字段：
- `mode`：`control`（找控件）或 `image`（找图）
- `target`：找控件时为匹配表达式（见 `Locator._node_matches` 的语法）；找图时为模板文件名/路径
- `threshold`：找图匹配阈值（0~1，默认 0.85）
- `timeout`：超时秒数（默认 5.0）
- `roi`：可选，找图搜索区域 `[x, y, 宽, 高]`（限定范围可显著提速）
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Callable, Optional

from core import device, storage

# 控件匹配表达式前缀（不写前缀时按「全字段模糊匹配」处理）
_PREFIXES = ("text:", "id:", "desc:", "cls:")

# 节点 bounds 形如 "[0,0][1080,2400]"
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def parse_bounds(text: str) -> Optional[tuple[int, int, int, int]]:
    """解析 uiautomator 节点的 bounds 属性。

    :param text: 形如 "[0,0][1080,2400]" 的字符串。
    :return: (left, top, right, bottom)；解析失败返回 None。
    """
    match = _BOUNDS_RE.search(text or "")
    if not match:
        return None
    left, top, right, bottom = (int(v) for v in match.groups())
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def center_of(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    """求控件矩形中心点（用于计算点击坐标）。

    :param bounds: (left, top, right, bottom)。
    :return: (中心 X, 中心 Y)。
    """
    left, top, right, bottom = bounds
    return (left + right) // 2, (top + bottom) // 2


def cv_available() -> bool:
    """探测 OpenCV / numpy 是否可用（用于「找图」方式的可选降级）。

    :return: 两者均可导入返回 True，否则 False。
    """
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except Exception:
        return False
    return True


class Locator:
    """识别器：按 `step` 的配置在屏幕上定位目标并返回屏幕坐标。

    本类不做点击，只负责「找到坐标」；这样引擎可以在命中后自行决定点击方式。
    """

    # 未命中时的重试间隔（秒）：避免在没有任何等待的情况下忙循环刷日志
    RETRY_INTERVAL = 0.3

    def __init__(self, adb: str, serial: str, width: int, height: int,
                 on_log: Optional[Callable[[str, str], None]] = None) -> None:
        """初始化识别器。

        :param adb: adb 可执行文件路径。
        :param serial: 目标设备序列号。
        :param width: 屏幕宽度像素（用于 ROI 校验）。
        :param height: 屏幕高度像素（用于 ROI 校验）。
        :param on_log: 日志回调，签名 (文本, 级别)；可为 None。
        """
        self.adb = adb
        self.serial = serial
        self.width = width
        self.height = height
        self.on_log = on_log

    # ---------------- 对外入口 ----------------

    def locate(self, step: dict, timeout: float, stop_event=None) -> Optional[tuple[int, int]]:
        """在超时时间内反复尝试定位目标，命中即返回坐标。

        :param step: find_click 步骤字典（含 mode / target / threshold / roi）。
        :param timeout: 超时秒数；<=0 表示只尝试一次。
        :param stop_event: threading.Event；被 set 时立即返回 None。
        :return: 命中时的屏幕坐标 (x, y)；超时或中止返回 None。
        """
        mode = str(step.get("mode", "control") or "control")
        deadline = time.perf_counter() + max(0.0, float(timeout or 0))
        attempt = 0
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            attempt += 1
            point = None
            try:
                if mode == "image":
                    point = self.locate_image(step, stop_event)
                else:
                    point = self.locate_control(step, stop_event)
            except Exception as exc:
                self._log(f"识别异常（第 {attempt} 次）: {type(exc).__name__}: {exc}", "warn")
            if point is not None:
                if attempt > 1:
                    self._log(f"识别成功（第 {attempt} 次尝试）: {point}", "ok")
                return point
            if time.perf_counter() >= deadline:
                return None
            if stop_event is not None:
                if stop_event.wait(self.RETRY_INTERVAL):
                    return None
            else:
                time.sleep(self.RETRY_INTERVAL)

    # ---------------- 方式 A：找控件（零依赖） ----------------

    def dump_hierarchy(self, timeout: float = 20.0) -> str:
        """导出当前界面的控件树 XML（uiautomator dump）。

        先在手机上生成 XML 文件，再用 `exec-out cat` 取回，避免 shell 文本模式
        对输出做换行转换。

        :param timeout: 单次 adb 调用超时秒数。
        :return: XML 文本。
        :raises RuntimeError: dump 失败或返回内容不是合法控件树时抛出。
        """
        remote = "/sdcard/window_dump.xml"
        dump = device.run_adb(self.adb, ["-s", self.serial, "shell",
                                         "uiautomator", "dump", remote], timeout=timeout)
        message = (dump.stdout or "").strip()
        if "ERROR" in message.upper() or dump.returncode != 0:
            raise RuntimeError(f"uiautomator dump 失败: {message or dump.stderr.strip()}")
        import subprocess
        result = subprocess.run(
            [self.adb, "-s", self.serial, "exec-out", "cat", remote],
            capture_output=True, timeout=timeout,
            creationflags=device.NO_WINDOW_FLAGS,  # 隐藏 cmd 窗口，与 device 层保持一致
        )
        xml = result.stdout.decode("utf-8", "replace")
        if "<hierarchy" not in xml:
            raise RuntimeError("未取到有效控件树（<hierarchy 缺失），请重试或改用找图方式")
        return xml

    def locate_control(self, step: dict, stop_event=None) -> Optional[tuple[int, int]]:
        """方式 A：导出控件树并按表达式定位一个可点击节点。

        :param step: find_click 步骤字典；使用 `target` 作为匹配表达式。
        :param stop_event: threading.Event；可为 None。
        :return: 命中控件的中心坐标；未命中返回 None。
        """
        target = str(step.get("target", "") or "")
        if not target:
            self._log("找控件未配置目标（text / id / desc 表达式），无法定位", "warn")
            return None
        if stop_event is not None and stop_event.is_set():
            return None
        xml = self.dump_hierarchy()
        return self.find_in_hierarchy(xml, target)

    def find_in_hierarchy(self, xml: str, target: str) -> Optional[tuple[int, int]]:
        """在控件树 XML 中查找匹配节点并返回其中心坐标。

        匹配优先级：可点击且可用的节点 > 匹配到的第一个节点（文本节点的中心
        通常落在其父按钮内部，直接点击仍有效）。

        :param xml: uiautomator 导出的控件树 XML。
        :param target: 匹配表达式，见 `_node_matches`。
        :return: 命中坐标 (x, y)；未命中返回 None。
        """
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise RuntimeError(f"控件树 XML 解析失败: {exc}") from exc

        matched: list[tuple[int, tuple[int, int]]] = []
        for node in root.iter("node"):
            attrs = node.attrib
            if not self._node_matches(attrs, target):
                continue
            bounds = parse_bounds(attrs.get("bounds", ""))
            if bounds is None:
                continue
            clickable = attrs.get("clickable") == "true"
            enabled = attrs.get("enabled", "true") == "true"
            if not enabled:
                continue
            # 可点击节点优先（排序键：越小越优先）
            priority = 0 if clickable else 1
            matched.append((priority, center_of(bounds)))
        if not matched:
            return None
        matched.sort(key=lambda item: item[0])
        return matched[0][1]

    def _node_matches(self, attrs: dict, target: str) -> bool:
        """判断单个控件节点是否匹配目标表达式。

        表达式语法（不区分大小写）：
        - `text:内容`：匹配 `text` 属性（先全等，后包含）
        - `id:内容`：匹配 `resource-id`（全等，或以 `/` 结尾包含）
        - `desc:内容`：匹配 `content-desc`（全等，后包含）
        - `cls:内容`：匹配 `class`（包含）
        - 无前缀：在上述四个属性中任意一项包含该内容即视为匹配

        :param attrs: 节点的属性字典。
        :param target: 匹配表达式。
        :return: 匹配返回 True。
        """
        expr = target.strip()
        lower = expr.lower()
        prefix = ""
        for item in _PREFIXES:
            if lower.startswith(item):
                prefix = item
                expr = expr[len(item):].strip()
                break
        if not expr:
            return False
        needle = expr.lower()

        def contains(*keys: str) -> bool:
            """判断给定属性中是否有任意一项包含目标文本。

            :param keys: 属性名列表。
            :return: 命中返回 True。
            """
            for key in keys:
                if needle in str(attrs.get(key, "")).lower():
                    return True
            return False

        if prefix == "text:":
            return contains("text")
        if prefix == "id:":
            return contains("resource-id")
        if prefix == "desc:":
            return contains("content-desc")
        if prefix == "cls:":
            return contains("class")
        return contains("text", "resource-id", "content-desc")

    # ---------------- 方式 B：找图（需 OpenCV） ----------------

    def locate_image(self, step: dict, stop_event=None) -> Optional[tuple[int, int]]:
        """方式 B：截屏 + 模板匹配定位目标。

        未安装 OpenCV / numpy 时给出中文提示并返回 None（降级而非崩溃）。

        :param step: find_click 步骤字典；`target` 为模板文件名/路径，
                     `threshold` 为匹配阈值，`roi` 为可选搜索区域 [x, y, 宽, 高]。
        :param stop_event: threading.Event；可为 None。
        :return: 命中坐标 (x, y)；未命中返回 None。
        """
        if not cv_available():
            self._log("未安装 OpenCV（opencv-python / numpy），无法使用「找图」方式；"
                      "源码运行请执行 pip install opencv-python numpy，或改用「找控件」方式", "error")
            return None
        import cv2
        import numpy as np

        target = str(step.get("target", "") or "")
        if not target:
            self._log("找图未配置模板文件，无法定位", "warn")
            return None
        path = storage.resolve_template(target)
        if not os.path.isfile(path):
            self._log(f"模板文件不存在: {path}（可在「找图配置」里保存当前截屏为模板）", "error")
            return None
        if stop_event is not None and stop_event.is_set():
            return None

        # 中文路径用 np.fromfile 读取，避免 cv2.imread 在 Windows 下失败
        template = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if template is None:
            self._log(f"模板文件无法解码: {path}", "error")
            return None

        shot_bytes = device.screencap_png(self.adb, self.serial)
        screen = cv2.imdecode(np.frombuffer(shot_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if screen is None:
            self._log("截屏数据无法解码", "error")
            return None

        region, offset_x, offset_y = self._resolve_roi(screen, step.get("roi"))
        if region is None:
            return None
        height, width = template.shape[:2]
        if region.shape[0] < height or region.shape[1] < width:
            self._log("模板尺寸大于搜索区域，无法匹配", "warn")
            return None

        result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, max_value, _, max_loc = cv2.minMaxLoc(result)
        threshold = float(step.get("threshold", 0.85) or 0.85)
        if max_value < threshold:
            self._log(f"找图未命中：最高相似度 {max_value:.3f} < 阈值 {threshold:.2f}", "info")
            return None
        center_x = offset_x + max_loc[0] + width // 2
        center_y = offset_y + max_loc[1] + height // 2
        self._log(f"找图命中（相似度 {max_value:.3f}）→ ({center_x}, {center_y})", "ok")
        return center_x, center_y

    def _resolve_roi(self, screen, roi) -> tuple[object, int, int]:
        """计算模板匹配的搜索区域（整屏或 ROI）。

        :param screen: OpenCV 图像（BGR ndarray）。
        :param roi: 形如 [x, y, 宽, 高] 的区域；为空或非法时使用整屏。
        :return: (搜索区域图像, 区域左上角 X 偏移, 区域左上角 Y 偏移)；
                 区域非法时返回 (None, 0, 0)。
        """
        if not roi or not isinstance(roi, (list, tuple)) or len(roi) != 4:
            return screen, 0, 0
        try:
            x, y, w, h = (int(v) for v in roi)
        except (TypeError, ValueError):
            return screen, 0, 0
        if w <= 0 or h <= 0:
            return screen, 0, 0
        limit_y, limit_x = screen.shape[:2]
        x = max(0, x)
        y = max(0, y)
        w = min(w, limit_x - x)
        h = min(h, limit_y - y)
        if w <= 0 or h <= 0:
            self._log("ROI 区域超出屏幕范围，已回退为整屏搜索", "warn")
            return screen, 0, 0
        return screen[y:y + h, x:x + w], x, y

    def capture_template(self, target: str) -> str:
        """抓取当前屏幕并保存为找图模板（供 GUI「保存当前截屏为模板」使用）。

        :param target: 模板文件名或路径（相对路径按 templates/ 解析）。
        :return: 实际写入的文件路径。
        :raises RuntimeError: 截屏失败时抛出。
        """
        path = storage.resolve_template(target)
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            device.ensure_dir(folder)
        device.save_screencap(self.adb, self.serial, path)
        return path

    # ---------------- 日志 ----------------

    def _log(self, text: str, level: str = "info") -> None:
        """输出日志（转交 on_log 回调）。

        :param text: 日志正文。
        :param level: 级别 info / warn / error / ok。
        :return: 无返回值。
        """
        if self.on_log:
            self.on_log(text, level)
