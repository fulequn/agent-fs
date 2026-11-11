"""文件指纹核心逻辑：size / mtime / inode / SHA-256。

跨平台说明：
- size、mtime、sha256 在 Mac / Linux / Windows 上语义一致，作为指纹判定的主要依据。
- inode（st_ino）在 Windows 上不一定具备"文件唯一标识"的语义（部分文件系统恒为 0，
  或者在文件被替换后复用旧 inode），因此 inode 仅作为 Mac/Linux 上的辅助信号，
  绝不作为跨平台判定"文件是否变化"的必要条件。

稳定性说明：
- `compute_fingerprint()` 只执行一次 stat → hash，两步之间文件仍可能被并发修改，
  产出的指纹不保证对应"单一确定版本"。它仅用于内部工具函数或明确接受这种弱保证的场景。
- 对外暴露的 `snapshot`/`verify`/`changed` 命令必须使用 `compute_stable_fingerprint()`，
  它执行 stat → hash → stat 三明治校验，前后 stat 不一致则判定为不稳定并重试，
  确保采集到的指纹对应磁盘上真实存在过的某个确定版本，而不是被并发写入穿插的中间态。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# 读取大文件时的分块大小，避免一次性把整个文件读入内存
_HASH_CHUNK_SIZE = 1024 * 1024  # 1 MiB

DEFAULT_STABLE_MAX_RETRIES = 3
DEFAULT_STABLE_BACKOFF_SECONDS = 0.05


class FileNotFoundForFingerprint(FileNotFoundError):
    """目标文件不存在或不是普通文件时抛出。"""


@dataclass(frozen=True)
class FileStat:
    """一次文件系统 stat 采样结果（不含内容哈希，读取代价极低）。"""

    size: int
    mtime_ns: int
    inode: int

    def to_dict(self) -> dict:
        return asdict(self)

    def matches(self, other: FileStat) -> bool:
        """比较两次 stat 采样是否指向"未变化"的同一份内容。

        inode 仅在双方都非 0（即在支持稳定 inode 语义的平台上）时才参与比较，
        避免 Windows 上 inode 恒为 0 导致误判。
        """
        if self.size != other.size or self.mtime_ns != other.mtime_ns:
            return False
        if self.inode != 0 and other.inode != 0 and self.inode != other.inode:
            return False
        return True


@dataclass(frozen=True)
class FileFingerprint:
    """一次完整的文件指纹：stat 元信息 + 内容哈希。"""

    path: str
    size: int
    mtime_ns: int
    inode: int
    sha256: str
    taken_at: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StableFingerprintResult:
    """稳定指纹采集结果：可能因反复不稳定而采集失败，需要调用方显式处理。"""

    stable: bool
    fingerprint: FileFingerprint | None
    attempts: int
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "stable": self.stable,
            "attempts": self.attempts,
            "reason": self.reason,
            **(self.fingerprint.to_dict() if self.fingerprint else {}),
        }


def stat_file(path: Path) -> FileStat:
    """采集一次文件的 size/mtime/inode，不读取内容。"""
    st = path.stat()
    return FileStat(size=st.st_size, mtime_ns=st.st_mtime_ns, inode=getattr(st, "st_ino", 0) or 0)


def hash_file(path: Path) -> str:
    """计算文件内容的 SHA-256（分块读取，适配大文件）。"""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def hash_bytes(data: bytes) -> str:
    """计算给定字节内容的 SHA-256，用于 read-fresh 场景下哈希与实际读取内容严格对齐。"""
    return hashlib.sha256(data).hexdigest()


def compute_fingerprint(path: Path) -> FileFingerprint:
    """一次性采集文件的完整指纹（stat + 内容哈希），弱保证，见模块文档。

    仅供内部复用或明确接受"两步之间可能被修改"这一弱保证的调用方使用。
    对外命令一律使用 compute_stable_fingerprint()。
    """
    if not path.is_file():
        raise FileNotFoundForFingerprint(f"文件不存在或不是普通文件: {path}")
    st = stat_file(path)
    digest = hash_file(path)
    # 规范化为绝对路径后再保存，避免同一文件的不同相对/含 .. 路径写法
    # 导致 FileFingerprint.path 字段不一致，进而影响调用方基于 path 字段做比较/去重。
    normalized_path = str(path.resolve())
    return FileFingerprint(
        path=normalized_path,
        size=st.size,
        mtime_ns=st.mtime_ns,
        inode=st.inode,
        sha256=digest,
        taken_at=time.time(),
    )


def compute_stable_fingerprint(
    path: Path,
    max_retries: int = DEFAULT_STABLE_MAX_RETRIES,
    backoff_seconds: float = DEFAULT_STABLE_BACKOFF_SECONDS,
) -> StableFingerprintResult:
    """stat → hash → stat 三明治校验，采集一份"确定对应磁盘上某个真实版本"的指纹。

    前后两次 stat 不一致（读取期间被并发修改）则判定本轮不稳定，退避后重试；
    达到重试上限仍不稳定，返回 stable=False，调用方不得使用其中的指纹字段作为结论依据。
    """
    if not path.is_file():
        return StableFingerprintResult(
            stable=False, fingerprint=None, attempts=0, reason="file_not_found"
        )

    attempts = 0
    last_reason = "unknown"
    while attempts < max_retries:
        attempts += 1
        try:
            before = stat_file(path)
            digest = hash_file(path)
            after = stat_file(path)
        except FileNotFoundError:
            last_reason = "file_not_found"
            time.sleep(backoff_seconds)
            continue

        if not before.matches(after):
            last_reason = "changed_during_read"
            time.sleep(backoff_seconds)
            continue

        fp = FileFingerprint(
            path=str(path.resolve()),
            size=after.size,
            mtime_ns=after.mtime_ns,
            inode=after.inode,
            sha256=digest,
            taken_at=time.time(),
        )
        return StableFingerprintResult(stable=True, fingerprint=fp, attempts=attempts)

    return StableFingerprintResult(
        stable=False, fingerprint=None, attempts=attempts, reason=last_reason
    )
