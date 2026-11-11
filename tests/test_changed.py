"""changed.py 单元测试。"""

from __future__ import annotations

from unittest.mock import patch

from agent_fs.changed import compute_changed
from agent_fs.fingerprint import FileStat, compute_fingerprint


def test_changed_false_when_content_identical(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("same content", encoding="utf-8")
    fp = compute_fingerprint(p)
    snapshot = fp.to_dict()

    result = compute_changed(p, snapshot, "snap-1")

    assert result.changed is False
    assert result.reason == "content_unchanged"
    assert result.current_sha256 == result.previous_sha256


def test_changed_true_when_content_modified(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")
    fp = compute_fingerprint(p)
    snapshot = fp.to_dict()

    p.write_text("v2-different", encoding="utf-8")

    result = compute_changed(p, snapshot, "snap-1")

    assert result.changed is True
    assert result.reason == "content_hash_mismatch"
    assert result.current_sha256 != result.previous_sha256


def test_changed_true_when_file_missing_now(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")
    fp = compute_fingerprint(p)
    snapshot = fp.to_dict()

    p.unlink()

    result = compute_changed(p, snapshot, "snap-1")

    assert result.changed is True
    assert result.reason == "file_missing_now"
    assert result.current_sha256 is None


def test_changed_unstable_when_read_conflicts_during_check(tmp_path):
    """读取期间文件持续变化导致 compute_stable_fingerprint 重试耗尽时，
    compute_changed 应返回 stable=False，且 changed 不应被误判为 False（防止调用方
    误以为“确认未变化”而跳过必要的重试）。"""
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")
    fp = compute_fingerprint(p)
    snapshot = fp.to_dict()

    stable_stat = FileStat(size=2, mtime_ns=100, inode=1)
    changed_stat = FileStat(size=99, mtime_ns=999, inode=1)

    with patch(
        "agent_fs.fingerprint.stat_file",
        side_effect=[stable_stat, changed_stat] * 3,
    ):
        result = compute_changed(p, snapshot, "snap-1", max_retries=3)

    assert result.stable is False
    assert result.changed is False
    assert result.reason == "changed_during_read"
