"""FastAPI 入口：上传、轮询任务、查看结果、下载复现库。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .errors import JobError, ScriptRejected, ValidationError
from .jobs import registry
from .patch_check import can_patch_check, run_patch_check
from .samples import ensure_samples
from .script_guard import parse_patch_script
from .service import run_pipeline

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_samples(STATIC_DIR)
    registry.cleanup_expired()

    async def _janitor() -> None:
        while True:
            await asyncio.sleep(60)
            registry.cleanup_expired()

    janitor = asyncio.create_task(_janitor())
    try:
        yield
    finally:
        janitor.cancel()


app = FastAPI(title="SQLite 复现库缩减服务", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/samples")
def list_samples() -> JSONResponse:
    return JSONResponse(ensure_samples(STATIC_DIR))


@app.post("/api/jobs")
async def create_job(
    db: UploadFile = File(..., description="满足约束的 SQLite 数据库"),
    sql: UploadFile | None = File(None, description="确定性 SQL 脚本文件"),
    script_text: str = Form("", description="或直接粘贴 SQL 脚本"),
) -> JSONResponse:
    raw_db = await db.read()
    if len(raw_db) == 0:
        raise ValidationError("数据库文件为空。")
    if len(raw_db) > config.MAX_DB_BYTES:
        raise ValidationError(
            f"数据库大小 {len(raw_db)} 字节，超过上限 {config.MAX_DB_BYTES} 字节。"
        )
    if not raw_db.startswith(b"SQLite format 3\x00"):
        raise ValidationError("文件不是合法的 SQLite 数据库（魔数校验失败）。")

    if sql is not None:
        raw_script = await sql.read()
    else:
        raw_script = script_text.encode("utf-8")
    if not raw_script.strip():
        raise ValidationError("请上传或粘贴 SQL 脚本。")

    job = registry.create()
    upload_path = job.dir / "upload.db"
    upload_path.write_bytes(raw_db)
    # 原件落盘后只读保存，任何试跑都只接触副本
    Path(upload_path).chmod(0o444)

    registry.run_in_background(job, run_pipeline, str(upload_path), raw_script)
    return JSONResponse({"id": job.id, "status": job.status}, status_code=202)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = registry.get(job_id)
    return JSONResponse(job.public_dict())


@app.get("/api/jobs/{job_id}/download")
def download_reduced(job_id: str) -> FileResponse:
    job = registry.get(job_id)
    if job.status != "done" or not job.result.get("download_available"):
        raise JobError("缩减结果尚不可下载。")
    reduced = job.dir / "reduced.db"
    if not reduced.exists():
        raise JobError("缩减库文件已被清理。")
    return FileResponse(
        reduced,
        media_type="application/x-sqlite3",
        filename="repro.db",
    )


@app.post("/api/jobs/{job_id}/patch-checks")
async def create_patch_check(
    job_id: str,
    sql: UploadFile | None = File(None, description="补丁 SQL 脚本文件"),
    script_text: str = Form("", description="或直接粘贴补丁 SQL 脚本"),
) -> JSONResponse:
    job = registry.get(job_id)
    if not can_patch_check(job):
        raise JobError("只有缩减成功的任务才能做补丁核查。")
    raw_patch = await sql.read() if sql is not None else script_text.encode("utf-8")
    if not raw_patch.strip():
        raise ValidationError("请上传或粘贴补丁 SQL 脚本。")
    if len(raw_patch) > config.MAX_SCRIPT_BYTES:
        raise ValidationError(
            f"补丁脚本大小 {len(raw_patch)} 字节，"
            f"超过上限 {config.MAX_SCRIPT_BYTES} 字节。"
        )
    # 结构性补丁在入队前直接拒绝，避免无意义的后台核查。
    parse_patch_script(raw_patch)

    job, check = registry.create_patch_check(job_id)
    registry.run_check_in_background(job, check, run_patch_check, raw_patch)
    return JSONResponse({"id": check.id, "status": check.status}, status_code=202)


@app.get("/api/jobs/{job_id}/patch-checks/{check_id}")
def get_patch_check(job_id: str, check_id: str) -> JSONResponse:
    check = registry.get_patch_check(job_id, check_id)
    return JSONResponse(check.public_dict())


@app.exception_handler(ValidationError)
@app.exception_handler(ScriptRejected)
@app.exception_handler(JobError)
def _handle_bad_request(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})
