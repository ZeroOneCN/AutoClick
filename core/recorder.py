# -*- coding: utf-8 -*-
"""AutoClick 录制器：监听手机真实手指触摸，把手势识别为可回放的流程步骤。

识别规则（见 DESIGN 4.4）：
- 位移小于阈值（默认 20px）判为 **tap**（点击）；
- 位移大于阈值判为 **swipe**（滑动），轨迹用 Douglas-Peucker 抽稀，保留 2~4 个关键点，
  关键点之间依次生成滑动步骤（曲线滑动会得到多段直线滑动）；
- 相邻手势的 PC 侧时间差作为**相对延时**，在下一步之前插入 sleep 步骤。

一期已验证：`getevent` 只捕获真实手指触摸，程序注入的点击不会出现在该数据流中，
因此录制过程不会记录到自己回放的点击（不会自激）。
"""
from __future__ import annotations

import math
import threading
import time
from typing import Callable, Optional

from core.device import TouchStream

Point = tuple[float, float, float]  # (x, y, 时间戳)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """计算两点之间的欧氏距离。

    :param a: 点 A (x, y)。
    :param b: 点 B (x, y)。
    :return: 距离（像素）。
    """
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _perpendicular_distance(point: tuple[float, float],
                            start: tuple[float, float],
                            end: tuple[float, float]) -> float:
    """计算点到直线 start→end 的垂直距离（用于轨迹抽稀）。

    :param point: 待测点 (x, y)。
    :param start: 直线起点。
    :param end: 直线终点。
    :return: 垂直距离。
    """
    if start == end:
        return _distance(point, start)
    numerator = abs((end[0] - start[0]) * (start[1] - point[1])
                    - (start[0] - point[0]) * (end[1] - start[1]))
    denominator = math.hypot(end[0] - start[0], end[1] - start[1])
    return numerator / denominator if denominator else 0.0


def simplify_points(points: list[tuple[float, float]], tolerance: float = 8.0) -> list[tuple[float, float]]:
    """用 Douglas-Peucker 算法抽稀轨迹点。

    :param points: 轨迹点列表 [(x, y), ...]。
    :param tolerance: 抽稀容差（像素），越大保留的关键点越少。
    :return: 抽稀后的关键点列表（至少保留首尾两点）。
    """
    if len(points) < 3:
        return list(points)

    def _dp(segment: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(segment) < 3:
            return segment
        start, end = segment[0], segment[-1]
        worst_distance, worst_index = 0.0, 0
        for index in range(1, len(segment) - 1):
            distance = _perpendicular_distance(segment[index], start, end)
            if distance > worst_distance:
                worst_distance, worst_index = distance, index
        if worst_distance > tolerance:
            left = _dp(segment[:worst_index + 1])
            right = _dp(segment[worst_index:])
            return left[:-1] + right
        return [start, end]

    return _dp(list(points))


class Recorder:
    """录制器：把手机上的真实触摸手势转换为流程步骤（tap / swipe / sleep）。

    步骤通过 `on_step` 回调实时抛出（运行在监听线程），同时也保存在内部列表中，
    调用方可在停止后通过 `steps()` 一次性取回全部结果。
    """

    def __init__(self, adb: str, serial: str, width: int, height: int,
                 on_step: Optional[Callable[[dict], None]] = None,
                 on_log: Optional[Callable[[str, str], None]] = None,
                 move_threshold: float = 20.0, tolerance: float = 8.0,
                 gap_threshold: float = 0.05) -> None:
        """初始化录制器（此时尚未启动监听）。

        :param adb: adb 可执行文件路径。
        :param serial: 目标设备序列号。
        :param width: 屏幕宽度像素。
        :param height: 屏幕高度像素。
        :param on_step: 每生成一个步骤时的回调，签名 (step)。
        :param on_log: 日志回调，签名 (文本, 级别)。
        :param move_threshold: 判定滑动的最小位移（像素）。
        :param tolerance: 轨迹抽稀容差（像素）。
        :param gap_threshold: 生成 sleep 步骤的最小时间间隔（秒）。
        """
        self.on_step = on_step
        self.on_log = on_log
        self.move_threshold = move_threshold
        self.tolerance = tolerance
        self.gap_threshold = gap_threshold

        self._lock = threading.Lock()
        self._steps: list[dict] = []
        self._points: list[Point] = []
        self._last_end_ts: Optional[float] = None
        self._stream = TouchStream(adb, serial, width, height, on_event=self._on_event)

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        """探测触摸设备并开始监听。

        :return: 无返回值。
        :raises RuntimeError: 触摸设备探测失败时抛出。
        """
        node, x_max, y_max = self._stream.probe()
        self._stream.start()
        self._log(f"录制已开始（触摸设备 {node}，原始范围 {x_max}x{y_max}）。"
                  f"请直接在手机上操作，识别出的步骤会实时出现。")

    def stop(self) -> list[dict]:
        """停止监听并返回录制的全部步骤。

        :return: 步骤列表（tap / swipe / sleep 的字典列表）。
        """
        self._flush_pending()
        self._stream.stop()
        error = self._stream.error()
        if error:
            self._log(f"录制中断：{error}", "warn")
        with self._lock:
            return [dict(step) for step in self._steps]

    def steps(self) -> list[dict]:
        """取当前已录制步骤的快照。

        :return: 步骤列表副本。
        """
        with self._lock:
            return [dict(step) for step in self._steps]

    def to_flow(self, name: str = "录制流程", screen: tuple[int, int] = (0, 0)) -> dict:
        """把录制结果包装成流程字典。

        :param name: 流程名称。
        :param screen: 录制时的屏幕分辨率 (宽, 高)。
        :return: 流程字典（结构同 core.flow.new_flow）。
        """
        return {
            "name": name,
            "screen": {"width": screen[0], "height": screen[1]},
            "steps": self.steps(),
            "control": {"repeat": 1, "stop_on_error": True},
        }

    # ---------------- 手势处理 ----------------

    def _on_event(self, kind: str, x: int, y: int, ts: float) -> None:
        """触摸流回调（监听线程）：累积轨迹并在手指抬起时生成步骤。

        :param kind: down / move / up。
        :param x: 屏幕 X 坐标。
        :param y: 屏幕 Y 坐标。
        :param ts: PC 侧时间戳。
        :return: 无返回值。
        """
        if kind == "down":
            self._points = [(float(x), float(y), ts)]
        elif kind == "move":
            self._points.append((float(x), float(y), ts))
        elif kind == "up":
            if not self._points:
                self._points = [(float(x), float(y), ts)]
            elif (self._points[-1][0], self._points[-1][1]) != (float(x), float(y)):
                self._points.append((float(x), float(y), ts))
            self._finish_gesture()

    def _flush_pending(self) -> None:
        """停止前把尚未结束的手势（如手指仍按在屏幕上）也转换掉。

        :return: 无返回值。
        """
        if len(self._points) >= 2:
            self._finish_gesture()

    def _finish_gesture(self) -> None:
        """结束当前手势：判断点击/滑动、生成步骤并处理相对延时。

        :return: 无返回值。
        """
        points = self._points
        self._points = []
        if not points:
            return

        first, last = points[0], points[-1]
        start_xy = (first[0], first[1])
        end_xy = (last[0], last[1])
        displacement = _distance(start_xy, end_xy)
        duration_ms = max(1, int(round((last[2] - first[2]) * 1000)))

        # 相对延时：本次手势开始 - 上次手势结束
        if self._last_end_ts is not None:
            gap = first[2] - self._last_end_ts
            if gap > self.gap_threshold:
                self._emit({"type": "sleep", "seconds": round(gap, 2)})
        self._last_end_ts = last[2]

        if displacement < self.move_threshold:
            self._emit({"type": "tap", "x": int(round(end_xy[0])), "y": int(round(end_xy[1])),
                        "interval": 0.0, "count": 1})
            return

        key_points = simplify_points([(p[0], p[1]) for p in points], self.tolerance)
        if len(key_points) < 2:
            key_points = [start_xy, end_xy]
        # 每段滑动的时长按时长比例分配（最少 100ms）
        segments = len(key_points) - 1
        per_segment_ms = max(100, int(round(duration_ms / segments)))
        for index in range(segments):
            src, dst = key_points[index], key_points[index + 1]
            self._emit({
                "type": "swipe",
                "from": [int(round(src[0])), int(round(src[1]))],
                "to": [int(round(dst[0])), int(round(dst[1]))],
                "duration": per_segment_ms,
            })

    def _emit(self, step: dict) -> None:
        """记录并回调一个步骤（回调异常不影响录制）。

        :param step: 步骤字典。
        :return: 无返回值。
        """
        with self._lock:
            self._steps.append(step)
        if self.on_step:
            try:
                self.on_step(dict(step))
            except Exception:
                pass

    def _log(self, text: str, level: str = "info") -> None:
        """输出日志（转交给 on_log 回调）。

        :param text: 日志正文。
        :param level: 级别。
        :return: 无返回值。
        """
        if self.on_log:
            self.on_log(text, level)
