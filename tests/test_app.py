"""端到端测试：乱序重投、幂等入库、冲突待核、观测撤下、重启与并发。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import create_app  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


def make_client(tmp_path, name="t.db"):
    app = create_app(str(tmp_path / name))
    return TestClient(app)


def upload(client, path):
    with open(path, "rb") as fh:
        return client.post("/api/upload", files={"file": (os.path.basename(path), fh)})


def msg_map(client):
    return {(m["boot_id"], m["msg_id"]): m
            for m in client.get("/api/messages").json()["messages"]}


# ---------- 正常流：乱序 + 重投 ----------

def test_out_of_order_and_retransmit(tmp_path):
    with make_client(tmp_path) as c:
        r = upload(c, os.path.join(SAMPLES, "sample_upload.ndjson"))
        assert r.status_code == 200
        counts = r.json()["counts"]
        assert counts["published"] == 2          # 1001、1002 收齐发布
        assert counts["duplicate"] == 3          # 三条完全相同的重投被幂等忽略
        assert counts["conflict"] == 0

        msgs = msg_map(c)
        assert msgs[(7, 1001)]["status"] == "published"
        assert msgs[(7, 1002)]["status"] == "published"
        m3 = msgs[(7, 1003)]
        assert m3["status"] == "collecting"
        assert m3["missing_segments"] == [2]     # 网页缺段列

        obs = c.get("/api/observations", params={"limit": 5000}).json()["observations"]
        assert len(obs) == 120                   # 两个报文各 60 分钟
        assert all(o["observed_at"].endswith(":00Z") for o in obs)

        csv = c.get("/api/observations.csv")
        assert csv.status_code == 200
        assert "attachment" in csv.headers["content-disposition"]
        assert len(csv.text.strip().splitlines()) == 1 + 120


def test_reupload_is_idempotent(tmp_path):
    path = os.path.join(SAMPLES, "sample_upload.ndjson")
    with make_client(tmp_path) as c:
        upload(c, path)
        before = c.get("/api/observations", params={"limit": 5000}).json()["observations"]
        r = upload(c, path)                      # 原样再传一遍
        counts = r.json()["counts"]
        assert counts["stored"] == 0 and counts["published"] == 0
        assert counts["duplicate"] == 12         # 12 行全部命中已入库的段
        after = c.get("/api/observations", params={"limit": 5000}).json()["observations"]
        assert before == after                   # 不重复入库


# ---------- 冲突 -> 待核 ----------

def test_conflicts_flag_pending_review(tmp_path):
    with make_client(tmp_path) as c:
        r = upload(c, os.path.join(SAMPLES, "sample_conflicts.ndjson"))
        assert r.status_code == 200
        msgs = msg_map(c)
        for msg_id, needle in [
            (2001, "内容冲突"), (2002, "总段数冲突"),
            (2003, "摘要校验不符"), (2005, "摘要冲突"),
        ]:
            m = msgs[(7, msg_id)]
            assert m["status"] == "pending_review", msg_id
            assert needle in m["reason"], (msg_id, m["reason"])


def test_published_observations_withdrawn_on_conflict(tmp_path):
    with make_client(tmp_path) as c:
        upload(c, os.path.join(SAMPLES, "sample_conflicts.ndjson"))
        m = msg_map(c)[(7, 2004)]                # 先发布后收到篡改段
        assert m["status"] == "pending_review"
        assert "内容冲突" in m["reason"]
        assert m["observations"] == 0            # 该报文观测已撤下
        obs = c.get("/api/observations", params={"limit": 5000}).json()["observations"]
        assert all(o["msg_id"] != 2004 for o in obs)


def test_pending_message_ignores_late_segments(tmp_path):
    with make_client(tmp_path) as c:
        upload(c, os.path.join(SAMPLES, "sample_conflicts.ndjson"))
        # 待核报文 2001 再来一段：忽略，不入库
        rec = {"station_id": 101, "boot_id": 7, "msg_id": 2001, "seg_no": 1,
               "seg_total": 2, "payload_b64": base64.b64encode(b"x").decode(),
               "sha256": "0" * 64}
        r = c.post("/api/upload", content=json.dumps(rec) + "\n")
        assert r.json()["counts"]["ignored"] == 1
        # 复位后可重新采集
        assert c.delete("/api/messages/101/7/2001").status_code == 200
        assert (7, 2001) not in msg_map(c)


# ---------- 坏行不拖垮整批 ----------

def test_bad_lines_do_not_abort_batch(tmp_path):
    good = {"station_id": 9, "boot_id": 1, "msg_id": 1, "seg_no": 1, "seg_total": 1,
            "payload_b64": base64.b64encode(b"2026-11-15T00:00:00Z,1.23\n").decode(),
            "sha256": hashlib.sha256(b"2026-11-15T00:00:00Z,1.23\n").hexdigest()}
    text = "not json\n" + json.dumps({"station_id": 9}) + "\n" + json.dumps(good) + "\n"
    with make_client(tmp_path) as c:
        r = c.post("/api/upload", content=text)
        counts = r.json()["counts"]
        assert counts["error"] == 2
        assert counts["published"] == 1
        obs = c.get("/api/observations").json()["observations"]
        assert len(obs) == 1 and obs[0]["level_m"] == 1.23


# ---------- 重启 ----------

def test_restart_keeps_state_and_stays_idempotent(tmp_path):
    path = os.path.join(SAMPLES, "sample_upload.ndjson")
    with make_client(tmp_path) as c1:
        upload(c1, path)
        before = c1.get("/api/observations", params={"limit": 5000}).json()["observations"]
    # 模拟服务重启：同一数据库文件起新应用
    with make_client(tmp_path) as c2:
        after = c2.get("/api/observations", params={"limit": 5000}).json()["observations"]
        assert before == after                   # 重启后观测仍在，且只含完整报文
        counts = upload(c2, path).json()["counts"]
        assert counts["stored"] == 0             # 重投不重复入库
        msgs = msg_map(c2)
        assert msgs[(7, 1003)]["missing_segments"] == [2]


# ---------- 并发上传 ----------

def test_concurrent_uploads_no_duplicates(tmp_path):
    path = os.path.join(SAMPLES, "sample_upload.ndjson")
    with make_client(tmp_path) as c:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: upload(c, path), range(8)))
        assert all(r.status_code == 200 for r in results)
        obs = c.get("/api/observations", params={"limit": 5000}).json()["observations"]
        assert len(obs) == 120                   # 恰好入库一次
        keys = [(o["observed_at"]) for o in obs]
        assert len(keys) == len(set(keys))
        msgs = msg_map(c)
        assert msgs[(7, 1001)]["received"] == 4  # 段不重复
        assert msgs[(7, 1002)]["received"] == 3


def test_concurrent_same_single_segment_message(tmp_path):
    # 多个请求同时发同一个单段报文：只发布一次、观测不重复
    payload = b"2026-11-15T10:00:00Z,2.5\n2026-11-15T10:01:00Z,2.6\n"
    rec = json.dumps({"station_id": 5, "boot_id": 2, "msg_id": 9, "seg_no": 1,
                      "seg_total": 1, "payload_b64": base64.b64encode(payload).decode(),
                      "sha256": hashlib.sha256(payload).hexdigest()}) + "\n"
    with make_client(tmp_path) as c:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: c.post("/api/upload", content=rec), range(6)))
        obs = c.get("/api/observations", params={"limit": 5000}).json()["observations"]
        assert len(obs) == 2
        assert msg_map(c)[(2, 9)]["status"] == "published"
