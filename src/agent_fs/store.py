"""快照本地存储。

快照落盘在用户主目录下的 `~/.agent-fs/snapshots/`，避免硬编码 `/tmp`（Windows 无此路径），
用 `pathlib.Path.home()` 保证跨平台一致。也支持通过环境变量 `AGENT_FS_HOME` 覆盖，
便于测试和多工作区隔离。

快照文件命名：`<snapshot_id>.json`，`snapshot_id` 由「路径的规范化绝对路径」与
「采集时间」共同派生的短哈希构成，避免同一文件多次快照互相覆盖，同时保持人类可读。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

# 快照 ID 会直接拼进文件名：拒绝路径分隔符、父目录引用与控制字符。
_SNAPSHOT_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_SNAPSHOT_ID_LENGTH = 200


class SnapshotCorruptedError(Exception):
    """快照文件存在但内容损坏 / 缺少必需字段。"""


def _agent_fs_home() -> Path:
    override = os.environ.get("AGENT_FS_HOME")
    if override:
        return Path(override)
    return Path.home() / ".agent-fs"


def _private_dir(path: Path) -> Path:
    """创建仅所有者可访问的目录（已存在时也收紧权限）。"""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def snapshots_dir() -> Path:
    return _private_dir(_agent_fs_home() / "snapshots")


def make_snapshot_id(path: Path, taken_at: float) -> str:
    """基于规范化绝对路径 + 采集时间生成稳定、可复现的快照 ID。

    basename 中的路径不安全字符替换为 `_`，保证 ID 可直接作为文件名；
    旧版本已生成的 ID 仍可读取（load_snapshot 只拒绝路径穿越形态）。
    """
    raw = f"{path.resolve()}::{taken_at:.6f}"
    short = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[:80] or "snapshot"
    return f"{safe_name}-{short}"


def _validate_snapshot_id(snapshot_id: str) -> None:
    if (
        not snapshot_id
        or len(snapshot_id) > _MAX_SNAPSHOT_ID_LENGTH
        or snapshot_id in {".", ".."}
        or snapshot_id.startswith(".")
        or "/" in snapshot_id
        or "\\" in snapshot_id
        or "\x00" in snapshot_id
    ):
        raise FileNotFoundError(f"非法快照 ID: {snapshot_id!r}")
    # ASCII 范围内的异常字符一律拒绝；非 ASCII 仅用于兼容旧版按原名生成的 ID。
    if not _SNAPSHOT_ID_SAFE_RE.match(snapshot_id) and snapshot_id.isascii():
        raise FileNotFoundError(f"非法快照 ID: {snapshot_id!r}")


def save_snapshot(snapshot_id: str, payload: dict[str, Any]) -> Path:
    """将快照记录原子写入本地存储（创建即 0600，返回落盘路径）。"""
    _validate_snapshot_id(snapshot_id)
    out_path = snapshots_dir() / f"{snapshot_id}.json"
    data = dict(payload)
    data["snapshot_id"] = snapshot_id
    data.setdefault("saved_at", time.time())
    encoded = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    tmp_path = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, out_path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return out_path


def prune_snapshots(
    *,
    older_than_days: float | None = None,
    keep_last: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """按条件清理本地快照；两个条件同时给出时必须都满足才删。

    保守语义：
    - `older_than_days` 按快照文件的 mtime 判断；
    - `keep_last` 是**按目标文件**保留最新 N 份（不是全局 N 份），避免清理
      一个文件时把其他协作者正在用的快照删掉；
    - 条件同时给出取交集（更老且不在保留窗口内），不会因为多给一个条件
      就删得更多；
    - 损坏/无法解析的快照不参与 keep_last 分组，也不会被删除。

    默认 `dry_run=False` 只是函数层默认值；CLI 在未显式给条件时直接拒绝，
    并要求 `--dry-run` 才能预览。真正删除时逐条 unlink，单条失败不影响其余。
    """
    directory = snapshots_dir()
    if older_than_days is not None and older_than_days < 0:
        raise ValueError("older_than_days 不能为负")
    if keep_last is not None and keep_last < 0:
        raise ValueError("keep_last 不能为负")

    cutoff = None
    if older_than_days is not None:
        cutoff = time.time() - older_than_days * 86400

    entries: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        try:
            stat = path.stat()
        except OSError:
            continue
        target: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("path"), str):
                target = data["path"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        entries.append(
            {
                "snapshot_id": path.stem,
                "file": path,
                "target": target,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )

    # 每个目标各自保留最新 keep_last 份；无法识别目标的条目不进入保留集合
    keep_ids: set[str] = set()
    if keep_last is not None:
        by_target: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            if entry["target"] is not None:
                by_target.setdefault(entry["target"], []).append(entry)
        for group in by_target.values():
            group.sort(key=lambda item: item["mtime"], reverse=True)
            keep_ids.update(item["snapshot_id"] for item in group[:keep_last])

    candidates = []
    for entry in entries:
        if cutoff is not None and entry["mtime"] >= cutoff:
            continue
        if keep_last is not None and entry["snapshot_id"] in keep_ids:
            continue
        candidates.append(entry)

    removed: list[str] = []
    failed: list[dict[str, str]] = []
    freed = 0
    if not dry_run:
        for entry in candidates:
            try:
                entry["file"].unlink()
            except OSError as error:
                failed.append(
                    {"snapshot_id": entry["snapshot_id"], "error": str(error)}
                )
                continue
            removed.append(entry["snapshot_id"])
            freed += entry["size"]
    else:
        removed = [entry["snapshot_id"] for entry in candidates]
        freed = sum(entry["size"] for entry in candidates)

    return {
        "dry_run": dry_run,
        "scanned": len(entries),
        "matched": len(candidates),
        "removed": removed,
        "failed": failed,
        "freed_bytes": freed,
        "next_action": (
            "预览未删除任何文件；确认后去掉 --dry-run 再执行"
            if dry_run
            else (
                "清理完成"
                if not failed
                else "部分快照删除失败：检查权限后重试，或手工处理 failed 列表"
            )
        ),
    }


def load_snapshot(snapshot_id: str) -> dict[str, Any]:
    """按 snapshot_id 读取一份已保存的快照记录（含格式与字段校验）。"""
    _validate_snapshot_id(snapshot_id)
    path = snapshots_dir() / f"{snapshot_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"快照不存在: {snapshot_id}（查找路径: {path}）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotCorruptedError(f"快照记录损坏: {snapshot_id}") from error
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("path"), str)
        or not data["path"]
        or not isinstance(data.get("sha256"), str)
        or not data["sha256"]
        or not isinstance(data.get("size"), int)
    ):
        raise SnapshotCorruptedError(f"快照缺少必需字段: {snapshot_id}")
    return data
