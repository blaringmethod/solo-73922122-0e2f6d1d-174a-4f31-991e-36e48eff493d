"""已完成缩减任务的“补丁核查”。

入仓开发者在缩减库上找到了修补 SQL，但还需要确认它对原库同样有效。
核查对**原库副本**与**缩减库副本**分别执行：

1. 在独立副本上运行补丁脚本 —— 必须每条语句都成功，且结束时没有未提交事务；
   补丁先 COMMIT 再报错同样判失败；
2. 关闭连接后重新打开该副本，用 ``PRAGMA foreign_key_check`` 检查实际已提交
   数据的外键有效性；
3. 再从该副本复制一份全新临时文件，重新运行原始故障脚本 —— 只有脚本不再失败
   （即补丁确实避免了原错误）才算通过；脚本写入后再报错也只影响这份临时副本。

两库互不影响：任一步骤的失败只记录在该库结论中，不会中断另一库的核查，
也不会打开原件或缩减下载库的写句柄。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .errors import JobError, ScriptRejected
from .jobs import Job, PatchCheck
from .runner import (
    PatchResult,
    TrialTimeout,
    read_effective_foreign_key_violations,
    run_on_copy,
    run_patch_on_copy,
)
from .script_guard import parse_patch_script

ORIGINAL = "original"
REDUCED = "reduced"


def can_patch_check(job: Job) -> bool:
    return (
        job.status == "done"
        and job.result.get("kind") == "reduced"
        and bool(job.result.get("download_available"))
    )


def _check_source(job: Job, kind: str) -> Path:
    name = "canonical.db" if kind == ORIGINAL else "reduced.db"
    path = Path(job.dir) / name
    if not path.exists():
        raise JobError(f"{'原库' if kind == ORIGINAL else '缩减库'}文件已被清理，无法核查。")
    return path


def _format_patch_problem(patch: PatchResult) -> str | None:
    if patch.timed_out:
        return f"补丁错误：{patch.error_message}"
    if not patch.ok:
        detail = patch.error_message or "未知错误"
        name = patch.error_name or "SQLITE_ERROR"
        code = "" if patch.error_code is None else f"（{patch.error_code}）"
        return f"补丁错误：{name}{code} {detail}"
    if not patch.committed:
        return "补丁错误：脚本结束时仍有未提交事务（BEGIN 后未 COMMIT）。"
    return None


def _format_fk_problem(violations: list[dict[str, object]]) -> str | None:
    if not violations:
        return None
    parts: list[str] = []
    for v in violations[:5]:
        if "integrity" in v:
            parts.append(f"完整性检查失败：{v['integrity']}")
            continue
        parts.append(
            f"表 {v.get('table')} 行 rowid={v.get('rowid')} "
            f"引用父表 {v.get('parent')} 失败"
        )
    return "外键问题：" + "；".join(parts)


def _check_one(job: Job, kind: str, patch_script: str, original_script: str,
               work_dir: Path) -> dict[str, object]:
    source = _check_source(job, kind)
    label = "原库" if kind == ORIGINAL else "缩减库"
    result: dict[str, object] = {
        "db": kind,
        "label": label,
        "ok": False,
        "patch": None,
        "foreign_keys": None,
        "verification": None,
        "problems": [],
    }
    problems: list[str] = result["problems"]  # type: ignore[assignment]

    patched = work_dir / f"{kind}_patched.db"
    patch = run_patch_on_copy(str(source), str(patched), patch_script, str(work_dir))
    result["patch"] = patch.to_dict()
    patch_problem = _format_patch_problem(patch)
    if patch_problem is not None:
        problems.append(patch_problem)
        return result

    violations = read_effective_foreign_key_violations(str(patched))
    result["foreign_keys"] = {"ok": not violations, "violations": violations}
    fk_problem = _format_fk_problem(violations)
    if fk_problem is not None:
        problems.append(fk_problem)
        return result

    # 验证总是从“已提交数据”再复制一份全新副本，脚本写入后报错也隔离在外。
    try:
        verdict = run_on_copy(str(patched), original_script, str(work_dir))
    except TrialTimeout as exc:
        result["verification"] = {"ok": False, "timed_out": True,
                                  "error_message": str(exc)}
        problems.append(f"验证错误：{exc}")
        return result

    result["verification"] = verdict.to_dict()
    if not verdict.ok:
        name = verdict.error_name or "SQLITE_ERROR"
        code = "" if verdict.error_code is None else f"（{verdict.error_code}）"
        problems.append(
            f"验证错误：重新运行原故障脚本仍失败：{name}{code} "
            f"{verdict.error_message}"
        )
        return result

    result["ok"] = True
    return result


def run_patch_check(job: Job, check: PatchCheck, raw_patch: bytes) -> None:
    check.status = "running"
    check.message = "正在校验补丁脚本…"

    patch_script = parse_patch_script(raw_patch)

    if not can_patch_check(job):
        raise ScriptRejected("只有缩减成功的任务才能做补丁核查。")
    script_path = Path(job.dir) / "original_script.sql"
    if not script_path.exists():
        raise JobError("原始故障脚本已被清理，无法核查。")
    original_script = script_path.read_text(encoding="utf-8")

    check.message = "正在原库副本上执行补丁…"
    work_dir = Path(job.dir) / "patch_checks" / check.id
    work_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict[str, object]] = {}
    try:
        # 两库完全独立：先原库，再缩减库；任一库的异常都不影响另一库。
        for kind, note in (
            (ORIGINAL, "正在原库副本上执行补丁…"),
            (REDUCED, "正在缩减库副本上执行补丁…"),
        ):
            check.message = note
            try:
                results[kind] = _check_one(
                    job, kind, patch_script, original_script, work_dir
                )
            except Exception as exc:  # noqa: BLE001 - 单库故障不影响另一库
                results[kind] = {
                    "db": kind,
                    "label": "原库" if kind == ORIGINAL else "缩减库",
                    "ok": False,
                    "patch": None,
                    "foreign_keys": None,
                    "verification": None,
                    "problems": [f"核查内部错误：{exc}"],
                }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            work_dir.parent.rmdir()  # 空的 patch_checks/ 目录
        except OSError:
            pass

    available = all(bool(r.get("ok")) for r in results.values()) and len(results) == 2
    check.result = {
        "available": available,
        "verdict": "可用" if available else "不可用",
        "original": results[ORIGINAL],
        "reduced": results[REDUCED],
        "problems": [
            f"{r['label']}：{p}"
            for r in (results[ORIGINAL], results[REDUCED])
            for p in r.get("problems", [])  # type: ignore[union-attr]
        ],
    }
    check.status = "done"
    check.message = (
        "补丁在原库与缩减库上均通过：补丁成功、外键有效、原故障脚本不再报错。"
        if available
        else "补丁未通过核查，详见逐库结论。"
    )
