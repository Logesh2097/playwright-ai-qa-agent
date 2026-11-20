"""Identifier helpers."""

import os
import time
import uuid


def new_request_id() -> str:
    return uuid.uuid4().hex


def uuid7() -> uuid.UUID:
    """Time-ordered UUID (RFC 9562, version 7).

    48 bits of Unix time in milliseconds followed by random bits, so primary
    keys sort by creation time and stay index-friendly.
    """
    timestamp_ms = time.time_ns() // 1_000_000
    value = (timestamp_ms & 0xFFFF_FFFF_FFFF) << 80 | int.from_bytes(os.urandom(10), "big")
    value &= ~(0xF << 76)
    value |= 0x7 << 76  # version
    value &= ~(0x3 << 62)
    value |= 0x2 << 62  # variant
    return uuid.UUID(int=value)
