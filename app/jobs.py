"""后台缩减任务管理。"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .errors import JobError


@dataclass
class PatchCheck:
    """针对某个已完成缩减任务的补丁核查。"""

    id: str
    status: str = "queued"  # queued / running / done / error
    created_at: float = field(default_factory=time.time)
    message: str = ""
    result: dict[str, object] = field(default_factory=dict)

    def public_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "message": self.message,
        }
        data.update(self.result)
        return data


@dataclass
class Job:
    id: str
    status: str = "queued"  # queued / running / done / error
    created_at: float = field(default_factory=time.time)
    message: str = ""
    result: dict[str, object] = field(default_factory=dict)
    dir: Path = field(default_factory=lambda: Path("."))
    patch_checks: dict[str, PatchCheck] = field(default_factory=dict)

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at > config.JOB_TTL_SECONDS

    def public_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "created_at": self.created_at,
        }
        data.update(self.result)
        return data


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_JOBS)

    def create(self) -> Job:
        job_id = uuid.uuid4().hex
        job_dir = config.DATA_DIR / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = Job(id=job_id, dir=job_dir)
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobError("任务不存在或已过期。")
        return job

    def create_patch_check(self, job_id: str) -> tuple[Job, PatchCheck]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobError("任务不存在或已过期。")
            check = PatchCheck(id=uuid.uuid4().hex)
            job.patch_checks[check.id] = check
        return job, check

    def get_patch_check(self, job_id: str, check_id: str) -> PatchCheck:
        job = self.get(job_id)
        with self._lock:
            check = job.patch_checks.get(check_id)
        if check is None:
            raise JobError("补丁核查不存在或已过期。")
        return check

    def run_in_background(self, job: Job, target, *args) -> None:
        def _runner() -> None:
            with self._slots:
                try:
                    target(job, *args)
                except Exception as exc:  # noqa: BLE001 - 异常要回传页面
                    job.status = "error"
                    job.message = str(exc)

        threading.Thread(target=_runner, name=f"job-{job.id}", daemon=True).start()

    def run_check_in_background(self, job: Job, check: PatchCheck,
                                target, *args) -> None:
        def _runner() -> None:
            with self._slots:
                try:
                    target(job, check, *args)
                except Exception as exc:  # noqa: BLE001 - 异常要回传页面
                    check.status = "error"
                    check.message = str(exc)

        threading.Thread(
            target=_runner, name=f"patch-{check.id}", daemon=True
        ).start()

    def cleanup_expired(self) -> None:
        now = time.time()
        with self._lock:
            expired = [j for j in self._jobs.values() if j.is_expired(now)]
            for job in expired:
                self._jobs.pop(job.id, None)
        for job in expired:
            # 正在运行的任务目录稍后随容器回收；这里只清理过期记录对应的文件
            if job.status in ("done", "error"):
                _remove_tree(job.dir)


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


registry = JobRegistry()
