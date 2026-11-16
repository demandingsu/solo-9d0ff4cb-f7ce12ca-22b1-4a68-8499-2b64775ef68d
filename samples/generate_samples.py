#!/usr/bin/env python3
"""生成浏览器可直接上传的样例 NDJSON：

  sample_upload.ndjson    —— 乱序 + 重投的正常样例（含一个缺段报文）
      站101/启动7/报文1001: 4 段，乱序，段2 重投两次（完全相同）
      站101/启动7/报文1002: 3 段，乱序，段1 重投一次
      站101/启动7/报文1003: 3 段只到 1、3 两段 -> 网页"缺段"列显示 2

  sample_conflicts.ndjson —— 冲突样例（演示"待核"与"撤下已发布观测"）
      报文2001: 同段号不同内容（段内容冲突）
      报文2002: 总段数 3 与 4 冲突
      报文2003: 段齐全但整报 SHA-256 与登记摘要不符
      报文2004: 先完整发布，随后收到内容被篡改的同号段 -> 转待核并撤下观测
      报文2005: 同一报文出现两个不同的整报摘要

用法: python3 samples/generate_samples.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
from datetime import datetime, timedelta

OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def tide_level(t: datetime) -> float:
    """半日潮 + 小扰动的模拟潮位（米）。"""
    hours = t.timestamp() / 3600.0
    return round(2.5 + 1.6 * math.sin(2 * math.pi * hours / 12.42)
                 + 0.3 * math.sin(2 * math.pi * hours / 4.1), 2)


def build_message(station: int, boot: int, msg: int,
                  start: datetime, minutes: int = 60) -> bytes:
    lines = [f"# station={station} boot={boot} msg={msg} kind=minute-tide"]
    for i in range(minutes):
        t = start + timedelta(minutes=i)
        lines.append(f"{t.strftime('%Y-%m-%dT%H:%M:00Z')},{tide_level(t):.2f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def fragment(data: bytes, n: int) -> list[bytes]:
    """按行边界切成 n 段（段拼接即还原整报）。"""
    lines = data.splitlines(keepends=True)
    chunks, per = [], max(1, math.ceil(len(lines) / n))
    for i in range(0, len(lines), per):
        chunks.append(b"".join(lines[i:i + per]))
    while len(chunks) < n:  # 行太少时补空段，保证段数正确
        chunks.append(b"")
    return chunks


def record(station: int, boot: int, msg: int, seg_no: int, seg_total: int,
           payload: bytes, sha256: str) -> dict:
    return {
        "station_id": station, "boot_id": boot, "msg_id": msg,
        "seg_no": seg_no, "seg_total": seg_total,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "sha256": sha256,
    }


def message_records(station: int, boot: int, msg: int,
                    start: datetime, n_seg: int) -> list[dict]:
    data = build_message(station, boot, msg, start)
    digest = hashlib.sha256(data).hexdigest()
    return [record(station, boot, msg, i + 1, n_seg, seg, digest)
            for i, seg in enumerate(fragment(data, n_seg))]


def write_ndjson(name: str, records: list[dict]) -> None:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {path} ({len(records)} records)")


def main() -> None:
    rng = random.Random(20261115)
    day = datetime(2026, 11, 14, 22, 0, 0)

    # ---------- 正常样例：乱序 + 重投 ----------
    m1001 = message_records(101, 7, 1001, day, 4)
    m1002 = message_records(101, 7, 1002, day + timedelta(hours=3), 3)
    m1003 = message_records(101, 7, 1003, day + timedelta(hours=6), 3)

    pool = m1001 + m1002 + m1003[:1] + m1003[2:]  # 1003 缺第 2 段
    dupes = [dict(m1001[1]), dict(m1001[1]), dict(m1002[0])]  # 完全相同的重投
    pool += dupes
    rng.shuffle(pool)
    write_ndjson("sample_upload.ndjson", pool)

    # ---------- 冲突样例 ----------
    out: list[dict] = []

    # 2001: 同段号不同内容
    m2001 = message_records(101, 7, 2001, day + timedelta(hours=9), 2)
    tampered = dict(m2001[0])
    tampered["payload_b64"] = base64.b64encode(b"# forged segment\n").decode()
    out += [m2001[0], m2001[1], tampered]

    # 2002: 总段数冲突（3 vs 4）
    m2002 = message_records(101, 7, 2002, day + timedelta(hours=12), 3)
    wrong_total = dict(m2002[1])
    wrong_total["seg_total"] = 4
    out += [m2002[0], wrong_total]

    # 2003: 段齐全但整报摘要与登记不符
    m2003 = message_records(101, 7, 2003, day + timedelta(hours=15), 2)
    bad_digest = hashlib.sha256(b"some other message").hexdigest()
    out += [dict(r, sha256=bad_digest) for r in m2003]

    # 2004: 先完整发布，再收到被篡改的同号段 -> 待核并撤下观测
    m2004 = message_records(101, 7, 2004, day + timedelta(hours=18), 2)
    forged = dict(m2004[0])
    forged["payload_b64"] = base64.b64encode(b"# retransmitted but altered\n").decode()
    out += m2004 + [forged]

    # 2005: 同一报文两个不同的整报摘要
    m2005 = message_records(101, 7, 2005, day + timedelta(hours=21), 2)
    other = dict(m2005[1])
    other["sha256"] = hashlib.sha256(b"conflicting digest").hexdigest()
    out += [m2005[0], other]

    write_ndjson("sample_conflicts.ndjson", out)


if __name__ == "__main__":
    main()
