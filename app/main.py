"""沿海潮位卫星短报文采集与小时基准核验服务（FastAPI + SQLite）。

接口：
  GET    /                                采集网页（上传 / 报文状态 / 基准 / 核验 / 下载）
  POST   /api/upload                      上传 NDJSON（multipart 文件或裸文本）
  GET    /api/messages                    报文状态：已收/缺段/待核原因
  GET    /api/observations                已确认观测（JSON 预览）
  GET    /api/observations.csv            已确认观测下载（可选 ?station_id=）
  POST   /api/baselines                   上传指定站号 + UTC 小时的分钟基准 CSV（整小时替换）
  GET    /api/baselines                   当前基准版本列表
  GET    /api/verification                核验结果 JSON（缺基准 / 超差明细 + 原报文）
  GET    /api/verification.csv            核验 CSV（仅差值 ≤ 0.20 m 的合格观测）
  GET    /api/verification/exceptions.csv 缺基准与超差明细 CSV（含两侧数值、差值、原报文）
  DELETE /api/messages/{station}/{boot}/{msg}  待核处理后复位报文，允许重新采集
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from . import core, db, verification

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _csv_response(text: str, name: str) -> Response:
    return Response(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def create_app(db_path: str) -> FastAPI:
    # 单进程内串行化对同一 SQLite 连接的所有读写；配合 BEGIN IMMEDIATE、
    # WAL 与 busy_timeout，保证并发请求各自看到完整且已提交的状态。
    state_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db.connect(db_path)
        yield
        app.state.db.close()

    app = FastAPI(title="潮位短报文采集与基准核验", lifespan=lifespan)

    async def _read_baseline_form(request: Request):
        """兼容 multipart 表单（file/station_id/hour）与裸 CSV + 查询参数。"""
        ctype = request.headers.get("content-type", "")
        station_raw = hour_raw = None
        data: bytes | None = None
        if ctype.startswith("multipart/form-data") or \
                ctype.startswith("application/x-www-form-urlencoded"):
            form = await request.form()
            station_raw = form.get("station_id")
            hour_raw = form.get("hour")
            uploaded = form.get("file") or form.get("csv")
            if isinstance(uploaded, str):
                data = uploaded.encode("utf-8")
            elif uploaded is not None and hasattr(uploaded, "read"):
                data = await uploaded.read()
            else:
                data = b""
        else:
            data = await request.body()
        if station_raw in (None, ""):
            station_raw = request.query_params.get("station_id")
        if hour_raw in (None, ""):
            hour_raw = request.query_params.get("hour")
        return station_raw, hour_raw, data or b""

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(os.path.join(STATIC_DIR, "index.html"))

    @app.post("/api/upload")
    async def upload(request: Request, file: UploadFile | None = File(default=None)):
        """接收 NDJSON：multipart 文件字段 `file`，或直接 POST 裸文本。"""
        if file is not None:
            raw = await file.read()
        else:
            raw = await request.body()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="内容必须是 UTF-8 编码的 NDJSON")
        if not text.strip():
            raise HTTPException(status_code=400, detail="空的 NDJSON")
        # 串行化写入；单事务保证一批上传要么全部落库要么全部回滚
        with state_lock:
            summary = core.ingest_ndjson(request.app.state.db, text)
        return JSONResponse(summary)

    @app.get("/api/messages")
    async def messages(request: Request):
        with state_lock:
            return {"messages": core.list_messages(request.app.state.db)}

    @app.get("/api/observations")
    async def observations(request: Request, station_id: int | None = None,
                           limit: int = 200):
        limit = max(1, min(limit, 5000))
        sql = ("SELECT station_id, observed_at, level_m, boot_id, msg_id"
               " FROM observations")
        args: list = []
        if station_id is not None:
            sql += " WHERE station_id=?"
            args.append(station_id)
        sql += " ORDER BY station_id, observed_at LIMIT ?"
        args.append(limit)
        with state_lock:
            rows = request.app.state.db.execute(sql, args).fetchall()
        return {"observations": [dict(r) for r in rows]}

    @app.get("/api/observations.csv")
    async def observations_csv(request: Request, station_id: int | None = None):
        sql = "SELECT station_id, observed_at, level_m FROM observations"
        args: list = []
        if station_id is not None:
            sql += " WHERE station_id=?"
            args.append(station_id)
        sql += " ORDER BY station_id, observed_at"
        with state_lock:
            rows = request.app.state.db.execute(sql, args).fetchall()
        lines = ["station_id,observed_at,level_m"]
        lines += [f"{r['station_id']},{r['observed_at']},{r['level_m']}" for r in rows]
        name = f"observations_{station_id}.csv" if station_id is not None else "observations.csv"
        return _csv_response("\n".join(lines) + "\n", name)

    @app.post("/api/baselines")
    async def upload_baseline(request: Request):
        """上传指定站号与 UTC 小时的分钟基准 CSV；成功即整小时替换旧基准。"""
        station_raw, hour_raw, data = await _read_baseline_form(request)
        if station_raw in (None, ""):
            raise HTTPException(status_code=400, detail="缺少 station_id")
        if hour_raw in (None, ""):
            raise HTTPException(status_code=400, detail="缺少 hour（UTC 整点）")
        try:
            station_id = int(str(station_raw).strip(), 10)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="station_id 必须是整数")
        try:
            hour = verification.parse_utc_hour(str(hour_raw))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not data.strip():
            raise HTTPException(status_code=400, detail="基准 CSV 为空")
        try:
            rows = verification.parse_baseline_csv(data, station_id, hour)
        except ValueError as exc:
            # 解析失败 -> 旧基准保持不变
            raise HTTPException(status_code=400, detail=f"基准 CSV 不合法: {exc}")

        try:
            with state_lock:
                result = verification.replace_baseline(
                    request.app.state.db, station_id, hour, rows
                )
        except sqlite3.Error as exc:
            # 写入失败 -> 事务已回滚，旧基准保持不变
            raise HTTPException(status_code=500, detail=f"基准保存失败，已保留旧版: {exc}")
        return JSONResponse(result)

    @app.get("/api/baselines")
    async def baselines(request: Request, station_id: int | None = None):
        with state_lock:
            return {"baselines": verification.list_baselines(
                request.app.state.db, station_id)}

    @app.get("/api/verification")
    async def verification_json(request: Request, station_id: int | None = None,
                                hour: str | None = None):
        try:
            with state_lock:
                return verification.build_verification(
                    request.app.state.db, station_id=station_id, hour=hour)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/verification.csv")
    async def verification_csv(request: Request, station_id: int | None = None,
                               hour: str | None = None):
        """合格核验 CSV：只含与基准差值绝对值 ≤ 0.20 m 的已确认观测。"""
        try:
            with state_lock:
                text = verification.verification_csv(
                    request.app.state.db, station_id=station_id, hour=hour)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        parts = ["verification"]
        if station_id is not None:
            parts.append(str(station_id))
        if hour is not None:
            parts.append(verification.parse_utc_hour(hour).replace(":", ""))
        return _csv_response(text, "_".join(parts) + ".csv")

    @app.get("/api/verification/exceptions.csv")
    async def verification_exceptions_csv(request: Request, station_id: int | None = None,
                                          hour: str | None = None):
        """缺基准与超差明细：分钟、两侧数值、差值、原报文行。"""
        try:
            with state_lock:
                report = verification.build_verification(
                    request.app.state.db, station_id=station_id, hour=hour)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["station_id", "observed_at", "status", "observed_level_m",
                    "baseline_level_m", "diff_m", "boot_id", "msg_id", "raw_line"])
        for r in report["results"]:
            if r["status"] == "pass":
                continue
            w.writerow([
                r["station_id"], r["observed_at"], r["status"],
                r["observed_level_m"],
                "" if r["baseline_level_m"] is None else r["baseline_level_m"],
                "" if r["diff_m"] is None else r["diff_m"],
                r["boot_id"], r["msg_id"], r["raw_line"] or "",
            ])
        parts = ["verification_exceptions"]
        if station_id is not None:
            parts.append(str(station_id))
        return _csv_response(buf.getvalue(), "_".join(parts) + ".csv")

    @app.delete("/api/messages/{station_id}/{boot_id}/{msg_id}")
    async def delete_message(request: Request, station_id: int, boot_id: int, msg_id: int):
        with state_lock:
            removed = core.reset_message(request.app.state.db, station_id, boot_id, msg_id)
        if not removed:
            raise HTTPException(status_code=404, detail="报文不存在")
        return {"deleted": True}

    return app


# uvicorn app.main:app 使用的默认实例；数据库路径可用 DB_PATH 覆盖
app = create_app(os.environ.get("DB_PATH", os.path.join("data", "tide.db")))
