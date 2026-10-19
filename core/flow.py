# -*- coding: utf-8 -*-
"""AutoClick 流程层：步骤数据模型、参数校验与执行引擎。

流程 JSON 结构（与 DESIGN 4.3 一致）：

```json
{
  "name": "签到示例",
  "screen": {"width": 1080, "height": 2400},
  "steps": [ { "type": "tap", "x": 540, "y": 2000, "interval": 0.5, "count": 3 } ],
  "control": { "repeat": 1, "stop_on_error": true }
}
```

步骤类型：`tap / long_press / swipe / sleep / text / keyevent / start_app /
find_click / loop / endloop`。

循环的实现约定（GUI 采用扁平列表，便于拖拽排序与编辑）：
- `loop` 与 `endloop` **成对出现**，二者之间（不含标记）的步骤为循环体；
- `loop` 字段：`mode` = `count`（按次数）或 `duration`（按时长）、`times`（次数，0 表示无限）、
  `duration`（秒）；支持嵌套 loop（按配对关系递归执行）。
- 整条流程的重复由 `control.repeat` 控制（0 表示无限）。
"""
from __future__ import annotations

import time
from typing import Callable, Optional

# 支持的步骤类型（GUI 下拉与校验共用）
STEP_TYPES: list[str] = [
    "tap", "long_press", "swipe", "sleep", "text",
    "keyevent", "start_app", "find_click", "loop", "endloop",
]

# 各步骤类型的参数字段定义：(键, 界面标签, 值类型)
# 值类型为 int / float / str / choice:...
STEP_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "tap": [("x", "X 坐标", "int"), ("y", "Y 坐标", "int"),
            ("interval", "间隔(秒)", "float"), ("count", "次数(0=无限)", "int")],
    "long_press": [("x", "X 坐标", "int"), ("y", "Y 坐标", "int"),
                   ("ms", "按压(毫秒)", "int")],
    "swipe": [("x1", "起点 X", "int"), ("y1", "起点 Y", "int"),
              ("x2", "终点 X", "int"), ("y2", "终点 Y", "int"),
              ("duration", "时长(毫秒)", "int")],
    "sleep": [("seconds", "秒数", "float")],
    "text": [("value", "文本", "str")],
    "keyevent": [("key", "按键名", "str")],
    "start_app": [("target", "组件/包名", "str")],
    "find_click": [("mode", "识别方式", "choice:control|image"),
                   ("target", "目标(文本/ID/模板)", "str"),
                   ("threshold", "匹配阈值", "float"),
                   ("timeout", "超时(秒)", "float"),
                   ("on_miss", "未命中处理", "choice:skip|stop")],
    "loop": [("mode", "循环模式", "choice:count|duration"),
             ("times", "次数(0=无限)", "int"),
             ("duration", "时长(秒)", "float")],
    "endloop": [],
}

# 各步骤类型的默认值
STEP_DEFAULTS: dict[str, dict] = {
    "tap": {"x": 540, "y": 1200, "interval": 0.5, "count": 1},
    "long_press": {"x": 540, "y": 1200, "ms": 800},
    "swipe": {"x1": 540, "y1": 1800, "x2": 540, "y2": 600, "duration": 300},
    "sleep": {"seconds": 1.0},
    "text": {"value": ""},
    "keyevent": {"key": "KEYCODE_BACK"},
    "start_app": {"target": ""},
    "find_click": {"mode": "control", "target": "", "threshold": 0.85,
                   "timeout": 5.0, "on_miss": "skip"},
    "loop": {"mode": "count", "times": 1, "duration": 0.0},
    "endloop": {},
}

STEP_LABELS: dict[str, str] = {
    "tap": "点击", "long_press": "长按", "swipe": "滑动", "sleep": "等待",
    "text": "输入文本", "keyevent": "按键", "start_app": "启动应用",
    "find_click": "找图/找控件点击", "loop": "循环开始", "endloop": "循环结束",
}


def new_step(step_type: str) -> dict:
    """创建一个带默认参数的步骤字典。

    :param step_type: 步骤类型（须属于 STEP_TYPES）。
    :return: 新的步骤字典（含 type 字段）。
    :raises ValueError: 类型不支持时抛出。
    """
    if step_type not in STEP_DEFAULTS:
        raise ValueError(f"不支持的步骤类型: {step_type}")
    step = {"type": step_type}
    step.update(STEP_DEFAULTS[step_type])
    return step


def step_summary(step: dict) -> str:
    """生成步骤的一行摘要文本（用于 GUI 列表展示）。

    :param step: 步骤字典。
    :return: 摘要字符串。
    """
    kind = step.get("type", "?")
    if kind == "tap":
        return f"({step.get('x')}, {step.get('y')}) 次数={step.get('count')} 间隔={step.get('interval')}s"
    if kind == "long_press":
        return f"({step.get('x')}, {step.get('y')}) 按压 {step.get('ms')}ms"
    if kind == "swipe":
        src = step.get("from") or [step.get("x1"), step.get("y1")]
        dst = step.get("to") or [step.get("x2"), step.get("y2")]
        return f"{tuple(src)} → {tuple(dst)} 时长 {step.get('duration')}ms"
    if kind == "sleep":
        return f"等待 {step.get('seconds')}s"
    if kind == "text":
        return f"输入 '{step.get('value')}'"
    if kind == "keyevent":
        return f"按键 {step.get('key')}"
    if kind == "start_app":
        return f"启动 {step.get('target')}"
    if kind == "find_click":
        return f"{step.get('mode')} 目标={step.get('target')} 阈值={step.get('threshold')} 超时={step.get('timeout')}s"
    if kind == "loop":
        if step.get("mode") == "duration":
            return f"循环 {step.get('duration')}s"
        times = step.get("times")
        return "循环 无限" if not times else f"循环 {times} 次"
    if kind == "endloop":
        return "循环体结束"
    return ""


def new_flow(name: str = "未命名流程", screen: tuple[int, int] = (0, 0)) -> dict:
    """创建一条空流程。

    :param name: 流程名称。
    :param screen: 制作该流程时的屏幕分辨率 (宽, 高)，用于跨设备加载校验。
    :return: 流程字典。
    """
    return {
        "name": name,
        "screen": {"width": screen[0], "height": screen[1]},
        "steps": [],
        "control": {"repeat": 1, "stop_on_error": True},
    }


def validate_flow(flow: dict, screen: tuple[int, int] = (0, 0)) -> list[str]:
    """校验流程结构，返回全部问题描述（空列表表示通过）。

    :param flow: 流程字典。
    :param screen: 当前设备分辨率 (宽, 高)；非零时用于坐标越界检查。
    :return: 问题描述列表。
    """
    problems: list[str] = []
    steps = flow.get("steps")
    if not isinstance(steps, list):
        return ["流程缺少 steps 列表"]
    if not steps:
        problems.append("流程没有任何步骤")
    loop_depth = 0
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            problems.append(f"第 {index} 步不是对象")
            continue
        kind = step.get("type")
        if kind not in STEP_TYPES:
            problems.append(f"第 {index} 步类型不支持: {kind}")
            continue
        if kind == "loop":
            loop_depth += 1
        elif kind == "endloop":
            loop_depth -= 1
            if loop_depth < 0:
                problems.append(f"第 {index} 步 endloop 缺少配对的 loop")
                loop_depth = 0
        if screen[0]:
            width, height = screen
            for key_x, key_y in (("x", "y"), ("x1", "y1"), ("x2", "y2")):
                if key_x in step and key_y in step:
                    x, y = step.get(key_x, 0), step.get(key_y, 0)
                    if not (0 <= x < width and 0 <= y < height):
                        problems.append(f"第 {index} 步坐标 ({x}, {y}) 超出屏幕 {width}x{height}")
    if loop_depth != 0:
        problems.append("存在未闭合的 loop（缺少 endloop）")
    return problems


class FlowRunner:
    """流程执行引擎：顺序执行步骤，支持循环、重试与随时停止。

    运行在后台线程中；日志通过 `on_log` 回调抛出，停止通过 `stop_event` 控制。
    界面层负责把这两者桥接到自己的消息队列上。
    """

    def __init__(self, flow: dict, actions, on_log: Optional[Callable[[str, str], None]] = None,
                 stop_event=None, vision=None, begin: int = 0) -> None:
        """初始化执行引擎。

        :param flow: 流程字典（见模块文档）。
        :param actions: `core.actions.Actions` 实例（提供 tap/swipe 等原子动作）。
        :param on_log: 日志回调，签名 (文本, 级别)；级别为 info/warn/error/ok。
        :param stop_event: `threading.Event`；被 set 时尽快停止执行。
        :param vision: 识别器实例（提供 locate 方法），find_click 步骤需要；可为 None。
        :param begin: 起始步骤下标（从 0 开始）；大于 0 时表示「从中间某步开始调试」，
            只执行该步到末尾的一轮，忽略流程的整体重复设置。
        """
        self.flow = flow
        self.actions = actions
        self.on_log = on_log
        self.stop_event = stop_event
        self.vision = vision
        control = flow.get("control") or {}
        self.repeat = int(control.get("repeat", 1) or 0)
        self.stop_on_error = bool(control.get("stop_on_error", True))
        self.steps: list[dict] = [s for s in (flow.get("steps") or []) if isinstance(s, dict)]
        self.begin = max(0, min(int(begin), len(self.steps)))  # 越界自动收敛到区间内

    # ---------------- 对外入口 ----------------

    def run(self) -> tuple[bool, str]:
        """执行整条流程（含 control.repeat 决定的整体重复）。

        若构造时指定了 `begin > 0`（从中间某步开始调试），则只执行
        [begin, 末尾) 这一轮，并忽略流程的整体重复设置，避免"调试一段却重复跑整条"。

        :return: (是否成功执行完毕, 结束原因描述)。
        """
        result = True
        reason = "流程执行完成"
        round_index = 0
        if self.begin:
            self.repeat = 1
            self._log(f"从第 {self.begin + 1} 步开始执行（仅执行到末尾一轮，忽略整体重复）", "warn")
        while True:
            if self._stopped():
                result, reason = False, "用户停止"
                break
            if self.repeat and round_index >= self.repeat:
                break
            round_index += 1
            if self.repeat != 1:
                self._log(f"— 开始第 {round_index} 轮" + ("（无限循环）" if not self.repeat else ""))
            # 第一轮从 begin 起（调试用），后续轮次按整体重复从第 1 步重新开始
            start = self.begin if round_index == 1 else 0
            if not self._run_range(start, len(self.steps)):
                if self._stopped():
                    result, reason = False, "用户停止"
                else:
                    result, reason = False, "步骤执行失败"
                break
        return result, reason

    def run_single(self, index: int) -> tuple[bool, str]:
        """只执行指定下标的单个步骤（试跑调试用）。

        结构性步骤（loop / endloop）不产生实际动作，直接返回失败并说明原因，
        避免试跑时给出"执行成功"的误导结论。

        :param index: 步骤下标（从 0 开始）。
        :return: (是否成功, 结束原因描述)。
        """
        if not (0 <= index < len(self.steps)):
            return False, "步骤下标越界"
        step = self.steps[index]
        kind = step.get("type")
        if kind in ("loop", "endloop"):
            return False, f"第 {index + 1} 步是结构性步骤（{kind}），不能单独试跑"
        if self._stopped():
            return False, "用户停止"
        number = index + 1
        self._log(f"试跑第 {number} 步：{step_summary(step)}", "warn")
        ok, message = self._exec_step(number, step)
        if message:
            self._log(message, "info" if ok else "warn")
        return (True, "试跑完成") if ok else (False, f"第 {number} 步执行失败")

    # ---------------- 执行细节 ----------------

    def _run_range(self, begin: int, end: int) -> bool:
        """执行 [begin, end) 区间内的步骤（处理 loop / endloop 配对）。

        :param begin: 起始下标（含）。
        :param end: 结束下标（不含）。
        :return: 顺利执行完毕返回 True；失败或停止返回 False。
        """
        index = begin
        while index < end:
            if self._stopped():
                return False
            step = self.steps[index]
            kind = step.get("type")
            if kind == "loop":
                match = self._match_endloop(index, end)
                if match is None:
                    self._log(f"第 {index + 1} 步 loop 缺少配对的 endloop，已跳过该段", "error")
                    index += 1
                    continue
                if not self._run_loop(step, index + 1, match):
                    return False
                index = match + 1
                continue
            if kind == "endloop":
                index += 1
                continue
            ok, message = self._exec_step(index + 1, step)
            if message:
                self._log(message, "info" if ok else "warn")
            if not ok:
                if self.stop_on_error:
                    self._log(f"第 {index + 1} 步失败，按「出错即停止」中断流程", "error")
                    return False
                self._log(f"第 {index + 1} 步失败，按设置跳过并继续", "warn")
            index += 1
        return True

    def _match_endloop(self, start: int, end: int) -> Optional[int]:
        """查找与 start 处 loop 配对的 endloop 下标（支持嵌套）。

        :param start: loop 所在下标。
        :param end: 搜索区间上界（不含）。
        :return: 配对 endloop 的下标；未找到返回 None。
        """
        depth = 0
        for index in range(start + 1, end):
            kind = self.steps[index].get("type")
            if kind == "loop":
                depth += 1
            elif kind == "endloop":
                if depth == 0:
                    return index
                depth -= 1
        return None

    def _run_loop(self, step: dict, begin: int, end: int) -> bool:
        """执行一个循环块。

        :param step: loop 步骤字典。
        :param begin: 循环体起始下标（含）。
        :param end: 循环体结束下标（不含，即 endloop 位置）。
        :return: 顺利执行完毕返回 True；失败或停止返回 False。
        """
        mode = step.get("mode", "count")
        times = int(step.get("times", 1) or 0)
        duration = float(step.get("duration", 0) or 0)
        if mode == "duration" and duration > 0:
            deadline = time.perf_counter() + duration
            while time.perf_counter() < deadline:
                if self._stopped():
                    return False
                if not self._run_range(begin, end):
                    return False
            return True
        if not times:
            self._log("循环次数为 0，按「无限循环」执行（点「停止」结束）", "warn")
        done = 0
        while not times or done < times:
            if self._stopped():
                return False
            if not self._run_range(begin, end):
                return False
            done += 1
        return True

    def _exec_step(self, number: int, step: dict) -> tuple[bool, str]:
        """执行单个步骤（含重试）。

        :param number: 步骤序号（从 1 开始，用于日志）。
        :param step: 步骤字典。
        :return: (是否成功, 日志文本)。
        """
        retry = int(step.get("retry", 0) or 0)
        for attempt in range(retry + 1):
            if self._stopped():
                return False, "已停止"
            ok, message = self._do_step(step)
            if ok:
                return True, f"[{number}] {message}"
            if attempt < retry:
                self._log(f"[{number}] 失败：{message}，重试 {attempt + 1}/{retry}", "warn")
                if self._sleep(0.5):
                    return False, "已停止"
        return False, f"[{number}] {message}"

    def _do_step(self, step: dict) -> tuple[bool, str]:
        """按类型执行一步具体动作。

        :param step: 步骤字典。
        :return: (是否成功, 描述文本)。
        """
        kind = step.get("type")
        if kind == "tap":
            return self._do_tap(step)
        if kind == "long_press":
            _, status, _ = self.actions.long_press(
                int(step.get("x", 0)), int(step.get("y", 0)), int(step.get("ms", 800)))
            return status == "OK", f"长按 ({step.get('x')}, {step.get('y')}) {step.get('ms')}ms"
        if kind == "swipe":
            src = step.get("from") or [step.get("x1", 0), step.get("y1", 0)]
            dst = step.get("to") or [step.get("x2", 0), step.get("y2", 0)]
            _, status, _ = self.actions.swipe(
                int(src[0]), int(src[1]), int(dst[0]), int(dst[1]), int(step.get("duration", 300)))
            return status == "OK", f"滑动 {tuple(src)} → {tuple(dst)}"
        if kind == "sleep":
            seconds = float(step.get("seconds", 0) or 0)
            self._log(f"等待 {seconds}s…")
            if self._sleep(seconds):
                return False, "已停止"
            return True, f"等待 {seconds}s 完成"
        if kind == "text":
            _, status, _ = self.actions.text(str(step.get("value", "")))
            return status == "OK", f"输入文本 '{step.get('value')}'"
        if kind == "keyevent":
            _, status, _ = self.actions.keyevent(str(step.get("key", "")))
            return status == "OK", f"按键 {step.get('key')}"
        if kind == "start_app":
            _, status, _ = self.actions.start_app(str(step.get("target", "")))
            return status == "OK", f"启动 {step.get('target')}"
        if kind == "find_click":
            return self._do_find_click(step)
        return True, f"未知类型 {kind}（已跳过）"

    def _do_tap(self, step: dict) -> tuple[bool, str]:
        """执行点击步骤（支持次数与间隔，次数 0 表示无限直到停止）。

        :param step: tap 步骤字典。
        :return: (是否成功, 描述文本)。
        """
        x, y = int(step.get("x", 0)), int(step.get("y", 0))
        count = int(step.get("count", 1) or 0)
        interval = float(step.get("interval", 0) or 0)
        done = 0
        while not count or done < count:
            if self._stopped():
                return False, f"点击 ({x}, {y}) 已停止（已完成 {done} 次）"
            _, status, lines = self.actions.tap(x, y)
            if status != "OK":
                return False, f"点击通道异常({status})"
            if lines:
                self._log(f"  手机端: {lines[:3]}", "warn")
            done += 1
            if interval > 0 and self._sleep(interval):
                return False, f"点击已停止（已完成 {done} 次）"
        return True, f"点击 ({x}, {y}) × {done} 次"

    def _do_find_click(self, step: dict) -> tuple[bool, str]:
        """执行找控件/找图点击步骤（依赖识别器 vision）。

        :param step: find_click 步骤字典。
        :return: (是否成功, 描述文本)。
        """
        mode = step.get("mode", "control")
        target = str(step.get("target", ""))
        if self.vision is None:
            return False, f"识别层不可用，无法执行 {mode} 定位（目标 {target}）"
        timeout = float(step.get("timeout", 5.0) or 5.0)
        try:
            point = self.vision.locate(step, timeout, self.stop_event)
        except Exception as exc:
            return False, f"识别异常: {type(exc).__name__}: {exc}"
        if point is None:
            on_miss = step.get("on_miss", "skip")
            if on_miss == "skip":
                return True, f"未找到目标 {target}（已跳过）"
            return False, f"未找到目标 {target}（超时 {timeout}s）"
        x, y = point
        _, status, _ = self.actions.tap(x, y)
        return status == "OK", f"命中 {target} → 点击 ({x}, {y})"

    def _sleep(self, seconds: float) -> bool:
        """可被停止打断的等待。

        :param seconds: 等待秒数。
        :return: 若在等待期间收到停止请求返回 True，否则 False。
        """
        if seconds <= 0:
            return False
        if self.stop_event is None:
            time.sleep(seconds)
            return False
        return self.stop_event.wait(seconds)

    def _stopped(self) -> bool:
        """判断是否收到停止请求。

        :return: 需要停止返回 True。
        """
        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _log(self, text: str, level: str = "info") -> None:
        """输出日志（转交给 on_log 回调）。

        :param text: 日志正文。
        :param level: 级别。
        :return: 无返回值。
        """
        if self.on_log:
            self.on_log(text, level)
