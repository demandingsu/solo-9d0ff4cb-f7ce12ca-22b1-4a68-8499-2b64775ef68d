"""小时基准潮位上传与卫星分钟观测核验。

基准按 `(站号, UTC 小时)` 整小时保存：一次上传成功会原子替换该小时的
全部旧基准；CSV 解析失败或数据库写入失败时事务回滚，旧基准原样保留。

核验结果不落库，而是每次从**已提交观测**与**当前基准**派生：

    observations LEFT JOIN baselines ON (station_id, observed_at)

因此补段发布、迟到冲突撤下观测、复位后重新采集，核验结果都会自动随
已提交观测变化，旧库观测也无需重新上传。读取核验结果时使用一个 SQLite
读事务，保证一次响应（网页 JSON 或 CSV）对应同一个完整提交快照。
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3
from datetime import datetime, timedelta, timezone

TOLERANCE_M = 0.20
_EPS = 1e-9

_HOUR_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%MZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H",
)
_MINUTE_FORMATS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%MZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
)
_HEADER_FIRST = {
    "observed_at", "time", "minute", "timestamp", "datetime", "utc_minute",
}
_HEADER_SECOND = {
    "level_m", "level", "baseline_m", "tide_m", "value", "baseline",
}


def canonical_hour(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:00:00Z")


def canonical_minute(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:00Z")


def parse_utc_hour(value: str) -> str:
    """把用户给出的 UTC 小时规范化成 `YYYY-MM-DDTHH:00:00Z`。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("UTC 小时不能为空")
    text = value.strip()
    dt = None
    for fmt in _HOUR_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(
            "UTC 小时格式错误（应为 YYYY-MM-DDTHH:00:00Z，也接受 "
            "YYYY-MM-DDTHH:00 或 YYYY-MM-DDTHH）"
        )
    if dt.minute or dt.second or dt.microsecond:
        raise ValueError("上传基准的 UTC 小时必须是整点（分钟和秒均为 0）")
    return canonical_hour(dt)


def _parse_minute(text: str, hour_dt: datetime) -> str:
    """解析基准行的时间：完整 UTC 分钟，或仅 `HH:MM`（属于指定小时）。"""
    text = text.strip()
    if not text:
        raise ValueError("分钟为空")

    # 上传时已指定小时，因此允许直接写 HH:MM，便于人工整理整点文件。
    if "T" not in text and " " not in text and ":" in text and len(text) <= 5:
        try:
            hm = datetime.strptime(text, "%H:%M")
        except ValueError:
            raise ValueError(f"分钟格式错误: {text!r}") from None
        dt = hour_dt.replace(hour=hm.hour, minute=hm.minute, second=0, microsecond=0)
    else:
        dt = None
        for fmt in _MINUTE_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(
                f"分钟时间格式错误（应为 YYYY-MM-DDTHH:MM:00Z 或 HH:MM）: {text!r}"
            )

    if dt.second or dt.microsecond:
        raise ValueError(f"基准时间必须是整分钟: {text!r}")
    if dt.replace(minute=0, second=0, microsecond=0) != hour_dt:
        raise ValueError(f"基准分钟 {canonical_minute(dt)} 不属于指定 UTC 小时")
    return canonical_minute(dt)


def _looks_like_header(parts: list[str]) -> bool:
    if len(parts) != 2:
        return False
    return parts[0].strip().casefold() in _HEADER_FIRST and \
        parts[1].strip().casefold() in _HEADER_SECOND


def parse_baseline_csv(data: bytes | str, station_id: int, hour: str) -> list[tuple[str, float]]:
    """解析指定站号、UTC 小时的分钟基准 CSV。

    每个数据行 `时间,基准潮位(m)`。时间可为完整 UTC 分钟或 `HH:MM`；
    允许空行、`#` 注释行，以及首行英文表头。每分钟只能出现一次，
    潮位必须是有限数值。任一行非法即抛 ValueError，调用方据此保留旧基准。
    """
    hour_start = parse_utc_hour(hour)
    hour_dt = datetime.strptime(hour_start, "%Y-%m-%dT%H:%M:%SZ")

    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"基准 CSV 不是合法 UTF-8: {exc}") from exc
    elif isinstance(data, str):
        text = data.lstrip("﻿")
    else:
        raise ValueError("基准内容必须是文本")

    rows: list[tuple[str, float]] = []
    seen: dict[str, int] = {}
    first_data_line = True

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if first_data_line and _looks_like_header(parts):
            first_data_line = False
            continue
        first_data_line = False

        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"第 {lineno} 行格式错误（应为 时间,基准潮位）: {line!r}")
        try:
            observed_at = _parse_minute(parts[0], hour_dt)
        except ValueError as exc:
            raise ValueError(f"第 {lineno} 行：{exc}") from None
        try:
            level = float(parts[1])
        except ValueError:
            raise ValueError(f"第 {lineno} 行基准潮位不是数值: {parts[1]!r}") from None
        if not math.isfinite(level):
            raise ValueError(f"第 {lineno} 行基准潮位不是有限数值: {parts[1]!r}")
        if observed_at in seen:
            raise ValueError(
                f"第 {lineno} 行与第 {seen[observed_at]} 行重复分钟 "
                f"{observed_at}（每分钟只能有一个数值）"
            )
        seen[observed_at] = lineno
        rows.append((observed_at, level))

    if not rows:
        raise ValueError("基准 CSV 不含任何数据行")
    return rows


def replace_baseline(con: sqlite3.Connection, station_id: int, hour: str,
                     rows: list[tuple[str, float]]) -> dict:
    """在一个事务里整小时替换旧基准；任何写入失败都回滚并保留旧版。"""
    hour_start = parse_utc_hour(hour)
    if not rows:
        raise ValueError("基准 CSV 不含任何数据行")
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    con.execute("BEGIN IMMEDIATE")
    try:
        previous = con.execute(
            "SELECT COUNT(*) FROM baselines WHERE station_id=? AND hour_start=?",
            (station_id, hour_start),
        ).fetchone()[0]
        con.execute(
            "DELETE FROM baselines WHERE station_id=? AND hour_start=?",
            (station_id, hour_start),
        )
        con.executemany(
            "INSERT INTO baselines"
            " (station_id, hour_start, observed_at, level_m, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(station_id, hour_start, ts, level, updated_at) for ts, level in rows],
        )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise

    return {
        "station_id": station_id,
        "hour": hour_start,
        "rows": len(rows),
        "previous_rows": previous,
        "updated_at": updated_at,
    }


def list_baselines(con: sqlite3.Connection, station_id: int | None = None) -> list[dict]:
    """当前各站各小时的基准版本摘要。"""
    sql = (
        "SELECT station_id, hour_start, COUNT(*) AS row_count,"
        " MIN(observed_at) AS first_minute, MAX(observed_at) AS last_minute,"
        " MAX(updated_at) AS updated_at"
        " FROM baselines"
    )
    args: list = []
    if station_id is not None:
        sql += " WHERE station_id=?"
        args.append(station_id)
    sql += " GROUP BY station_id, hour_start ORDER BY station_id, hour_start"
    return [
        {
            "station_id": r["station_id"],
            "hour": r["hour_start"],
            "rows": r["row_count"],
            "first_minute": r["first_minute"],
            "last_minute": r["last_minute"],
            "updated_at": r["updated_at"],
        }
        for r in con.execute(sql, args).fetchall()
    ]


def _raw_line_map(con: sqlite3.Connection, keys: set[tuple[int, int, int]]) -> dict:
    """为结果行取回来源报文的原始 CSV 行（用于“原报文”展示）。

    以观测自身记录的来源 (站号, 启动号, 报文号) 定位，避免同一分钟被多个
    报文重传时把原报文行张冠李戴。同一报文内重复分钟取首次出现的行，
    与 `INSERT OR IGNORE` 的入库语义一致。旧库中来源段已不存在的观测没有
    原报文行，返回空。
    """
    out: dict[tuple[int, int, int, str], str] = {}
    for station_id, boot_id, msg_id in sorted(keys):
        segments = con.execute(
            "SELECT payload FROM segments"
            " WHERE station_id=? AND boot_id=? AND msg_id=? ORDER BY seg_no",
            (station_id, boot_id, msg_id),
        ).fetchall()
        if not segments:
            continue
        try:
            text = b"".join(bytes(s["payload"]) for s in segments).decode("utf-8")
        except UnicodeDecodeError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 2:
                continue
            try:
                dt = datetime.strptime(parts[0], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            out.setdefault(
                (station_id, boot_id, msg_id, canonical_minute(dt)), line)
    return out


def build_verification(con: sqlite3.Connection, station_id: int | None = None,
                       hour: str | None = None) -> dict:
    """从同一个已提交快照计算核验结果。调用方须持有数据库互斥锁。"""
    hour_start = parse_utc_hour(hour) if hour is not None else None
    where: list[str] = []
    args: list = []
    if station_id is not None:
        where.append("o.station_id=?")
        args.append(station_id)
    if hour_start is not None:
        end_dt = datetime.strptime(hour_start, "%Y-%m-%dT%H:%M:%SZ") + timedelta(hours=1)
        where.append("o.observed_at >= ? AND o.observed_at < ?")
        args += [hour_start, canonical_hour(end_dt)]

    sql = (
        "SELECT o.station_id, o.observed_at, o.level_m AS observed_m,"
        " o.boot_id, o.msg_id, b.level_m AS baseline_m"
        " FROM observations o"
        " LEFT JOIN baselines b ON b.station_id=o.station_id"
        "   AND b.observed_at=o.observed_at"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.station_id, o.observed_at"

    # 一个读事务内同时读取观测/基准与来源报文，避免响应被并发的发布、
    # 撤下或基准替换切成两半。
    con.execute("BEGIN")
    try:
        rows = con.execute(sql, args).fetchall()
        # 只有“缺基准 / 超差”的明细需要展示原报文，避免为合格行读取全部段。
        need_raw = {
            (r["station_id"], r["boot_id"], r["msg_id"])
            for r in rows
            if r["baseline_m"] is None
            or abs(r["observed_m"] - r["baseline_m"]) > TOLERANCE_M + _EPS
        }
        raw_lines = _raw_line_map(con, need_raw)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise

    results: list[dict] = []
    summary = {"checked": 0, "passed": 0, "missing_baseline": 0,
               "out_of_tolerance": 0}
    for r in rows:
        observed = r["observed_m"]
        baseline = r["baseline_m"]
        diff = None
        if baseline is None:
            status = "missing_baseline"
            summary["missing_baseline"] += 1
        else:
            diff = observed - baseline
            if abs(diff) <= TOLERANCE_M + _EPS:
                status = "pass"
                summary["passed"] += 1
            else:
                status = "out_of_tolerance"
                summary["out_of_tolerance"] += 1
        summary["checked"] += 1
        results.append({
            "station_id": r["station_id"],
            "observed_at": r["observed_at"],
            "observed_level_m": observed,
            "baseline_level_m": baseline,
            "diff_m": round(diff, 3) if diff is not None else None,
            "status": status,
            "boot_id": r["boot_id"],
            "msg_id": r["msg_id"],
            "raw_line": raw_lines.get(
                (r["station_id"], r["boot_id"], r["msg_id"], r["observed_at"])),
        })

    return {
        "tolerance_m": TOLERANCE_M,
        "station_id": station_id,
        "hour": hour_start,
        "summary": summary,
        "results": results,
    }


def verification_csv(con: sqlite3.Connection, station_id: int | None = None,
                     hour: str | None = None) -> str:
    """核验 CSV：只包含差值绝对值不超过 0.20 米的已确认观测。"""
    report = build_verification(con, station_id=station_id, hour=hour)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "station_id", "observed_at", "observed_level_m", "baseline_level_m",
        "diff_m", "boot_id", "msg_id",
    ])
    for r in report["results"]:
        if r["status"] != "pass":
            continue
        writer.writerow([
            r["station_id"], r["observed_at"], r["observed_level_m"],
            r["baseline_level_m"], r["diff_m"], r["boot_id"], r["msg_id"],
        ])
    return buf.getvalue()
