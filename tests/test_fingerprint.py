"""fingerprint.py 单元测试。"""

from __future__ import annotations

import time

import pytest

from agent_fs.fingerprint import (
    FileNotFoundForFingerprint,
    FileStat,
    compute_fingerprint,
    hash_bytes,
    hash_file,
    stat_file,
)


def test_hash_file_matches_hash_bytes(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello world", encoding="utf-8")
    assert hash_file(p) == hash_bytes(b"hello world")


def test_hash_is_deterministic_across_calls(tmp_path):
    p = tmp_path / "a.txt"
    content = "跨平台一致性测试 across mac/windows/linux\n" * 100
    p.write_text(content, encoding="utf-8")
    h1 = hash_file(p)
    h2 = hash_file(p)
    assert h1 == h2
    # 与直接对 bytes 求哈希也应一致，验证同一字节内容在不同代码路径下产生同一哈希
    assert h1 == hash_bytes(content.encode("utf-8"))


def test_compute_fingerprint_basic(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")
    fp = compute_fingerprint(p)
    # path 字段会被规范化为 resolve() 后的绝对路径（可能与原始写法不完全相同，
    # 例如 macOS 上 /tmp -> /private/tmp 的符号链接）。
    assert fp.path == str(p.resolve())
    assert fp.size == len(b"v1")
    assert fp.sha256 == hash_bytes(b"v1")
    assert fp.mtime_ns > 0
    assert fp.taken_at > 0


def test_compute_fingerprint_changes_after_write(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")
    fp1 = compute_fingerprint(p)

    time.sleep(0.01)
    p.write_text("v2-longer-content", encoding="utf-8")
    fp2 = compute_fingerprint(p)

    assert fp1.sha256 != fp2.sha256
    assert fp1.size != fp2.size


def test_compute_fingerprint_missing_file_raises(tmp_path):
    missing = tmp_path / "does-not-exist.txt"
    with pytest.raises(FileNotFoundForFingerprint):
        compute_fingerprint(missing)


def test_compute_fingerprint_directory_raises(tmp_path):
    with pytest.raises(FileNotFoundForFingerprint):
        compute_fingerprint(tmp_path)


def test_filestat_matches_true_for_identical(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("same", encoding="utf-8")
    s1 = stat_file(p)
    s2 = stat_file(p)
    assert s1.matches(s2)


def test_filestat_matches_false_for_changed_size():
    a = FileStat(size=10, mtime_ns=100, inode=5)
    b = FileStat(size=20, mtime_ns=100, inode=5)
    assert not a.matches(b)


def test_filestat_matches_false_for_changed_mtime():
    a = FileStat(size=10, mtime_ns=100, inode=5)
    b = FileStat(size=10, mtime_ns=200, inode=5)
    assert not a.matches(b)


def test_filestat_inode_zero_is_ignored_for_windows_compat():
    """Windows 上 inode 可能恒为 0，两侧都为 0 时不应因 inode 判定不匹配。"""
    a = FileStat(size=10, mtime_ns=100, inode=0)
    b = FileStat(size=10, mtime_ns=100, inode=0)
    assert a.matches(b)


def test_filestat_matches_false_for_different_nonzero_inode():
    a = FileStat(size=10, mtime_ns=100, inode=5)
    b = FileStat(size=10, mtime_ns=100, inode=6)
    assert not a.matches(b)


def test_compute_fingerprint_path_normalized_for_different_notations(tmp_path):
    """回归测试：同一文件的不同路径写法（相对/含 .. ）应该得到一致的
    规范化 path 字段，便于调用方基于 path 字段做比较/去重。
    对应黑盒评审发现的 Critical 问题：同一文件不同路径表示得到不一致的 path 字段。
    """
    sub = tmp_path / "subdir"
    sub.mkdir()
    p = tmp_path / "a.txt"
    p.write_text("content", encoding="utf-8")

    direct_path = p
    indirect_path = sub / ".." / "a.txt"

    fp1 = compute_fingerprint(direct_path)
    fp2 = compute_fingerprint(indirect_path)

    assert fp1.path == fp2.path == str(p.resolve())
