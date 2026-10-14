# -*- coding: utf-8 -*-
"""AutoClick 存储层：流程（*.json）与找图模板目录的读写与定位。

设计约定（见 DESIGN 4.7）：
- `flows/`（流程 JSON）与 `templates/`（找图模板 PNG）放在**用户数据目录**
  （打包后为 EXE 同级目录），首次使用时自动创建，保证可写、可备份、升级不丢数据；
- 文件统一使用 UTF-8 编码与 `ensure_ascii=False`，避免中文乱码。
"""
from __future__ import annotations

import json
import os

from core.device import app_dir, ensure_dir

FLOW_SUFFIX = ".json"


def flows_dir() -> str:
    """返回流程文件目录（必要时创建）。

    :return: 流程目录绝对路径。
    """
    return ensure_dir(os.path.join(app_dir(), "flows"))


def templates_dir() -> str:
    """返回找图模板目录（必要时创建）。

    :return: 模板目录绝对路径。
    """
    return ensure_dir(os.path.join(app_dir(), "templates"))


def resolve_template(target: str) -> str:
    """把模板路径解析为绝对路径（相对路径按 templates/ 目录解析）。

    :param target: 用户填写的模板文件名或路径。
    :return: 解析后的绝对路径。
    """
    if os.path.isabs(target):
        return target
    return os.path.join(templates_dir(), target)


def list_flow_files() -> list[str]:
    """列出流程目录下所有 JSON 文件的绝对路径（按文件名排序）。

    :return: 流程文件路径列表。
    """
    folder = flows_dir()
    names = sorted(n for n in os.listdir(folder) if n.lower().endswith(FLOW_SUFFIX))
    return [os.path.join(folder, n) for n in names]


def load_flow(path: str) -> dict:
    """读取流程 JSON 文件。

    :param path: 流程文件路径。
    :return: 流程字典。
    :raises FileNotFoundError: 文件不存在时抛出。
    :raises ValueError: JSON 内容非法或结构不是对象时抛出。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"流程文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as fp:
        try:
            data = json.load(fp)
        except json.JSONDecodeError as exc:
            raise ValueError(f"流程文件不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("流程文件的顶层结构必须是 JSON 对象")
    return data


def save_flow(path: str, flow: dict) -> str:
    """把流程字典写入 JSON 文件（UTF-8、缩进 2、保留中文）。

    :param path: 目标文件路径。
    :param flow: 流程字典。
    :return: 实际写入的文件路径。
    """
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        ensure_dir(folder)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(flow, fp, ensure_ascii=False, indent=2)
    return path


def suggest_flow_name(base: str = "流程") -> str:
    """在流程目录中生成一个不重复的文件名（用于新建流程）。

    :param base: 名称前缀。
    :return: 形如 `流程1.json` 的绝对路径（若重名则序号递增）。
    """
    folder = flows_dir()
    index = 1
    while True:
        candidate = os.path.join(folder, f"{base}{index}{FLOW_SUFFIX}")
        if not os.path.exists(candidate):
            return candidate
        index += 1
