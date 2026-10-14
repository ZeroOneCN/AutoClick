# -*- coding: utf-8 -*-
"""AutoClick GUI 入口（唯一入口）。

职责：创建 tkinter 主窗口并启动主循环。业务逻辑位于 core/，界面实现位于 gui/app.py。
打包（PyInstaller）后本文件所在目录不再是磁盘路径，因此这里统一按
`sys._MEIPASS`（打包）或文件所在目录（源码运行）补入 sys.path。
"""
from __future__ import annotations

import os
import sys


def _bootstrap_path() -> None:
    """把项目根目录（或打包解包目录）加入 sys.path，保证能 import core / gui。

    :return: 无返回值。
    """
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    if base and base not in sys.path:
        sys.path.insert(0, base)


def main() -> int:
    """程序入口：创建主窗口、启动界面主循环。

    :return: 进程退出码，0 表示正常结束。
    """
    _bootstrap_path()
    import tkinter as tk  # noqa: PLC0415

    from gui.app import AutoClickApp  # noqa: PLC0415

    root = tk.Tk()
    AutoClickApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
