"""基准上传/整小时替换/核验结果/并发一致性测试。"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import verification  # noqa: E402
from app.main import create_app  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
UPLOAD = os.path.join(SAMPLES, "sample_upload.ndjson")
CONFLICTS = os.path.join(SAMPLES, "sample_conflicts.ndjson")
BASE_22 = os.path.join(SAMPLES, "sample_baseline_101_2026-11-14T22.csv")
BASE_01 = os.path.join(SAMPLES, "sample_baseline_101_2026-11-15T01.csv")
HOUR_22 = "2026-11-14T22:00:00Z"
HOUR_01 = "2026-11-15T01:00:00Z"


def make_client(tmp_path, name="t.db"):
    return TestClient(create_app(str(tmp_path / name)))


def upload(client, path):
    with open(path, "rb") as fh:
        return client.post("/api/upload", files={"file": (os.path.basename(path), fh)})


def upload_baseline(client, station, hour, text_or_path):
    if os.path.exists(str(text_or_path)):
        raw = open(text_or_path, "rb").read()
        name = os.path.basename(text_or_path)
    else:
        raw = str(text_or_path).encode("utf-8")
        name = "baseline.csv"
    return client.post(
        "/api/baselines",
        data={"station_id": str(station), "hour": hour},
        files={"file": (name, raw)},
    )


def verif(client, **params):
    r = client.get("/api/verification", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def pair_map(report):
    return {(r["station_id"], r["observed_at"]): r for r in report["results"]}


def baseline_lines(hour_start, levels):
    """levels: {minute_index: level}。"""
    lines = [f"# baseline {hour_start}"]
    hour = int(hour_start[11:13])
    for minute, level in levels.items():
        ts = hour_start[:11] + f"{hour:02d}:{minute:02d}:00Z"
        lines.append(f"{ts},{level}")
    return "\n".join(lines) + "\n"


# ---------- 基准 CSV 解析 ----------

def test_parse_baseline_accepts_full_minute_hm_header_and_comments():
    text = (
        "# 海事测绘部门小时基准\n"
        "\n"
        "time,level_m\n"
        "2026-11-15T08:00:00Z,1.10\n"
        "08:01,1.11\n"
        "2026-11-15T08:02Z,1.12\n"
    )
    rows = verification.parse_baseline_csv(text, 101, "2026-11-15T08:00:00Z")
    assert rows == [
        ("2026-11-15T08:00:00Z", 1.10),
        ("2026-11-15T08:01:00Z", 1.11),
        ("2026-11-15T08:02:00Z", 1.12),
    ]


@pytest.mark.parametrize("bad", [
    "2026-11-15T08:00:00Z,1.0\n2026-11-15T08:00:00Z,1.1\n",  # 重复分钟
    "08:00,1.0\n08:00,1.1\n",
    "2026-11-15T08:00:00Z,nan\n",                            # 非有限
    "2026-11-15T08:00:00Z,inf\n",
    "2026-11-15T08:00:00Z,-inf\n",
    "2026-11-15T08:00:00Z,abc\n",                            # 非数值
    "2026-11-15T08:00:30Z,1.0\n",                            # 非整分钟
    "2026-11-15T09:00:00Z,1.0\n",                            # 不属于该小时
    "2026-11-15T08:00:00Z\n",                                # 缺列
    "not-a-time,1.0\n",
    "2026-11-15T08:00:00Z,1.0,extra\n",
    "\n# only comments\n",                                   # 无数据行
])
def test_parse_baseline_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        verification.parse_baseline_csv(bad.encode("utf-8"), 101,
                                        "2026-11-15T08:00:00Z")


def test_parse_baseline_rejects_invalid_utf8():
    with pytest.raises(ValueError):
        verification.parse_baseline_csv(b"\xff\xfe\x00garbage", 101,
                                        "2026-11-15T08:00:00Z")


@pytest.mark.parametrize("hour", [
    "2026-11-15T08:30:00Z", "2026-11-15", "2026-11-15T08:00:01Z", "",
])
def test_parse_utc_hour_rejects_non_hour(hour):
    with pytest.raises(ValueError):
        verification.parse_utc_hour(hour)


def test_parse_utc_hour_accepts_compact_forms():
    assert verification.parse_utc_hour("2026-11-15T08:00Z") == "2026-11-15T08:00:00Z"
    assert verification.parse_utc_hour("2026-11-15T08:00") == "2026-11-15T08:00:00Z"
    assert verification.parse_utc_hour("2026-11-15T08") == "2026-11-15T08:00:00Z"


# ---------- 整小时替换 / 失败保留旧版 ----------

def test_baseline_upload_replaces_whole_hour(tmp_path):
    with make_client(tmp_path) as c:
        first = upload_baseline(c, 101, HOUR_22, "22:00,1.0\n22:01,1.1\n22:02,1.2\n")
        assert first.status_code == 200, first.text
        assert first.json()["rows"] == 3
        assert first.json()["previous_rows"] == 0

        # 第二次只含 2 行：该小时其余分钟必须消失（整小时替换，而非合并）
        second = upload_baseline(c, 101, HOUR_22, "22:10,2.0\n22:11,2.1\n")
        assert second.status_code == 200
        assert second.json()["rows"] == 2
        assert second.json()["previous_rows"] == 3
        listed = c.get("/api/baselines", params={"station_id": 101}).json()["baselines"]
        assert len(listed) == 1
        assert listed[0]["hour"] == HOUR_22
        assert listed[0]["rows"] == 2
        assert listed[0]["first_minute"] == "2026-11-14T22:10:00Z"
        assert listed[0]["last_minute"] == "2026-11-14T22:11:00Z"


def test_baseline_bad_format_keeps_old_version(tmp_path):
    with make_client(tmp_path) as c:
        assert upload_baseline(c, 101, HOUR_22, "22:00,1.0\n22:01,1.1\n").status_code == 200
        before = verif(c, station_id=101, hour=HOUR_22)
        r = upload_baseline(c, 101, HOUR_22, "22:00,2.0\n22:00,2.1\n")
        assert r.status_code == 400
        assert "重复分钟" in r.json()["detail"]
        assert upload_baseline(c, 101, HOUR_22, "22:00,nan\n").status_code == 400
        assert upload_baseline(c, 101, HOUR_22, "").status_code == 400
        listed = c.get("/api/baselines").json()["baselines"]
        assert listed[0]["rows"] == 2
        assert verif(c, station_id=101, hour=HOUR_22)["results"] == before["results"]


def test_baseline_rejects_missing_station_or_hour(tmp_path):
    with make_client(tmp_path) as c:
        r = c.post("/api/baselines", files={"file": ("b.csv", b"22:00,1.0\n")})
        assert r.status_code == 400 and "station_id" in r.json()["detail"]
        r = c.post("/api/baselines", data={"station_id": "101"},
                   files={"file": ("b.csv", b"22:00,1.0\n")})
        assert r.status_code == 400 and "hour" in r.json()["detail"]
        r = c.post("/api/baselines", data={"station_id": "abc", "hour": HOUR_22},
                   files={"file": ("b.csv", b"22:00,1.0\n")})
        assert r.status_code == 400 and "station_id" in r.json()["detail"]
        r = c.post("/api/baselines", data={"station_id": "101", "hour": "2026-11-14T22:30:00Z"},
                   files={"file": ("b.csv", b"22:00,1.0\n")})
        assert r.status_code == 400 and "整点" in r.json()["detail"]


def test_baseline_write_failure_rolls_back_and_keeps_old(tmp_path):
    with make_client(tmp_path) as c:
        assert upload_baseline(c, 101, HOUR_22, "22:00,1.0\n22:01,1.1\n").status_code == 200
        con = c.app.state.db
        before = [tuple(r) for r in con.execute(
            "SELECT station_id, observed_at, level_m FROM baselines"
            " WHERE station_id=101 AND hour_start=? ORDER BY observed_at", (HOUR_22,))]

        class FailingProxy:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def executemany(self, *args, **kwargs):
                raise sqlite3.OperationalError("disk I/O error (simulated)")

        with pytest.raises(sqlite3.OperationalError):
            verification.replace_baseline(
                FailingProxy(con), 101, HOUR_22, [("2026-11-14T22:00:00Z", 9.9)])

        after = [tuple(r) for r in con.execute(
            "SELECT station_id, observed_at, level_m FROM baselines"
            " WHERE station_id=101 AND hour_start=? ORDER BY observed_at", (HOUR_22,))]
        assert after == before
        # 事务已结束，连接仍可正常使用
        assert con.execute("SELECT COUNT(*) FROM baselines").fetchone()[0] == 2


def test_baseline_upload_route_returns_500_and_keeps_old_on_db_error(tmp_path, monkeypatch):
    with make_client(tmp_path) as c:
        assert upload_baseline(c, 101, HOUR_22, "22:00,1.0\n").status_code == 200

        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(verification, "replace_baseline", boom)
        r = upload_baseline(c, 101, HOUR_22, "22:00,2.0\n")
        assert r.status_code == 500
        assert "保留旧版" in r.json()["detail"]
        assert c.get("/api/baselines").json()["baselines"][0]["rows"] == 1


# ---------- 配对与三分类 ----------

def test_verification_pairs_station_and_minute_with_tolerance(tmp_path):
    with make_client(tmp_path) as c:
        assert upload(c, UPLOAD).status_code == 200
        # 先有观测、后上传基准：旧观测无需重传即可核验
        assert upload_baseline(c, 101, HOUR_22, BASE_22).status_code == 200
        assert upload_baseline(c, 101, HOUR_01, BASE_01).status_code == 200

        report = verif(c)
        rows = pair_map(report)
        assert report["tolerance_m"] == 0.20
        assert report["summary"]["checked"] == len(rows) == 120

        # 第 10、11 分钟基准故意偏移 >= 0.35 m
        m10 = rows[(101, "2026-11-14T22:10:00Z")]
        assert m10["status"] == "out_of_tolerance"
        assert m10["baseline_level_m"] is not None
        assert m10["observed_level_m"] is not None
        assert m10["diff_m"] == pytest.approx(
            round(m10["observed_level_m"] - m10["baseline_level_m"], 3))
        assert abs(m10["diff_m"]) > 0.20
        assert m10["raw_line"] == (
            f"2026-11-14T22:10:00Z,{m10['observed_level_m']:.2f}")
        assert m10["msg_id"] == 1001

        # 第 30–32 分钟缺基准
        miss = rows[(101, "2026-11-14T22:30:00Z")]
        assert miss["status"] == "missing_baseline"
        assert miss["baseline_level_m"] is None and miss["diff_m"] is None
        assert miss["raw_line"] and miss["msg_id"] == 1001

        assert report["summary"]["missing_baseline"] == 3
        assert report["summary"]["out_of_tolerance"] == 2
        assert report["summary"]["passed"] == 115

        # 01 时基准全部合格
        hour01 = [r for r in report["results"] if r["observed_at"].startswith("2026-11-15T01")]
        assert len(hour01) == 60 and all(r["status"] == "pass" for r in hour01)


def test_verification_tolerance_boundary_is_inclusive(tmp_path):
    payload = ("2026-11-15T05:00:00Z,1.20\n"
               "2026-11-15T05:01:00Z,1.1999\n").encode()
    rec = json.dumps({
        "station_id": 55, "boot_id": 1, "msg_id": 1, "seg_no": 1, "seg_total": 1,
        "payload_b64": base64.b64encode(payload).decode(),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }) + "\n"
    with make_client(tmp_path) as c:
        assert c.post("/api/upload", content=rec).json()["counts"]["published"] == 1
        r = upload_baseline(c, 55, "2026-11-15T05:00:00Z",
                            "05:00,1.00\n05:01,1.00\n")
        assert r.status_code == 200
        rows = pair_map(verif(c))
        assert rows[(55, "2026-11-15T05:00:00Z")]["status"] == "pass"   # 恰好 0.20
        assert rows[(55, "2026-11-15T05:01:00Z")]["status"] == "pass"   # 0.1999
        r2 = upload_baseline(c, 55, "2026-11-15T05:00:00Z",
                             "05:00,1.00\n05:01,0.9998\n")
        assert r2.status_code == 200
        rows = pair_map(verif(c))
        assert rows[(55, "2026-11-15T05:01:00Z")]["status"] == "out_of_tolerance"


def test_verification_ignores_other_station_and_filters_hour(tmp_path):
    with make_client(tmp_path) as c:
        upload(c, UPLOAD)
        upload_baseline(c, 101, HOUR_22, BASE_22)
        upload_baseline(c, 101, HOUR_01, BASE_01)
        only22 = verif(c, station_id=101, hour=HOUR_22)
        assert only22["hour"] == HOUR_22
        assert len(only22["results"]) == 60
        assert all(r["observed_at"].startswith("2026-11-14T22") for r in only22["results"])
        assert verif(c, station_id=999)["results"] == []
        assert c.get("/api/verification", params={"hour": "bad"}).status_code == 400


# ---------- 核验 CSV ----------

def test_verification_csv_contains_only_passing_rows(tmp_path):
    with make_client(tmp_path) as c:
        upload(c, UPLOAD)
        upload_baseline(c, 101, HOUR_22, BASE_22)
        upload_baseline(c, 101, HOUR_01, BASE_01)
        resp = c.get("/api/verification.csv")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        assert len(rows) == 115
        assert set(rows[0]) == {
            "station_id", "observed_at", "observed_level_m",
            "baseline_level_m", "diff_m", "boot_id", "msg_id"}
        for r in rows:
            assert abs(float(r["diff_m"])) <= 0.20 + 1e-9
            assert abs(float(r["observed_level_m"]) - float(r["baseline_level_m"])
                       - float(r["diff_m"])) < 1e-2
        minutes = {(r["station_id"], r["observed_at"]) for r in rows}
        assert (101, "2026-11-14T22:10:00Z") not in minutes   # 超差
        assert (101, "2026-11-14T22:30:00Z") not in minutes   # 缺基准

        only01 = c.get("/api/verification.csv", params={"hour": HOUR_01})
        assert len(list(csv.DictReader(io.StringIO(only01.text)))) == 60


def test_exceptions_csv_lists_missing_and_out_of_tolerance(tmp_path):
    with make_client(tmp_path) as c:
        upload(c, UPLOAD)
        upload_baseline(c, 101, HOUR_22, BASE_22)
        upload_baseline(c, 101, HOUR_01, BASE_01)
        rows = list(csv.DictReader(io.StringIO(
            c.get("/api/verification/exceptions.csv").text)))
        assert len(rows) == 5
        by_minute = {r["observed_at"]: r for r in rows}
        over = by_minute["2026-11-14T22:10:00Z"]
        assert over["status"] == "out_of_tolerance"
        assert over["observed_level_m"] and over["baseline_level_m"] and over["diff_m"]
        miss = by_minute["2026-11-14T22:30:00Z"]
        assert miss["status"] == "missing_baseline"
        assert miss["baseline_level_m"] == "" and miss["diff_m"] == ""
        assert miss["observed_level_m"] and miss["raw_line"]


# ---------- 核验结果随已提交观测更新 ----------

def _split_message(station, boot, msg, minutes, split_at):
    lines = [f"2026-11-15T04:{m:02d}:00Z,2.50" for m in range(minutes)]
    data = ("\n".join(lines) + "\n").encode()
    parts = [b"\n".join(data.splitlines()[:split_at]) + b"\n",
             b"\n".join(data.splitlines()[split_at:]) + b"\n"]
    digest = hashlib.sha256(data).hexdigest()
    return [
        {"station_id": station, "boot_id": boot, "msg_id": msg,
         "seg_no": i + 1, "seg_total": 2,
         "payload_b64": base64.b64encode(part).decode(), "sha256": digest}
        for i, part in enumerate(parts)
    ]


def test_late_segment_publish_updates_verification(tmp_path):
    with make_client(tmp_path) as c:
        seg1, seg2 = _split_message(101, 7, 1003, 60, 30)
        c.post("/api/upload", content=json.dumps(seg1) + "\n")
        upload_baseline(c, 101, "2026-11-15T04:00:00Z",
                        baseline_lines("2026-11-15T04:00:00Z",
                                       {m: 2.5 for m in range(60)}))
        before = verif(c, station_id=101, hour="2026-11-15T04:00:00Z")
        assert before["results"] == []

        # 补齐第 2 段 -> 整报发布，核验结果自动出现
        r = c.post("/api/upload", content=json.dumps(seg2) + "\n")
        assert r.json()["counts"]["published"] == 1
        after = verif(c, station_id=101, hour="2026-11-15T04:00:00Z")
        assert len(after["results"]) == 60
        assert after["summary"]["checked"] == 60
        assert all(row["msg_id"] == 1003 for row in after["results"])
        assert len(list(csv.DictReader(io.StringIO(
            c.get("/api/verification.csv",
                  params={"hour": "2026-11-15T04:00:00Z"}).text)))) == 60


def test_late_conflict_withdraws_verification_and_cause(tmp_path):
    with make_client(tmp_path) as c:
        # 2004 先完整发布
        recs = [json.loads(line) for line in open(CONFLICTS, encoding="utf-8")]
        first = [r for r in recs if r["msg_id"] == 2004][:2]
        c.post("/api/upload", content="\n".join(json.dumps(r) for r in first) + "\n")
        assert any(r["msg_id"] == 2004 for r in verif(c)["results"])

        # 再收到被篡改的同号段 -> 待核并撤下观测，核验结果同步减少
        forged = [r for r in recs if r["msg_id"] == 2004][2]
        c.post("/api/upload", content=json.dumps(forged) + "\n")
        report = verif(c)
        assert all(r["msg_id"] != 2004 for r in report["results"])
        assert all(r["msg_id"] != 2004 for r in csv.DictReader(io.StringIO(
            c.get("/api/verification.csv").text)))
        # 原始观测下载同样不再包含它
        assert all(o["msg_id"] != 2004 for o in
                   c.get("/api/observations", params={"limit": 5000}).json()["observations"])


def test_reset_then_reingest_updates_verification(tmp_path):
    payload = b"2026-11-15T06:00:00Z,3.00\n"
    good = {
        "station_id": 77, "boot_id": 2, "msg_id": 5, "seg_no": 1, "seg_total": 1,
        "payload_b64": base64.b64encode(payload).decode(),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    with make_client(tmp_path) as c:
        c.post("/api/upload", content=json.dumps(good) + "\n")
        upload_baseline(c, 77, "2026-11-15T06:00:00Z", "06:00,3.05\n")
        assert pair_map(verif(c))[(77, "2026-11-15T06:00:00Z")]["status"] == "pass"

        # 复位后观测撤销，核验结果随之清空；基准保留
        assert c.delete("/api/messages/77/2/5").status_code == 200
        assert verif(c)["results"] == []
        assert c.get("/api/baselines").json()["baselines"][0]["rows"] == 1

        # 重新采集不同数值 -> 核验按新观测重算
        payload2 = b"2026-11-15T06:00:00Z,3.50\n"
        good2 = dict(good, payload_b64=base64.b64encode(payload2).decode(),
                     sha256=hashlib.sha256(payload2).hexdigest())
        c.post("/api/upload", content=json.dumps(good2) + "\n")
        row = pair_map(verif(c))[(77, "2026-11-15T06:00:00Z")]
        assert row["status"] == "out_of_tolerance"
        assert row["observed_level_m"] == 3.5 and row["baseline_level_m"] == 3.05


# ---------- 并发：网页核验 JSON / CSV 各自是完整提交快照 ----------

def test_concurrent_uploads_baselines_and_verification(tmp_path):
    start = "2026-11-15T09:00:00Z"
    payload = "".join(
        f"2026-11-15T09:{m:02d}:00Z,{1.0 + m * 0.001:.3f}\n" for m in range(60)
    ).encode()
    rec = json.dumps({
        "station_id": 88, "boot_id": 3, "msg_id": 1, "seg_no": 1, "seg_total": 1,
        "payload_b64": base64.b64encode(payload).decode(),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }) + "\n"

    # 两版基准：A 全部合格（偏差 0.05），B 全部超差（偏差 0.9）
    base_a = "".join(
        f"09:{m:02d},{1.0 + m * 0.001 + 0.05:.3f}\n" for m in range(60))
    base_b = "".join(
        f"09:{m:02d},{1.0 + m * 0.001 - 0.9:.3f}\n" for m in range(60))

    with make_client(tmp_path) as c:
        assert c.post("/api/upload", content=rec).status_code == 200

        def do_baseline(i):
            return upload_baseline(c, 88, start, base_a if i % 2 == 0 else base_b)

        def do_verify(_):
            r = c.get("/api/verification", params={"station_id": 88})
            assert r.status_code == 200
            return r.json()

        def do_csv(_):
            r = c.get("/api/verification.csv", params={"station_id": 88})
            assert r.status_code == 200
            return r.text

        def do_observations(_):
            return c.get("/api/observations.csv", params={"station_id": 88})

        tasks = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            for i in range(12):
                tasks.append(pool.submit(do_baseline, i))
                tasks.append(pool.submit(do_verify, i))
                tasks.append(pool.submit(do_csv, i))
                tasks.append(pool.submit(do_observations, i))
            results = [t.result() for t in tasks]

        reports = results[1::4]
        csv_texts = results[2::4]
        obs_resps = results[3::4]

        for report in reports:
            s = report["summary"]
            assert s["checked"] == len(report["results"]) == 60
            assert s["passed"] + s["missing_baseline"] + s["out_of_tolerance"] == 60
            # 同一响应内基准必须完整来自某一版（含“尚无基准”），不能新旧混排
            diffs = {r["diff_m"] for r in report["results"]}
            assert len(diffs) == 1, diffs
            only = report["results"][0]["status"]
            assert all(r["status"] == only for r in report["results"])
            if only == "missing_baseline":
                assert next(iter(diffs)) is None
                assert s["missing_baseline"] == 60
            elif only == "pass":
                assert s["passed"] == 60 and abs(next(iter(diffs))) < 0.06
            else:
                assert only == "out_of_tolerance" and s["out_of_tolerance"] == 60
                assert abs(next(iter(diffs))) > 0.8

        for text in csv_texts:
            rows = list(csv.DictReader(io.StringIO(text)))
            assert len(rows) in (0, 60)
            assert all(abs(float(r["diff_m"])) <= 0.20 + 1e-9 for r in rows)
            if rows:
                assert len({r["diff_m"] for r in rows}) == 1

        for resp in obs_resps:
            assert resp.status_code == 200
            lines = resp.text.strip().splitlines()
            assert len(lines) == 61          # 表头 + 60 分钟，原始下载不受基准影响
            assert lines[0] == "station_id,observed_at,level_m"


def test_concurrent_ndjson_upload_and_verification_snapshot(tmp_path):
    start = "2026-11-15T10:00:00Z"

    def message(station, msg_id):
        payload = "".join(
            f"2026-11-15T10:{m:02d}:00Z,{2.0 + m * 0.001:.3f}\n" for m in range(60)
        ).encode()
        return json.dumps({
            "station_id": station, "boot_id": 4, "msg_id": msg_id, "seg_no": 1,
            "seg_total": 1, "payload_b64": base64.b64encode(payload).decode(),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }) + "\n"

    base = "".join(f"10:{m:02d},{2.0 + m * 0.001 + 0.01:.3f}\n" for m in range(60))
    with make_client(tmp_path) as c:
        upload_baseline(c, 99, start, base)

        def ingest(i):
            return c.post("/api/upload", content=message(99 + i, 1000 + i))

        def verify(_):
            return verif(c)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = []
            for i in range(10):
                futures.append(pool.submit(ingest, i))
                futures.append(pool.submit(verify, i))
            results = [f.result() for f in futures]
        for i in range(0, len(results), 2):
            assert results[i].status_code == 200

        # 每个核验响应都必须是“整站整小时”的完整快照：某站的 60 分钟要么
        # 全在（且全合格），要么还没提交，绝不能出现半批。
        for i in range(1, len(results), 2):
            report = results[i]
            per_station: dict[int, list] = {}
            for row in report["results"]:
                per_station.setdefault(row["station_id"], []).append(row)
            for station_id, rows in per_station.items():
                assert len(rows) == 60
                expected = "pass" if station_id == 99 else "missing_baseline"
                assert all(r["status"] == expected for r in rows)

        final = verif(c)
        assert len(final["results"]) == 600
        assert final["summary"]["passed"] == 60        # 仅 99 站有基准
        assert final["summary"]["missing_baseline"] == 540


# ---------- 页面 ----------

def test_index_has_baseline_and_verification_controls(tmp_path):
    with make_client(tmp_path) as c:
        html = c.get("/").text
        for needle in ("base-station", "base-hour", "base-file", "btn-baseline",
                       "/api/verification.csv", "verify-body", "原始观测"):
            assert needle in html


# ---------- 旧库升级：无需重传已有观测 ----------

def test_existing_database_upgrades_and_old_observations_verify(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript("""
    CREATE TABLE messages (station_id INTEGER NOT NULL, boot_id INTEGER NOT NULL,
      msg_id INTEGER NOT NULL, seg_total INTEGER NOT NULL, sha256 TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'collecting', reason TEXT,
      updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
      PRIMARY KEY (station_id, boot_id, msg_id));
    CREATE TABLE segments (station_id INTEGER NOT NULL, boot_id INTEGER NOT NULL,
      msg_id INTEGER NOT NULL, seg_no INTEGER NOT NULL, payload BLOB NOT NULL,
      received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
      PRIMARY KEY (station_id, boot_id, msg_id, seg_no));
    CREATE TABLE observations (station_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
      level_m REAL NOT NULL, boot_id INTEGER NOT NULL, msg_id INTEGER NOT NULL,
      PRIMARY KEY (station_id, observed_at));
    INSERT INTO observations VALUES (101, '2026-11-15T08:00:00Z', 1.50, 1, 1);
    INSERT INTO observations VALUES (101, '2026-11-15T08:01:00Z', 1.80, 1, 1);
    INSERT INTO observations VALUES (101, '2026-11-15T08:02:00Z', 1.70, 1, 1);
    """)
    legacy.commit()
    legacy.close()

    with TestClient(create_app(str(path))) as c:
        r = upload_baseline(c, 101, "2026-11-15T08:00:00Z",
                            "08:00,1.60\n08:01,1.60\n08:02,1.45\n")
        assert r.status_code == 200
        rows = pair_map(verif(c))
        assert rows[(101, "2026-11-15T08:00:00Z")]["status"] == "pass"    # 0.10
        assert rows[(101, "2026-11-15T08:01:00Z")]["status"] == "pass"    # 恰好 0.20
        assert rows[(101, "2026-11-15T08:02:00Z")]["status"] == "out_of_tolerance"  # 0.25
        # 原报文段已不存在时原报文行为空，但其余核验信息完整
        assert rows[(101, "2026-11-15T08:02:00Z")]["raw_line"] is None
        # 原始下载行为不变
        assert c.get("/api/observations.csv").text.strip().splitlines() == [
            "station_id,observed_at,level_m",
            "101,2026-11-15T08:00:00Z,1.5",
            "101,2026-11-15T08:01:00Z,1.8",
            "101,2026-11-15T08:02:00Z,1.7",
        ]


def test_raw_line_follows_observations_own_source_message(tmp_path):
    # 同一站同一分钟被两个报文重传：观测归属第一个，原报文行也必须来自它
    minute = b"2026-11-15T07:00:00Z,9.90\n"
    other = b"2026-11-15T07:00:00Z,1.00\n"

    def rec(msg_id, payload):
        return json.dumps({
            "station_id": 66, "boot_id": 1, "msg_id": msg_id, "seg_no": 1,
            "seg_total": 1, "payload_b64": base64.b64encode(payload).decode(),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }) + "\n"

    with make_client(tmp_path) as c:
        c.post("/api/upload", content=rec(1, minute))
        c.post("/api/upload", content=rec(2, other))   # 同一分钟，被忽略
        upload_baseline(c, 66, "2026-11-15T07:00:00Z", "07:00,1.00\n")
        row = pair_map(verif(c))[(66, "2026-11-15T07:00:00Z")]
        assert row["msg_id"] == 1 and row["observed_level_m"] == 9.9
        assert row["raw_line"] == "2026-11-15T07:00:00Z,9.90"
        assert row["status"] == "out_of_tolerance"
