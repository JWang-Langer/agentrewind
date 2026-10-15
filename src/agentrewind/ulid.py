# Copyright 2026 WZMLab
# SPDX-License-Identifier: Apache-2.0
"""ULID —— 时间有序的 128 位标识符（Crockford Base32，26 字符）。

为什么不用自增/业务号：主键需要**单调有序**（便于按时间扫描）且**不泄露业务信息**。
实现遵循 ulid/spec：48 位毫秒时间戳 + 80 位随机数。
"""

from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TIME_LEN = 10
_RAND_LEN = 16


def _encode(value: int, length: int) -> str:
    out = ["0"] * length
    for i in range(length - 1, -1, -1):
        out[i] = _CROCKFORD[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_ulid(timestamp_ms: int | None = None) -> str:
    """生成一个 ULID 字符串。"""
    ts = int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms)
    if ts < 0 or ts >= 1 << 48:
        raise ValueError("timestamp out of 48-bit range")
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(ts, _TIME_LEN) + _encode(randomness, _RAND_LEN)


def ulid_timestamp_ms(ulid: str) -> int:
    """从 ULID 还原毫秒时间戳（用于按时间排序/断言）。"""
    if len(ulid) != _TIME_LEN + _RAND_LEN:
        raise ValueError(f"invalid ulid length: {len(ulid)}")
    value = 0
    for ch in ulid[:_TIME_LEN]:
        value = (value << 5) | _CROCKFORD.index(ch.upper())
    return value
