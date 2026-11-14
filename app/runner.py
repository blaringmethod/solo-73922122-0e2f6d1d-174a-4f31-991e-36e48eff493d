"""在独立数据库副本上执行 SQL 脚本并捕获错误。

每一次试跑都：
1. 从规范原件复制出一个全新临时文件；
2. 用带 authorizer 的连接执行脚本（foreign_keys 强制开启）；
3. 捕获 SQLite 扩展错误码、错误名与完整错误文本；
4. 关闭连接并删除该副本及其全部 SQLite 侧车文件（-wal/-shm/-journal）。

因此脚本即便先 COMMIT 再失败，修改也只落在临时副本上，
不会污染原件、后续试跑或最终下载库。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass

from . import config
from .errors import ScriptRejected, ValidationError


@dataclass(frozen=True)
class ScriptResult:
    ok: bool
    error_code: int | None = None
    error_name: str | None = None
    error_message: str | None = None

    def same_error(self, other: "ScriptResult") -> bool:
        """错误码与完整错误文本完全一致才算复现。"""
        if self.ok or other.ok:
            return False
        return (
            self.error_code == other.error_code
            and self.error_message == other.error_message
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "error_code": self.error_code,
            "error_name": self.error_name,
            "error_message": self.error_message,
        }


class TrialTimeout(Exception):
    """单次试跑超过时限。"""


def _authorizer(action: int, arg1: str | None, arg2: str | None,
                db_name: str | None, trigger: str | None) -> int:
    if action == sqlite3.SQLITE_ATTACH or action == sqlite3.SQLITE_DETACH:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        if str(arg1).lower() == "foreign_keys":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and str(arg1).lower() in (
        "load_extension",
        "readfile",
        "writefile",
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _remove_sidecars(db_path: str) -> None:
    """只删除 SQLite 侧车文件，保留主库文件本身。"""
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.unlink(db_path + suffix)
        except FileNotFoundError:
            pass


def make_canonical_copy(source_path: str, dest_path: str) -> None:
    """生成规范原件：普通 DELETE 日志模式，无侧车文件。"""
    shutil.copyfile(source_path, dest_path)
    conn = sqlite3.connect(dest_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        # 完整性检查，损坏库直接拒绝
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            raise ValidationError(f"数据库完整性检查失败：{verdict}")
        conn.commit()
    finally:
        conn.close()
    _remove_sidecars(dest_path)


def run_on_copy(canonical_path: str, script: str, work_dir: str) -> ScriptResult:
    """在全新临时副本上执行脚本，返回结构化结果。"""
    fd, copy_path = tempfile.mkstemp(prefix="trial_", suffix=".db", dir=work_dir)
    os.close(fd)
    shutil.copyfile(canonical_path, copy_path)

    result_box: dict[str, ScriptResult] = {}
    error_box: dict[str, BaseException] = {}

    def _worker() -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(copy_path, timeout=5.0)
            conn.set_authorizer(_authorizer)
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                conn.executescript(script)
                result_box["result"] = ScriptResult(ok=True)
            except sqlite3.Error as exc:
                result_box["result"] = ScriptResult(
                    ok=False,
                    error_code=exc.sqlite_errorcode,
                    error_name=exc.sqlite_errorname,
                    error_message=str(exc),
                )
        except BaseException as exc:  # noqa: BLE001 - 回传线程内任何异常
            error_box["error"] = exc
        finally:
            if conn is not None:
                conn.close()

    thread = threading.Thread(target=_worker, name="sqlite-trial", daemon=True)
    thread.start()
    thread.join(config.TRIAL_TIMEOUT_SECONDS)

    timed_out = thread.is_alive()
    try:
        if timed_out:
            raise TrialTimeout(
                f"单次试跑超过 {config.TRIAL_TIMEOUT_SECONDS} 秒，已放弃。"
            )
        if "error" in error_box:
            raise error_box["error"]
        return result_box["result"]
    finally:
        # 即便工作线程仍持有连接（超时），也要解除文件链接，
        # 后续写入只作用于已不可达的 inode，不会污染工作目录。
        _remove_sidecars(copy_path)
        try:
            os.unlink(copy_path)
        except FileNotFoundError:
            pass


def confirm_baseline(canonical_path: str, script: str, work_dir: str) -> ScriptResult:
    """在两个独立副本上各跑一次：确认失败且错误完全一致（确定性）。"""
    first = run_on_copy(canonical_path, script, work_dir)
    if first.ok:
        return first
    second = run_on_copy(canonical_path, script, work_dir)
    if second.ok or not first.same_error(second):
        raise ScriptRejected(
            "脚本在两个独立副本上的结果不一致，无法保证确定性，拒绝缩减。"
        )
    return first


@dataclass(frozen=True)
class PatchResult:
    """补丁脚本在单个独立副本上的执行结果。

    * ``ok`` 仅当脚本每条语句都成功；
    * ``committed`` 表示脚本结束时连接上没有未提交事务；
    * 即使脚本先提交、后报错，``ok`` 仍为 False（按失败处理）。
    """

    ok: bool
    committed: bool = False
    write_attempted: bool = False
    timed_out: bool = False
    error_code: int | None = None
    error_name: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "committed": self.committed,
            "write_attempted": self.write_attempted,
            "timed_out": self.timed_out,
            "error_code": self.error_code,
            "error_name": self.error_name,
            "error_message": self.error_message,
        }


# 补丁允许的动作：仅数据读写、查询与事务控制。结构变更一律拒绝。
_PATCH_DATA_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_RECURSIVE,
    }
)
_PATCH_WRITE_ACTIONS = frozenset(
    {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
)


def _make_patch_authorizer() -> tuple[object, list[bool]]:
    """补丁专用 authorizer，是静态检查之外的运行期最终防线。

    允许改数据、控制事务与使用普通 SQL 函数；拒绝 CREATE/DROP/ALTER/ATTACH
    等一切结构或外部操作。PRAGMA 仅允许 foreign_keys（补丁可以借此临时关闭
    外键，但提交后的外键有效性由核查统一校验）。
    """
    write_seen = [False]

    def _authorizer(action: int, arg1: str | None, arg2: str | None,
                    db_name: str | None, trigger: str | None) -> int:
        if action in _PATCH_WRITE_ACTIONS:
            if str(arg1).lower().startswith("sqlite_"):
                return sqlite3.SQLITE_DENY
            write_seen[0] = True
            return sqlite3.SQLITE_OK
        if action in _PATCH_DATA_ACTIONS:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            if str(arg1).lower() in (
                "load_extension",
                "readfile",
                "writefile",
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            if str(arg1).lower() == "foreign_keys" and db_name is None:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    return _authorizer, write_seen


def run_patch_on_copy(source_path: str, dest_path: str, script: str,
                      work_dir: str) -> PatchResult:
    """把 source 复制到 dest，在 dest 上执行补丁脚本并保留已提交的数据。

    与试跑一样在独立副本上进行；脚本结束（或报错）后连接一定关闭。
    dest 上的最终数据即“实际已提交的数据”，供调用方做外键检查与验证。
    """
    shutil.copyfile(source_path, dest_path)
    _remove_sidecars(dest_path)

    result_box: dict[str, PatchResult] = {}
    error_box: dict[str, BaseException] = {}
    authorizer, write_seen = _make_patch_authorizer()

    def _worker() -> None:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(dest_path, timeout=5.0)
            conn.set_authorizer(authorizer)
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                conn.executescript(script)
            except sqlite3.Error as exc:
                result_box["result"] = PatchResult(
                    ok=False,
                    committed=not conn.in_transaction,
                    write_attempted=write_seen[0],
                    error_code=exc.sqlite_errorcode,
                    error_name=exc.sqlite_errorname,
                    error_message=str(exc),
                )
            else:
                result_box["result"] = PatchResult(
                    ok=True,
                    committed=not conn.in_transaction,
                    write_attempted=write_seen[0],
                )
        except BaseException as exc:  # noqa: BLE001 - 回传线程内任何异常
            error_box["error"] = exc
        finally:
            if conn is not None:
                conn.close()

    thread = threading.Thread(target=_worker, name="sqlite-patch", daemon=True)
    thread.start()
    thread.join(config.TRIAL_TIMEOUT_SECONDS)

    if thread.is_alive():
        # 补丁可能仍在写 dest；调用方据此判失败，不再读取该文件。
        return PatchResult(
            ok=False,
            timed_out=True,
            error_message=(
                f"补丁执行超过 {config.TRIAL_TIMEOUT_SECONDS} 秒，已放弃。"
            ),
        )
    if "error" in error_box:
        raise error_box["error"]
    return result_box["result"]


def read_effective_foreign_key_violations(db_path: str) -> list[dict[str, object]]:
    """关闭连接后重新打开数据库，检查已提交数据的外键有效性与完整性。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        violations = [
            dict(row)
            for row in conn.execute("PRAGMA foreign_key_check").fetchall()
        ]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if integrity != "ok":
        violations.append({"integrity": integrity})
    return violations


def delete_rows(canonical_path: str, work_dir: str, kept_rows: set[tuple[str, int]],
                schema_tables: list[str], pk_column: dict[str, str],
                out_path: str) -> None:
    """生成删除指定行后的新副本，并保证外键有效。

    kept_rows 必须已经是外键闭包封闭集合；关闭 foreign_keys 批量删除，
    再开启并执行 PRAGMA foreign_key_check 验证。
    """
    shutil.copyfile(canonical_path, out_path)
    conn = sqlite3.connect(out_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in schema_tables:
            kept = sorted(pk for (t, pk) in kept_rows if t == table)
            placeholders = ",".join("?" for _ in kept)
            if kept:
                conn.execute(
                    f'DELETE FROM "{table}" WHERE "{pk_column[table]}" '
                    f"NOT IN ({placeholders})",
                    kept,
                )
            else:
                conn.execute(f'DELETE FROM "{table}"')
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            detail = ", ".join(map(str, violations[:5]))
            raise RuntimeError(f"缩减库外键校验失败（内部错误）：{detail}")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"缩减库完整性检查失败（内部错误）：{integrity}")
    finally:
        conn.close()
    _remove_sidecars(out_path)
