"""新鲜读取（read-fresh）核心逻辑。

设计目标：证明"这次读到的内容"和"读取那一刻磁盘上的内容"是同一个版本，
而不是被并发写入穿插污染的中间状态。

做法：stat → read → stat 的三明治校验。
- 读取前采集一次 stat（size/mtime/inode）。
- 立即读取文件内容并对实际读取到的字节计算 SHA-256（哈希与读取必须发生在同一轮，
  不能先读文件、再对另一个时刻的文件单独求哈希）。
- 读取后再次采集 stat，与读取前对比。
- 前后 stat 一致 → 本轮读取视为稳定，返回内容与哈希。
- 前后 stat 不一致 → 本轮不稳定（读取期间文件被修改），短暂退避后重试；
  达到重试上限仍不稳定，返回冲突，调用方不得使用这次的内容作为结论依据。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path

from agent_fs.fingerprint import hash_bytes, stat_file

DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 0.05


@dataclass(frozen=True)
class ReadFreshResult:
    stable: bool
    path: str
    content: str | None
    sha256: str | None
    size: int | None
    mtime_ns: int | None
    attempts: int
    encoding: str
    truncated: bool
    reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def read_fresh(
    path: Path,
    offset: int | None = None,
    limit: int | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    encoding: str = "utf-8",
) -> ReadFreshResult:
    """执行一次新鲜读取，必要时重试，返回稳定读取结果或冲突标记。

    offset/limit 按「行」计算，语义与常见文件读取工具一致，方便 Agent 只读取
    文件的某一段而不必载入全文件。
    """
    if not path.is_file():
        return ReadFreshResult(
            stable=False,
            path=str(path),
            content=None,
            sha256=None,
            size=None,
            mtime_ns=None,
            attempts=0,
            encoding=encoding,
            truncated=False,
            reason="file_not_found",
        )

    if (offset is not None and offset < 0) or (limit is not None and limit < 0):
        return ReadFreshResult(
            stable=False,
            path=str(path),
            content=None,
            sha256=None,
            size=None,
            mtime_ns=None,
            attempts=0,
            encoding=encoding,
            truncated=False,
            reason="invalid_offset_or_limit",
        )

    attempts = 0
    last_reason = "unknown"
    while attempts < max_retries:
        attempts += 1
        try:
            before = stat_file(path)
            raw_bytes = path.read_bytes()
            after = stat_file(path)
        except FileNotFoundError:
            last_reason = "file_not_found"
            time.sleep(backoff_seconds)
            continue

        if not before.matches(after):
            last_reason = "changed_during_read"
            time.sleep(backoff_seconds)
            continue

        digest = hash_bytes(raw_bytes)
        try:
            text = raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            return ReadFreshResult(
                stable=True,
                path=str(path),
                content=None,
                sha256=digest,
                size=after.size,
                mtime_ns=after.mtime_ns,
                attempts=attempts,
                encoding=encoding,
                truncated=False,
                reason="binary_or_undecodable",
            )

        truncated = False
        if offset is not None or limit is not None:
            lines = text.splitlines(keepends=True)
            start = offset or 0
            end = start + limit if limit is not None else None
            selected = lines[start:end]
            truncated = len(selected) != len(lines)
            text = "".join(selected)

        return ReadFreshResult(
            stable=True,
            path=str(path),
            content=text,
            sha256=digest,
            size=after.size,
            mtime_ns=after.mtime_ns,
            attempts=attempts,
            encoding=encoding,
            truncated=truncated,
        )

    return ReadFreshResult(
        stable=False,
        path=str(path),
        content=None,
        sha256=None,
        size=None,
        mtime_ns=None,
        attempts=attempts,
        encoding=encoding,
        truncated=False,
        reason=last_reason,
    )
