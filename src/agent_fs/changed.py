"""变更检测：对比某个历史快照与当前文件状态。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_fs.fingerprint import DEFAULT_STABLE_MAX_RETRIES, compute_stable_fingerprint


@dataclass(frozen=True)
class ChangedResult:
    changed: bool
    stable: bool
    path: str
    snapshot_id: str
    previous_sha256: str | None
    current_sha256: str | None
    previous_size: int | None
    current_size: int | None
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def compute_changed(
    path: Path,
    snapshot: dict[str, Any],
    snapshot_id: str,
    max_retries: int = DEFAULT_STABLE_MAX_RETRIES,
) -> ChangedResult:
    """比较一份历史快照记录与当前文件的最新（稳定）指纹。

    使用 compute_stable_fingerprint 而非一次性 stat+hash，避免在文件被并发写入的
    瞬间采样到中间态，从而给出"看似确定实则不可靠"的 changed 结论。
    """
    previous_sha256 = snapshot.get("sha256")
    previous_size = snapshot.get("size")

    result = compute_stable_fingerprint(path, max_retries=max_retries)

    if not result.stable:
        if result.reason == "file_not_found":
            return ChangedResult(
                changed=True,
                stable=True,  # "文件已消失"本身是可以确定的结论，不属于不稳定态
                path=str(path),
                snapshot_id=snapshot_id,
                previous_sha256=previous_sha256,
                current_sha256=None,
                previous_size=previous_size,
                current_size=None,
                reason="file_missing_now",
            )
        return ChangedResult(
            changed=False,
            stable=False,
            path=str(path),
            snapshot_id=snapshot_id,
            previous_sha256=previous_sha256,
            current_sha256=None,
            previous_size=previous_size,
            current_size=None,
            reason=result.reason or "unstable_read",
        )

    current = result.fingerprint
    assert current is not None  # noqa: S101 - stable=True 时 fingerprint 必然存在

    if current.sha256 == previous_sha256:
        return ChangedResult(
            changed=False,
            stable=True,
            path=str(path),
            snapshot_id=snapshot_id,
            previous_sha256=previous_sha256,
            current_sha256=current.sha256,
            previous_size=previous_size,
            current_size=current.size,
            reason="content_unchanged",
        )

    return ChangedResult(
        changed=True,
        stable=True,
        path=str(path),
        snapshot_id=snapshot_id,
        previous_sha256=previous_sha256,
        current_sha256=current.sha256,
        previous_size=previous_size,
        current_size=current.size,
        reason="content_hash_mismatch",
    )
