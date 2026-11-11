"""store.py 单元测试。"""

from __future__ import annotations

import stat
import sys

import pytest

from agent_fs import store

# AGENT_FS_HOME 隔离由 tests/conftest.py 的 autouse fixture 统一提供


def test_make_snapshot_id_is_deterministic(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    sid1 = store.make_snapshot_id(p, taken_at=123.456789)
    sid2 = store.make_snapshot_id(p, taken_at=123.456789)
    assert sid1 == sid2


def test_make_snapshot_id_differs_by_time(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    sid1 = store.make_snapshot_id(p, taken_at=1.0)
    sid2 = store.make_snapshot_id(p, taken_at=2.0)
    assert sid1 != sid2


def test_save_and_load_snapshot_roundtrip(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    sid = store.make_snapshot_id(p, taken_at=1.0)

    store.save_snapshot(sid, {"path": str(p), "sha256": "deadbeef", "size": 1})
    loaded = store.load_snapshot(sid)

    assert loaded["path"] == str(p)
    assert loaded["sha256"] == "deadbeef"
    assert loaded["snapshot_id"] == sid


def test_load_snapshot_missing_raises():
    with pytest.raises(FileNotFoundError):
        store.load_snapshot("does-not-exist-snapshot-id")


def test_snapshots_dir_uses_env_override(tmp_path):
    d = store.snapshots_dir()
    assert str(d).startswith(str(tmp_path))
    assert d.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 权限语义在 Windows 上不适用")
def test_save_snapshot_restricts_file_permission(tmp_path):
    """黑盒评审发现的 Medium 问题：快照文件之前以默认权限（0o644）写入，
    包含文件绝对路径与内容哈希，在多用户系统下存在信息泄露风险。
    修复后应收紧为仅所有者可读写（0o600）。
    """
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    sid = store.make_snapshot_id(p, taken_at=1.0)

    out_path = store.save_snapshot(sid, {"path": str(p), "sha256": "deadbeef", "size": 1})

    mode = stat.S_IMODE(out_path.stat().st_mode)
    assert mode == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 权限语义在 Windows 上不适用")
def test_snapshots_dir_restricted_to_owner(tmp_path):
    """快照目录同样只允许所有者进入（目录可列举即泄露路径与哈希）。"""
    d = store.snapshots_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


class TestSnapshotIdValidation:
    @pytest.mark.parametrize(
        "sid",
        [
            "",
            ".",
            "..",
            ".hidden",
            "../escape",
            "a/b",
            "a\\b",
            "a\x00b",
            "x" * 201,
        ],
    )
    def test_rejects_unsafe_ids(self, sid):
        with pytest.raises(FileNotFoundError):
            store.load_snapshot(sid)

    def test_rejects_ascii_non_whitelist(self):
        with pytest.raises(FileNotFoundError):
            store.load_snapshot("a b")

    def test_make_snapshot_id_sanitizes_basename(self, tmp_path):
        p = tmp_path / "we ird:name?.txt"
        p.write_text("x", encoding="utf-8")
        sid = store.make_snapshot_id(p, taken_at=1.0)
        assert "/" not in sid and " " not in sid and ":" not in sid
        store.save_snapshot(sid, {"path": str(p), "sha256": "deadbeef", "size": 1})
        assert store.load_snapshot(sid)["snapshot_id"] == sid


class TestSnapshotLoadValidation:
    def test_corrupted_json_raises_structured_error(self, tmp_path):
        sid = "corrupt-snap"
        path = store.snapshots_dir() / f"{sid}.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(store.SnapshotCorruptedError):
            store.load_snapshot(sid)

    def test_undecodable_bytes_raise_structured_error(self, tmp_path):
        sid = "binary-snap"
        path = store.snapshots_dir() / f"{sid}.json"
        path.write_bytes(b"\xff\xfe\x00")
        with pytest.raises(store.SnapshotCorruptedError):
            store.load_snapshot(sid)

    @pytest.mark.parametrize(
        "payload",
        [
            {"sha256": "deadbeef", "size": 1},
            {"path": "", "sha256": "deadbeef", "size": 1},
            {"path": "/tmp/x", "size": 1},
            {"path": "/tmp/x", "sha256": "deadbeef"},
            {"path": "/tmp/x", "sha256": "deadbeef", "size": "1"},
            [],
        ],
    )
    def test_missing_required_fields_raise(self, payload):
        sid = "bad-fields"
        path = store.snapshots_dir() / f"{sid}.json"
        import json

        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(store.SnapshotCorruptedError):
            store.load_snapshot(sid)

    def test_save_is_atomic_and_leaves_no_temp_files(self, tmp_path):
        p = tmp_path / "a.txt"
        p.write_text("x", encoding="utf-8")
        sid = store.make_snapshot_id(p, taken_at=1.0)
        store.save_snapshot(sid, {"path": str(p), "sha256": "deadbeef", "size": 1})
        leftovers = [n for n in store.snapshots_dir().iterdir() if ".tmp" in n.name]
        assert leftovers == []
