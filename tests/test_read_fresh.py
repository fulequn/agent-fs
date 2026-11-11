"""read_fresh.py 单元测试。"""

from __future__ import annotations

from unittest.mock import patch

from agent_fs.fingerprint import FileStat, hash_bytes
from agent_fs.read_fresh import read_fresh


def test_read_fresh_stable_returns_content_and_hash(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = read_fresh(p)

    assert result.stable is True
    assert result.content == "line1\nline2\nline3\n"
    assert result.sha256 == hash_bytes(b"line1\nline2\nline3\n")
    assert result.attempts == 1
    assert result.truncated is False


def test_read_fresh_with_offset_and_limit(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")

    result = read_fresh(p, offset=1, limit=2)

    assert result.stable is True
    assert result.content == "l2\nl3\n"
    assert result.truncated is True


def test_read_fresh_missing_file(tmp_path):
    missing = tmp_path / "nope.txt"
    result = read_fresh(missing)
    assert result.stable is False
    assert result.reason == "file_not_found"


def test_read_fresh_detects_change_during_read_and_retries(tmp_path):
    """模拟读取期间文件发生变化：前后两次 stat 不一致，应重试直到超限并返回不稳定。"""
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")

    stable_stat = FileStat(size=2, mtime_ns=100, inode=1)
    changed_stat = FileStat(size=99, mtime_ns=999, inode=1)

    # 每次调用 read_fresh 内部都会 stat 两次（before/after），
    # 让每一轮的 before/after 都不一致，模拟"读取期间被修改"。
    with patch(
        "agent_fs.read_fresh.stat_file",
        side_effect=[
            stable_stat,
            changed_stat,  # 第 1 次尝试：before != after
            stable_stat,
            changed_stat,  # 第 2 次尝试
            stable_stat,
            changed_stat,  # 第 3 次尝试
        ],
    ):
        result = read_fresh(p, max_retries=3, backoff_seconds=0)

    assert result.stable is False
    assert result.reason == "changed_during_read"
    assert result.attempts == 3


def test_read_fresh_recovers_after_transient_instability(tmp_path):
    """前两次不稳定，第三次稳定，应最终返回成功结果。"""
    p = tmp_path / "a.txt"
    p.write_text("stable-content", encoding="utf-8")

    stable_stat = FileStat(size=len(b"stable-content"), mtime_ns=100, inode=1)
    changed_stat = FileStat(size=999, mtime_ns=999, inode=1)

    with patch(
        "agent_fs.read_fresh.stat_file",
        side_effect=[
            stable_stat,
            changed_stat,  # 第 1 次：不稳定
            stable_stat,
            stable_stat,  # 第 2 次：稳定
        ],
    ):
        result = read_fresh(p, max_retries=3, backoff_seconds=0)

    assert result.stable is True
    assert result.attempts == 2
    assert result.content == "stable-content"


def test_read_fresh_binary_content_returns_hash_without_text(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"\xff\xfe\x00\x01binary")

    result = read_fresh(p)

    assert result.stable is True
    assert result.content is None
    assert result.reason == "binary_or_undecodable"
    assert result.sha256 == hash_bytes(b"\xff\xfe\x00\x01binary")
