# -*- coding: utf-8 -*-
"""AutoClick 网络对时（SNTP）：向阿里云 NTP 服务器取标准时间，算出本机时钟偏差。

「定时执行」是以本机时钟为基准等待的，若本机时钟本身不准，定时再准也会整体提前或
延后（Windows 默认 w32time 同步周期约 7 天，精度仅百毫秒级，休眠/漂移后偏差可达秒级）。
本模块用标准库 socket 实现最简 SNTP 客户端，不需要任何第三方依赖，供界面把「标准时间
− 本机时钟」的偏差加到定时基准上。

设计要点：
- **只读时间、不改系统时钟**：偏差交给调用方自己使用，因此无需管理员权限，也不影响
  系统里其它软件的时间基准；
- **多次采样取「往返延迟最小」的一次**：往返越小，抖动越小，算出的偏差越可信；
- **应答校验**：模式必须为服务器应答(4)、stratum 有效且服务器已同步（LI≠3），且应答
  里的「原始时戳」必须与本机发出的时戳一致（可挡伪造/陈旧的应答），KoD 报文一律忽略。

源码运行与 PyInstaller 打包均可用（仅使用标准库）。
"""
from __future__ import annotations

import socket
import struct
import time
from typing import NamedTuple, Optional

# NTP 时戳以 1900-01-01 为纪元，Unix 以 1970-01-01 为纪元，两者相差 2208988800 秒
NTP_EPOCH_DELTA = 2208988800

NTP_PORT = 123

# 阿里云公网 NTP 服务器：首个为主用，其余为兜底（逐个轮转尝试）
ALIYUN_NTP_SERVERS = (
    "ntp.aliyun.com",
    "ntp1.aliyun.com",
    "ntp2.aliyun.com",
    "ntp3.aliyun.com",
)

# 默认单次等待应答的超时（秒）与总采样次数
DEFAULT_TIMEOUT = 0.8
DEFAULT_ATTEMPTS = 3

# 服务器未同步（闰秒指示位 = 3）时的取值
_LEAP_UNSYNCED = 3


class SyncResult(NamedTuple):
    """一次网络对时的结果。

    :字段 offset: 标准时间 − 本机时钟（秒）；正数表示本机偏慢，负数表示本机偏快。
    :字段 rtt_ms: 该次采样的往返延迟（毫秒）。
    :字段 server: 提供该结果的服务器地址。
    :字段 samples: 成功采样的总次数（用于评估可信度）。
    """

    offset: float
    rtt_ms: float
    server: str
    samples: int


def _build_request(stamp: float) -> bytes:
    """构造一个 48 字节的 SNTP 客户端请求报文。

    :param stamp: 本机当前时间（Unix 秒，可为小数），写入报文的「发送时戳」字段，
        服务器会把它原样回填到应答的「原始时戳」中，供本机校验应答真伪。
    :return: 48 字节请求报文。
    """
    whole = int(stamp)
    seconds = whole + NTP_EPOCH_DELTA
    fraction = int((stamp - whole) * (1 << 32))
    # 首字节 = LI(0) + VN(3) + Mode(3=客户端)，第 1~39 字节留空，末 8 字节为发送时戳
    return b"\x1b" + 43 * b"\x00" + struct.pack("!II", seconds, fraction)


def _to_unix(seconds: int, fraction: int) -> float:
    """把 NTP 时戳换算为 Unix 时间戳（秒，含小数）。

    :param seconds: NTP 时戳的整数秒部分（自 1900 年起算）。
    :param fraction: NTP 时戳的小数部分（2^32 分之一秒为单位的整数）。
    :return: Unix 时间戳（秒）。秒数为 0 时返回 0.0，表示该字段无效。
    """
    if seconds == 0:
        return 0.0
    return (seconds - NTP_EPOCH_DELTA) + fraction / float(1 << 32)


def query_once(server: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[tuple[float, float]]:
    """向单个 NTP 服务器发起一次查询并计算时钟偏差。

    按 RFC 5905 的计算方式：设 T1 为本机发送时刻、T2 为服务器接收时刻、T3 为服务器
    回发时刻、T4 为本机收到时刻，则
    `偏差 = ((T2 - T1) + (T3 - T4)) / 2`，`往返 = (T4 - T1) - (T3 - T2)`。

    :param server: NTP 服务器域名或 IP。
    :param timeout: 等待应答的超时秒数。
    :return: (偏差秒数, 往返毫秒数)；网络异常、应答非法或校验失败时返回 None。
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        t1 = time.time()
        sock.sendto(_build_request(t1), (server, NTP_PORT))
        data, _ = sock.recvfrom(1024)
        t4 = time.time()
    except OSError:
        return None
    finally:
        if sock is not None:
            sock.close()

    if len(data) < 48:
        return None
    mode = data[0] & 0x07
    leap = (data[0] >> 6) & 0x03
    stratum = data[1]
    # 仅接受服务器应答(4)：stratum 1~15 合法，0 为 KoD 报文，LI=3 表示服务器自身未同步
    if mode != 4 or leap == _LEAP_UNSYNCED or not (1 <= stratum <= 15):
        return None
    sent = struct.unpack("!II", _build_request(t1)[40:48])
    if struct.unpack("!II", data[24:32]) != sent:  # 原始时戳与本机发出的不一致
        return None
    t2 = _to_unix(*struct.unpack("!II", data[32:40]))
    t3 = _to_unix(*struct.unpack("!II", data[40:48]))
    if t2 <= 0 or t3 <= 0:
        return None
    offset = ((t2 - t1) + (t3 - t4)) / 2.0
    rtt = (t4 - t1) - (t3 - t2)
    if rtt < 0:  # 服务器时戳异常时退化为本机往返计时，避免出现负延迟
        rtt = t4 - t1
    return offset, rtt * 1000.0


def sync(servers: tuple[str, ...] = ALIYUN_NTP_SERVERS,
         timeout: float = DEFAULT_TIMEOUT,
         attempts: int = DEFAULT_ATTEMPTS) -> Optional[SyncResult]:
    """向阿里云 NTP 服务器对时，返回最可信的一次偏差。

    逐个轮转服务器采样 `attempts` 次，只保留成功的结果，并取「往返延迟最小」的那次
    —— 往返越小则抖动越小，偏差越接近真实值。

    :param servers: 服务器列表（按顺序轮转；可为单个服务器）。
    :param timeout: 每次查询等待应答的超时秒数。
    :param attempts: 总采样次数（每次换一个服务器）。
    :return: 对时结果；全部失败（如断网、被防火墙拦截 UDP 123）时返回 None。
    """
    if attempts < 1:
        attempts = 1
    best: Optional[tuple[float, float, str]] = None
    samples = 0
    for index in range(attempts):
        server = servers[index % len(servers)]
        result = query_once(server, timeout)
        if result is None:
            continue
        samples += 1
        offset, rtt_ms = result
        if best is None or rtt_ms < best[1]:
            best = (offset, rtt_ms, server)
    if best is None:
        return None
    return SyncResult(offset=best[0], rtt_ms=best[1], server=best[2], samples=samples)


def describe(result: SyncResult) -> str:
    """把对时结果格式化为中文描述（供日志与界面显示）。

    :param result: 对时结果。
    :return: 形如「本机时钟比标准时间慢 0.32 秒（往返 12.4 ms，ntp.aliyun.com，3 次采样）」的描述。
    """
    if result.offset >= 0:
        return (f"本机时钟比标准时间慢 {result.offset:.3f} 秒"
                f"（往返 {result.rtt_ms:.1f} ms，{result.server}，{result.samples} 次采样）")
    return (f"本机时钟比标准时间快 {abs(result.offset):.3f} 秒"
            f"（往返 {result.rtt_ms:.1f} ms，{result.server}，{result.samples} 次采样）")
