# -*- coding: utf-8 -*-
"""AutoClick 原子动作层：把「点击 / 长按 / 滑动 / 输入文本 / 按键 / 启动应用 / 等待」
封装为可复用的方法，全部经由 `core.device.ShellChannel` 下发到手机端执行，
并等待回显标记，保证「命令执行完才算一步结束」。
"""
from __future__ import annotations

import time

from core.device import ShellChannel


def shell_quote(value: str) -> str:
    """把文本安全地包装为 shell 单引号字符串（内部单引号按 POSIX 规则转义）。

    :param value: 原始文本。
    :return: 可直接拼接到 shell 命令中的单引号字符串。
    """
    return "'" + value.replace("'", "'\\''") + "'"


class Actions:
    """原子动作集合：绑定一条持久 shell 通道，向手机端注入各类输入事件。

    所有动作方法返回 (耗时毫秒, 状态, 输出行)，状态为 "OK" 表示手机端已执行完毕，
    其他状态（EOF / TIMEOUT）表示通道异常，调用方应据此判定步骤失败。
    """

    def __init__(self, channel: ShellChannel) -> None:
        """初始化动作集合。

        :param channel: 已建立并握手成功的持久 shell 通道。
        """
        self.channel = channel

    def tap(self, x: int, y: int) -> tuple[float, str, list[str]]:
        """单击指定屏幕坐标。

        :param x: 屏幕 X 坐标（像素）。
        :param y: 屏幕 Y 坐标（像素）。
        :return: (耗时毫秒, 状态, 输出行)。
        """
        return self.channel.command(f"input tap {int(x)} {int(y)}")

    def long_press(self, x: int, y: int, ms: int = 800) -> tuple[float, str, list[str]]:
        """在指定坐标长按（用同点滑动模拟按压时长）。

        :param x: 屏幕 X 坐标（像素）。
        :param y: 屏幕 Y 坐标（像素）。
        :param ms: 按压时长（毫秒）。
        :return: (耗时毫秒, 状态, 输出行)。
        """
        ms = max(1, int(ms))
        return self.channel.command(f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {ms}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> tuple[float, str, list[str]]:
        """从起点滑动到终点。

        :param x1: 起点 X 坐标（像素）。
        :param y1: 起点 Y 坐标（像素）。
        :param x2: 终点 X 坐标（像素）。
        :param y2: 终点 Y 坐标（像素）。
        :param ms: 滑动持续时长（毫秒）。
        :return: (耗时毫秒, 状态, 输出行)。
        """
        ms = max(1, int(ms))
        return self.channel.command(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {ms}"
        )

    def text(self, value: str) -> tuple[float, str, list[str]]:
        """向当前焦点输入框输入文本。

        Android 的 `input text` 不直接支持空格，此处按惯例把空格替换为 `%s`，
        并对单引号做转义，避免破坏 shell 命令结构。

        :param value: 要输入的文本。
        :return: (耗时毫秒, 状态, 输出行)。
        """
        payload = shell_quote(value.replace(" ", "%s"))
        return self.channel.command(f"input text {payload}")

    def keyevent(self, key: str) -> tuple[float, str, list[str]]:
        """发送按键事件。

        :param key: 按键名（如 KEYCODE_HOME / KEYCODE_BACK）或数字键码。
        :return: (耗时毫秒, 状态, 输出行)。
        """
        return self.channel.command(f"input keyevent {key.strip()}")

    def start_app(self, target: str) -> tuple[float, str, list[str]]:
        """启动应用。

        :param target: 含 "/" 时按组件名处理（am start -n，如 com.a/.MainActivity）；
                       否则按包名处理（用 monkey 拉起该包的主界面）。
        :return: (耗时毫秒, 状态, 输出行)。
        """
        target = target.strip()
        if "/" in target:
            return self.channel.command(f"am start -n {target}")
        return self.channel.command(
            f"monkey -p {target} -c android.intent.category.LAUNCHER 1"
        )

    def sleep(self, seconds: float) -> None:
        """在 PC 侧等待指定秒数（不占用手机端资源）。

        :param seconds: 等待秒数。
        :return: 无返回值。
        """
        if seconds > 0:
            time.sleep(seconds)
