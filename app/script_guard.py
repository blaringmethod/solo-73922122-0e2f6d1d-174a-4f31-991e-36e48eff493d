"""SQL 脚本的静态安全检查。

运行期还会在每个连接上安装 authorizer 做强制拦截（见 runner.py），
这里的静态检查用于给出更友好的错误信息，并提前拒绝明显违规的脚本。
"""
from __future__ import annotations

import re

from . import config
from .errors import ScriptRejected

# 禁止出现的 SQL 关键字/内置函数。authorizer 是最终防线。
_BANNED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bATTACH\b", re.IGNORECASE), "禁止 ATTACH 附加任何数据库（包括内存库）。"),
    (re.compile(r"\bDETACH\b", re.IGNORECASE), "禁止 DETACH 操作。"),
    (
        re.compile(r"\bPRAGMA\s+(?!foreign_keys\b)", re.IGNORECASE),
        "仅允许 PRAGMA foreign_keys，禁止其它 PRAGMA。",
    ),
    (
        re.compile(r"\bload_extension\s*\(", re.IGNORECASE),
        "禁止 load_extension 加载扩展。",
    ),
    (
        re.compile(r"\bVACUUM\s+INTO\b", re.IGNORECASE),
        "禁止 VACUUM INTO 写外部文件。",
    ),
    (
        re.compile(r"\b(?:readfile|writefile)\s*\(", re.IGNORECASE),
        "禁止 readfile/writefile 访问外部文件。",
    ),
    (re.compile(r"\.import\b", re.IGNORECASE), "禁止 .import 等外部文件操作。"),
)


def parse_script(raw: bytes) -> str:
    if len(raw) == 0:
        raise ScriptRejected("SQL 脚本为空。")
    if len(raw) > config.MAX_SCRIPT_BYTES:
        raise ScriptRejected(
            f"脚本大小 {len(raw)} 字节，超过上限 {config.MAX_SCRIPT_BYTES} 字节。"
        )
    if b"\x00" in raw:
        raise ScriptRejected("脚本包含 NUL 字节。")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScriptRejected(f"脚本必须是 UTF-8 文本：{exc}") from exc
    if not text.strip():
        raise ScriptRejected("SQL 脚本为空。")
    for pattern, message in _BANNED_PATTERNS:
        if pattern.search(text):
            raise ScriptRejected(message)
    return text


# 补丁脚本只允许修改原有表的数据或控制事务，禁止一切结构变更。
_PATCH_SCHEMA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bCREATE\b", re.IGNORECASE),
     "补丁只能修改原有表数据或控制事务，禁止 CREATE 结构变更。"),
    (re.compile(r"\bDROP\b", re.IGNORECASE),
     "补丁只能修改原有表数据或控制事务，禁止 DROP 结构变更。"),
    (re.compile(r"\bALTER\b", re.IGNORECASE),
     "补丁只能修改原有表数据或控制事务，禁止 ALTER 结构变更。"),
    (re.compile(r"\bVACUUM\b", re.IGNORECASE),
     "补丁只能修改原有表数据或控制事务，禁止 VACUUM。"),
    (re.compile(r"\bREINDEX\b", re.IGNORECASE),
     "补丁只能修改原有表数据或控制事务，禁止 REINDEX。"),
    (re.compile(r"\bANALYZE\b", re.IGNORECASE),
     "补丁只能修改原有表数据或控制事务，禁止 ANALYZE。"),
    (re.compile(r"\bTRUNCATE\b", re.IGNORECASE),
     "补丁只能修改原有表数据或控制事务，禁止 TRUNCATE。"),
)


def _strip_sql_noise(sql: str) -> str:
    """去掉注释与字符串/标识符字面量，避免把数据内容误判为 SQL 关键字。

    被删除的片段用空白替换，防止 ``CRE/**/ATE`` 这类由注释分隔的文本被拼接
    成关键字造成误判；authorizer 仍是运行期最终防线。
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            i += 2
            while i < n and sql[i] not in "\r\n":
                i += 1
            out.append(" ")
        elif ch == "/" and i + 1 < n and sql[i + 1] == "*":
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            out.append(" ")
        elif ch in ("'", '"', "`", "["):
            close = "]" if ch == "[" else ch
            i += 1
            while i < n:
                if sql[i] == close:
                    if ch != "[" and i + 1 < n and sql[i + 1] == close:
                        i += 2  # SQL 中以双写引号转义
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_patch_script(raw: bytes) -> str:
    """补丁脚本额外限制：不允许任何结构变更，只能动数据/事务。"""
    text = parse_script(raw)
    code_only = _strip_sql_noise(text)
    for pattern, message in _PATCH_SCHEMA_PATTERNS:
        if pattern.search(code_only):
            raise ScriptRejected(message)
    return text
