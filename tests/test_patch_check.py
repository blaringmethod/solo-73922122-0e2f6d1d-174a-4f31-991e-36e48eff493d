"""补丁核查：两库独立副本执行、提交后外键、验证脚本隔离。"""
from __future__ import annotations

import sqlite3

import pytest

from app.errors import ScriptRejected
from app.jobs import Job, PatchCheck
from app.patch_check import can_patch_check, run_patch_check
from app.reducer import reduce_database
from app.runner import confirm_baseline, make_canonical_copy, run_on_copy
from app.script_guard import parse_patch_script
from app.validation import validate_database


def _prepare_reduced_job(tmp_path, source_db, script, *, kind="reduced"):
    """构造与真实流水线一致的已完成任务目录。"""
    job = Job(id="job", dir=tmp_path)
    canonical = tmp_path / "canonical.db"
    make_canonical_copy(str(source_db), str(canonical))
    schema = validate_database(str(source_db))
    baseline = confirm_baseline(str(canonical), script, str(tmp_path))
    reduced = tmp_path / "reduced.db"
    outcome = reduce_database(
        str(canonical), script, str(tmp_path), schema, baseline, str(reduced)
    )
    (tmp_path / "original_script.sql").write_text(script, encoding="utf-8")
    job.status = "done"
    job.result = {"kind": kind, "download_available": True}
    return job, outcome, canonical, reduced


UNIQUE_SQL = "INSERT INTO dept (id, name) VALUES (1, 'Platform');"
FIXED_PATCH = (
    "BEGIN;\nDELETE FROM project;\nDELETE FROM employee;\nDELETE FROM dept;\nCOMMIT;\n"
)


def _run(job, patch, name="check"):
    check = PatchCheck(id=name)
    run_patch_check(job, check, patch.encode("utf-8"))
    return check


def test_patch_available_when_both_databases_pass(tmp_path, cyclic_db):
    job, _outcome, _canonical, _reduced = _prepare_reduced_job(
        tmp_path, cyclic_db, UNIQUE_SQL
    )
    check = _run(job, FIXED_PATCH)
    assert check.status == "done"
    assert check.result["available"] is True
    assert check.result["verdict"] == "可用"
    for kind in ("original", "reduced"):
        block = check.result[kind]
        assert block["ok"] is True
        assert block["patch"]["ok"] is True
        assert block["patch"]["committed"] is True
        assert block["foreign_keys"]["ok"] is True
        assert block["verification"]["ok"] is True
        assert block["problems"] == []


def test_patch_error_reported_per_database(tmp_path, cyclic_db):
    """同一补丁在缩减库成功、在原库失败：逐库独立报告，整体不可用。"""
    job, _outcome, _canonical, _reduced = _prepare_reduced_job(
        tmp_path, cyclic_db, UNIQUE_SQL
    )
    # 原库中 employee 10/11 与 project 100 仍引用 dept 1，直接删除触发外键错误；
    # 缩减库只剩 dept 1 这一行，删除后原脚本不再报错，核查通过。
    check = _run(job, "DELETE FROM dept WHERE id = 1;")
    assert check.result["available"] is False
    assert check.result["original"]["ok"] is False
    assert any("补丁错误" in p for p in check.result["original"]["problems"])
    assert check.result["original"]["patch"]["error_name"] == (
        "SQLITE_CONSTRAINT_FOREIGNKEY"
    )
    assert check.result["reduced"]["ok"] is True
    assert check.result["problems"][0].startswith("原库：")


def test_commit_then_error_still_fails(tmp_path, cyclic_db):
    """补丁先提交成功写入、后报错，仍判失败（committed=True 也不可用）。"""
    job, _outcome, _canonical, _reduced = _prepare_reduced_job(
        tmp_path, cyclic_db, UNIQUE_SQL
    )
    patch = (
        "UPDATE dept SET name = 'fixed' WHERE id = 1;\n"
        "INSERT INTO dept (id, name) VALUES (1, 'boom');\n"
    )
    check = _run(job, patch)
    assert check.result["available"] is False
    for kind in ("original", "reduced"):
        block = check.result[kind]
        assert block["ok"] is False
        assert block["patch"]["ok"] is False
        assert block["patch"]["committed"] is True  # 第一条修改已经写入
        assert block["patch"]["error_name"] == "SQLITE_CONSTRAINT_PRIMARYKEY"


def test_patch_fk_violation_detected_after_commit(tmp_path, cyclic_db):
    """补丁临时关闭 foreign_keys 并留下无效引用：提交后外键检查必须发现。"""
    job, _outcome, _canonical, _reduced = _prepare_reduced_job(
        tmp_path, cyclic_db, UNIQUE_SQL
    )
    patch = "PRAGMA foreign_keys=OFF; DELETE FROM dept WHERE id = 2;"
    check = _run(job, patch)
    assert check.result["available"] is False
    block = check.result["original"]
    assert block["patch"]["ok"] is True
    assert block["foreign_keys"]["ok"] is False
    assert any("外键问题" in p for p in block["problems"])
    # 缩减库本就没有 dept 2，补丁与外键检查都通过，但原脚本仍会报唯一约束错误
    reduced_block = check.result["reduced"]
    assert reduced_block["patch"]["ok"] is True
    assert reduced_block["foreign_keys"]["ok"] is True
    assert any("验证错误" in p for p in reduced_block["problems"])


def test_uncommitted_transaction_is_failure(tmp_path, cyclic_db):
    job, _outcome, _canonical, _reduced = _prepare_reduced_job(
        tmp_path, cyclic_db, UNIQUE_SQL
    )
    check = _run(job, "BEGIN; UPDATE dept SET name = 'x';")
    assert check.result["available"] is False
    for kind in ("original", "reduced"):
        assert "未提交事务" in check.result[kind]["problems"][0]


def test_verification_write_then_error_is_isolated(
    tmp_path, self_ref_db, cyclic_db
):
    """验证脚本（原故障脚本）写入后报错：另一库仍在核查，原件与下载不受影响。"""
    script = (
        "BEGIN; DELETE FROM node WHERE id = 3; COMMIT; "
        "INSERT INTO node (id, parent_id, label) VALUES (9, 1, NULL);"
    )
    job, outcome, canonical, reduced = _prepare_reduced_job(
        tmp_path, self_ref_db, script
    )
    original_before = canonical.read_bytes()
    reduced_before = reduced.read_bytes()
    conn = sqlite3.connect(str(canonical))
    count_before = conn.execute("SELECT COUNT(*) FROM node").fetchone()[0]
    conn.close()

    check = _run(job, "DELETE FROM node WHERE id = 3;")
    assert check.status == "done"
    assert check.result["available"] is False
    for kind in ("original", "reduced"):
        block = check.result[kind]
        assert block["patch"]["ok"] is True
        assert block["foreign_keys"]["ok"] is True
        # 验证脚本确实先提交了 DELETE 再触发 NOT NULL 错误
        assert block["verification"]["ok"] is False
        assert block["verification"]["error_name"] == "SQLITE_CONSTRAINT_NOTNULL"

    # 原件与缩减库逐字节不变，原脚本写入的修改只落在临时副本
    assert canonical.read_bytes() == original_before
    assert reduced.read_bytes() == reduced_before
    conn = sqlite3.connect(str(canonical))
    assert conn.execute("SELECT COUNT(*) FROM node").fetchone()[0] == count_before
    assert conn.execute("SELECT label FROM node WHERE id=3").fetchone()[0] == "b"
    conn.close()
    # 核查工作目录已清理
    assert not (tmp_path / "patch_checks").exists()


def test_one_database_crash_does_not_block_the_other(
    monkeypatch, tmp_path, cyclic_db
):
    job, _outcome, _canonical, _reduced = _prepare_reduced_job(
        tmp_path, cyclic_db, UNIQUE_SQL
    )
    from app import patch_check as pc

    real = pc.run_patch_on_copy

    def flaky(source, dest, script, work_dir):
        if source.endswith("canonical.db"):
            raise RuntimeError("原库副本读取失败（模拟）")
        return real(source, dest, script, work_dir)

    monkeypatch.setattr(pc, "run_patch_on_copy", flaky)
    check = _run(job, FIXED_PATCH)
    assert check.status == "done"
    assert check.result["available"] is False
    assert check.result["original"]["ok"] is False
    assert any("核查内部错误" in p for p in check.result["original"]["problems"])
    # 另一库照常完成核查
    assert check.result["reduced"]["ok"] is True


def test_can_patch_check_requires_reduced_job(tmp_path):
    job = Job(id="job", dir=tmp_path)
    job.status = "done"
    job.result = {"kind": "success_no_reduction", "download_available": False}
    assert can_patch_check(job) is False
    job.result = {"kind": "reduced", "download_available": True}
    assert can_patch_check(job) is True
    job.status = "running"
    assert can_patch_check(job) is False
    job.status = "done"
    job.result = {"kind": "reduced", "download_available": True}
    # 任务文件中缺少原始故障脚本时同样拒绝核查
    job.result = {"kind": "success_no_reduction", "download_available": False}
    with pytest.raises(ScriptRejected):
        run_patch_check(job, PatchCheck(id="c"), b"SELECT 1;")


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE x (id INTEGER PRIMARY KEY);",
        "DROP TABLE dept;",
        "ALTER TABLE dept ADD COLUMN z TEXT;",
        "VACUUM;",
        "REINDEX;",
        "ANALYZE;",
    ],
)
def test_patch_guard_rejects_schema_changes(sql):
    with pytest.raises(ScriptRejected):
        parse_patch_script(sql.encode())


def test_patch_guard_allows_data_and_keyword_literals():
    text = parse_patch_script(
        b"-- CREATE is in a comment\n"
        b"UPDATE dept SET name = 'DROP TABLE dept' WHERE id = 1;"
    )
    assert "UPDATE dept" in text
    assert parse_patch_script(b"BEGIN; DELETE FROM dept WHERE id=2; COMMIT;")
    # 普通故障脚本仍然只受原有静态规则约束
    from app.script_guard import parse_script

    assert "DROP TABLE x;" in parse_script(b"DROP TABLE x;")


def test_verification_runs_from_committed_data(tmp_path, cyclic_db):
    """验证脚本在补丁提交后的全新副本上运行，可在原库上确认修复。"""
    job, _outcome, _canonical, _reduced = _prepare_reduced_job(
        tmp_path, cyclic_db, UNIQUE_SQL
    )
    check = _run(job, FIXED_PATCH)
    original_block = check.result["original"]
    assert original_block["verification"] == {
        "ok": True,
        "error_code": None,
        "error_name": None,
        "error_message": None,
    }
    # 核查结束后工作目录已清理，原库仍可直接运行原脚本复现错误
    assert not (tmp_path / "patch_checks").exists()
    replay = run_on_copy(str(tmp_path / "canonical.db"), UNIQUE_SQL, str(tmp_path))
    assert replay.error_name == "SQLITE_CONSTRAINT_PRIMARYKEY"
