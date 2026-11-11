"""write_atomic.py 单元测试：write-if-match 的受控原子写入（schema-2 结果契约）。

故障注入沿用 unittest.mock.patch 模式；回读故障统一注入
agent_fs.write_atomic._stable_readback（替换后稳定回读的唯一入口）。
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from agent_fs.fingerprint import (
    StableFingerprintResult,
    compute_fingerprint,
    compute_stable_fingerprint,
    hash_bytes,
)
from agent_fs.result import (
    COMMIT_COMMITTED,
    COMMIT_NOT_COMMITTED,
    STATUS_ERROR,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
)
from agent_fs.write_atomic import (
    _acquire_target_lock,
    _lock_file_path,
    _open_lock_file,
    _release_target_lock,
    write_if_match,
)


def _visible_files(tmp_path):
    """目录内容剔除 AGENT_FS_HOME（conftest 隔离目录，锁文件在其中，不在目标目录）。"""
    return [x for x in tmp_path.iterdir() if x.name != ".agent-fs"]


def test_write_if_match_success(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_VERIFIED
    assert result.commit_state == COMMIT_COMMITTED
    assert result.reason_code == "replaced"
    assert p.read_text(encoding="utf-8") == "new content"
    # 稳定回读哈希等于预期结果（Htarget 由命令自行计算）
    assert result.observed_sha256 == result.desired_sha256
    assert result.desired_sha256 == hash_bytes(b"new content")
    assert result.durability == "synced"


def test_write_if_match_declared_target_mismatch_rejects(tmp_path):
    """调用方声明的 Htarget 与实际准备字节不一致 → prepared_content_changed，目标不动。"""
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    result = write_if_match(p, b"new content", base_hash, desired_sha256="e" * 64)

    assert result.status == STATUS_REJECTED
    assert result.commit_state == COMMIT_NOT_COMMITTED
    assert result.reason_code == "prepared_content_changed"
    assert p.read_text(encoding="utf-8") == "original"


def test_write_if_match_declared_target_verified(tmp_path):
    """声明的 Htarget 与实际字节一致 → 正常写入并 verified。"""
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256
    target = hash_bytes(b"new content")

    result = write_if_match(p, b"new content", base_hash, desired_sha256=target)

    assert result.status == STATUS_VERIFIED
    assert result.desired_sha256 == target
    assert result.observed_sha256 == target


def test_write_if_match_rejects_when_cooperating_writer_holds_lock(tmp_path):
    p = tmp_path / "shared.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256
    lock_file = _acquire_target_lock(p)
    assert lock_file is not None
    try:
        result = write_if_match(p, b"attempted", base_hash)
    finally:
        _release_target_lock(lock_file)

    assert result.status == STATUS_REJECTED
    assert result.reason_code == "lock_busy"
    assert result.commit_state == COMMIT_NOT_COMMITTED
    assert p.read_text(encoding="utf-8") == "original"


def test_write_if_match_rejects_on_hash_mismatch(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")

    result = write_if_match(p, b"attempted overwrite", "0" * 64)

    assert result.status == STATUS_REJECTED
    assert result.reason_code == "hash_mismatch"
    # previous_sha256 是本次观察到的写前值，不冒充输入基线
    assert result.previous_sha256 == compute_fingerprint(p).sha256
    # 内容不应被修改
    assert p.read_text(encoding="utf-8") == "original"


def test_write_if_match_detects_concurrent_modification(tmp_path):
    """模拟典型的 TOCTOU 场景：调用方读取哈希后，文件被另一个进程修改，
    write_if_match 必须拒绝写入，而不是用过期的哈希覆盖新内容。"""
    p = tmp_path / "shared.txt"
    p.write_text("v1", encoding="utf-8")
    stale_hash = compute_fingerprint(p).sha256

    # 模拟另一个 Agent 抢先修改了文件
    p.write_text("v2-changed-by-someone-else", encoding="utf-8")

    result = write_if_match(p, b"my-overwrite-attempt", stale_hash)

    assert result.status == STATUS_REJECTED
    assert result.reason_code == "hash_mismatch"
    assert p.read_text(encoding="utf-8") == "v2-changed-by-someone-else"


def test_write_if_match_missing_file(tmp_path):
    missing = tmp_path / "nope.txt"
    result = write_if_match(missing, b"content", "0" * 64)
    assert result.status == STATUS_ERROR
    assert result.reason_code == "file_not_found"


def test_write_if_match_directory_path_rejected(tmp_path):
    result = write_if_match(tmp_path, b"content", "0" * 64)
    assert result.status == STATUS_ERROR
    assert result.reason_code == "not_a_regular_file"


def test_write_if_match_no_leftover_temp_file_on_success(tmp_path):
    """成功写入后，目录中不应残留临时文件。"""
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    write_if_match(p, b"v2", base_hash)

    assert _visible_files(tmp_path) == [p]


def test_write_if_match_no_leftover_temp_file_on_rejection(tmp_path):
    """校验失败拒绝写入后，目录中也不应残留临时文件。"""
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")

    write_if_match(p, b"v2", "0" * 64)

    assert _visible_files(tmp_path) == [p]


def test_write_if_match_preserves_content_on_binary_data(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"\x00\x01\x02original")
    base_hash = compute_fingerprint(p).sha256

    new_content = b"\xff\xfe\x00new-binary-content"
    result = write_if_match(p, new_content, base_hash)

    assert result.status == STATUS_VERIFIED
    assert p.read_bytes() == new_content


# ── 系统级 I/O 异常的兼容性测试：验证不会因磁盘满/权限不足等错误而抛出未捕获异常 ──


def test_write_if_match_write_temp_file_oserror_returns_structured_result(tmp_path):
    """写临时文件时若抛出 OSError（模拟磁盘满/权限不足，A06），
    返回 error/not_committed 且原文件保留，不得向上传播未捕获异常。"""
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    real_open = os.open

    def open_fails_on_temp(path, flags, mode=0o777, *args, **kwargs):
        if "agentfs-tmp" in str(path):
            raise OSError("disk full (simulated)")
        return real_open(path, flags, mode, *args, **kwargs)

    with patch("os.open", side_effect=open_fails_on_temp):
        result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_ERROR
    assert result.commit_state == COMMIT_NOT_COMMITTED
    assert result.reason_code == "write_temp_file_failed"
    # 原文件内容不应受影响
    assert p.read_text(encoding="utf-8") == "original"


def test_write_if_match_replace_oserror_returns_structured_result(tmp_path):
    """os.replace 本身失败（模拟权限/跨文件系统等问题）时，
    应返回结构化结果，且临时文件仍应被清理。"""
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    with patch("os.replace", side_effect=OSError("permission denied (simulated)")):
        result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_ERROR
    assert result.commit_state == COMMIT_NOT_COMMITTED
    assert result.reason_code == "replace_failed"
    # 替换失败，原文件应保持不变
    assert p.read_text(encoding="utf-8") == "original"
    # 临时文件不应残留
    assert _visible_files(tmp_path) == [p]


# ── 提交事实与结果确认分离（SPEC A07/A08/A16/A17）──


def test_write_if_match_readback_failure_is_unknown_committed(tmp_path):
    """A07：替换已执行但回读抛 PermissionError → unknown/committed，
    绝不回退为 not_committed，也不提示直接重试。"""
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256
    target = hash_bytes(b"new content")

    # 调用序：基线(1) → 替换前复核(2) → 回读(3)；只让回读抛 PermissionError
    real_stable = compute_stable_fingerprint
    calls = {"n": 0}

    def flaky_stable(path, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            return real_stable(path, **kwargs)
        raise PermissionError(13, "Permission denied")

    with patch(
        "agent_fs.write_atomic.compute_stable_fingerprint", side_effect=flaky_stable
    ):
        result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_UNKNOWN
    assert result.commit_state == COMMIT_COMMITTED
    assert result.reason_code == "replaced_but_post_check_failed"
    assert p.read_text(encoding="utf-8") == "new content"  # 替换确实发生了
    assert result.observed_sha256 is None
    assert result.desired_sha256 == target
    compat = result.to_dict()
    assert compat["written"] is True  # 替换事实成立
    assert compat["new_sha256"] is None  # 但无法核实结果
    # 只允许核实流程，明确不自动重试（SPEC A07：不得提示直接重试）
    assert "不自动重试" in compat["next_action"]
    assert "reconcile" in compat["next_action"]


def test_write_if_match_external_overwrite_before_readback_is_unknown(tmp_path):
    """A08：替换后、回读前被外部写入者改写 → unknown + observed 与 desired 分别提供。"""
    from agent_fs.fingerprint import compute_stable_fingerprint

    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256
    target = hash_bytes(b"new content")
    external = b"external-content"
    external_sha = hash_bytes(external)

    def external_writer_readback(path):
        path.write_bytes(external)  # 外部写入者在回读前抢先改写
        return compute_stable_fingerprint(path)

    with patch(
        "agent_fs.write_atomic._stable_readback", side_effect=external_writer_readback
    ):
        result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_UNKNOWN
    assert result.commit_state == COMMIT_COMMITTED
    assert result.reason_code == "observed_mismatch"
    assert result.observed_sha256 == external_sha
    assert result.desired_sha256 == target
    assert result.observed_sha256 != result.desired_sha256
    compat = result.to_dict()
    assert compat["written"] is True
    assert compat["new_sha256"] is None  # 外部内容不得冒充本次预期成果
    assert compat["observed_sha256"] == external_sha
    assert p.read_bytes() == external  # 外部内容保留，不被自动重写


def test_write_if_match_readback_unstable_is_unknown_committed(tmp_path):
    """回读反复不稳定 → unknown/committed（observed_unstable_after_replace）。"""
    from agent_fs.fingerprint import StableFingerprintResult

    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    with patch(
        "agent_fs.write_atomic._stable_readback",
        return_value=StableFingerprintResult(
            stable=False, fingerprint=None, attempts=3, reason="changed_during_read"
        ),
    ):
        result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_UNKNOWN
    assert result.commit_state == COMMIT_COMMITTED
    assert result.reason_code == "observed_unstable_after_replace"


def test_write_if_match_post_commit_fsync_io_error_degrades_durability(tmp_path):
    """A17（提交后）：目录 fsync 真实 I/O 错误不回滚——内容已验证但持久化未确认。"""
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    real_fsync_path = "agent_fs.write_atomic._fsync_path"

    def selective_fsync(target):
        import os

        if target == p.parent:
            return ("io", "OSError: simulated dir fsync failure")
        with open(target, "rb") as fh:  # tmp 文件照常 fsync（O_RDONLY 语义由实现决定）
            os.fsync(fh.fileno())
        return None

    with patch(real_fsync_path, side_effect=selective_fsync):
        result = write_if_match(p, b"new content", base_hash)

    # 提交后目录 fsync 失败：不回滚、不重复写入；内容已验证 + 持久化未确认
    assert result.status == STATUS_VERIFIED
    assert result.commit_state == COMMIT_COMMITTED
    assert result.durability == "unconfirmed"
    assert "dir_fsync_io" in (result.detail or "")
    assert p.read_text(encoding="utf-8") == "new content"


def test_write_if_match_tmp_fsync_io_error_blocks_replace(tmp_path):
    """A17（提交前）：临时文件 fsync 真实 I/O 错误必须阻止替换。"""
    import errno as errno_mod

    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    with patch(
        "os.fsync", side_effect=OSError(errno_mod.EIO, "Input/output error")
    ):
        result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_ERROR
    assert result.commit_state == COMMIT_NOT_COMMITTED
    assert result.reason_code == "sync_io_error_before_commit"
    assert p.read_text(encoding="utf-8") == "original"  # 目标原文保留


def test_write_if_match_fsync_unsupported_degrades_not_fails(tmp_path):
    """平台不支持 fsync（ENOTSUP/EINVAL 等）→ 降级 unconfirmed，不虚构掉电保证。"""
    import errno as errno_mod

    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    with patch(
        "os.fsync", side_effect=OSError(errno_mod.ENOTSUP, "Operation not supported")
    ):
        result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_VERIFIED
    assert result.durability == "unconfirmed"
    assert "unsupported" in (result.detail or "")


def test_write_if_match_tmp_cleanup_failure_is_ancillary(tmp_path):
    """A16：提交后临时清理失败不改变 commit_state，附加 ancillary_failures。"""
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    # 注入点：写入核心已验证 tmp 被替换消费，仅当 finally 清理误判 tmp 仍存在
    # 时才会走到 unlink——这里让 unlink 抛 OSError 模拟清理失败路径。
    with patch("pathlib.Path.unlink", side_effect=OSError("cleanup failed (simulated)")):
        with patch("pathlib.Path.exists", side_effect=[True, True, True, True, True, True]):
            result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_VERIFIED
    assert result.commit_state == COMMIT_COMMITTED
    assert any("tmp_cleanup_failed" in f for f in result.ancillary_failures)
    assert p.read_text(encoding="utf-8") == "new content"


def test_write_if_match_lock_release_failure_is_ancillary(tmp_path):
    """A16：解锁失败不掩盖 verified 结论。"""
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    with patch(
        "agent_fs.write_atomic._release_target_lock",
        side_effect=OSError("unlock failed (simulated)"),
    ):
        result = write_if_match(p, b"new content", base_hash)

    assert result.status == STATUS_VERIFIED
    assert result.commit_state == COMMIT_COMMITTED
    assert any("lock_release_failed" in f for f in result.ancillary_failures)


def test_write_if_match_cli_returns_json_not_traceback_on_oserror(tmp_path, capsys):
    """端到端验证：通过 CLI 调用时，即使底层抛出 OSError，
    输出也应是结构化 JSON 而不是 Python 堆栈跟踪。"""
    import json

    from agent_fs.cli import main

    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")
    base_hash = compute_fingerprint(p).sha256

    real_open = os.open

    def open_fails_on_temp(path, flags, mode=0o777, *args, **kwargs):
        if "agentfs-tmp" in str(path):
            raise OSError("disk full (simulated)")
        return real_open(path, flags, mode, *args, **kwargs)

    with patch("os.open", side_effect=open_fails_on_temp):
        exit_code = main(
            ["write-if-match", str(p), "--expected-sha256", base_hash, "--content", "x"]
        )

    out = capsys.readouterr().out
    # 关键断言：输出必须是合法 JSON（若崩溃会抛出 Traceback 而非 JSON，
    # json.loads 会直接抛出异常使本测试失败，这正是我们要防御的回归。
    # write_if_match 内部已将 OSError 转化为 error/not_committed 的业务性结果，
    # 因此退出码为 EXIT_ERROR(2)，与"可确定发生在提交前的错误"语义一致。
    result = json.loads(out)
    assert exit_code == 2
    assert result["written"] is False
    assert result["reason_code"] == "write_temp_file_failed"
    assert result["status"] == "error"
    assert result["commit_state"] == "not_committed"


# ── 审查修复回归（2026-10-02 审查 P1-1/P1-2/P1-3）──


class TestFsyncCloseErrorContained:
    def test_fsync_path_close_error_is_classified_not_raised(self, tmp_path):
        """P1-1：_fsync_path 的 close 失败必须收敛为分类结果，不能异常逃逸——
        否则替换后的 close 错误会被 CLI 局部捕获误报为 not_committed。"""
        import os as _os

        f = tmp_path / "f.txt"
        f.write_bytes(b"x")
        real_close = _os.close
        calls = {"n": 0}

        def flaky_close(fd):
            calls["n"] += 1
            real_close(fd)
            if calls["n"] == 1:  # _fsync_path 每次调用只 close 一个 fd
                raise OSError("simulated close failure")

        from agent_fs.write_atomic import _fsync_path

        with patch("os.close", side_effect=flaky_close):
            outcome = _fsync_path(f)
        assert outcome is not None
        kind, message = outcome
        assert kind == "io"
        assert "close failure" in message

    def test_post_commit_dir_close_error_stays_verified_unconfirmed(self, tmp_path):
        """P1-1 端到端：目录同步 close 失败发生在替换之后 → verified/committed +
        durability=unconfirmed，绝不回退为 error/not_committed。"""
        p = tmp_path / "a.txt"
        p.write_text("original", encoding="utf-8")
        base = compute_fingerprint(p).sha256

        # 注入：替换后的目录 fsync 返回 io 类错误（close 引起的真实 I/O 失败）
        def dir_fsync_io(target):
            if target == p.parent:
                return ("io", "OSError: dir handle close failed")
            return None

        with patch("agent_fs.write_atomic._fsync_path", side_effect=dir_fsync_io):
            result = write_if_match(p, b"new content", base)

        assert result.status == "verified"
        assert result.commit_state == "committed"
        assert result.durability == "unconfirmed"
        assert "dir_fsync_io" in (result.detail or "")
        assert p.read_text(encoding="utf-8") == "new content"


class TestStableFingerprintBaseline:
    def test_unstable_baseline_rejected(self, tmp_path):
        """P1-2：基线读取期间内容持续被改 → target_unstable 拒绝，目标不动。"""
        p = tmp_path / "a.txt"
        p.write_text("v1", encoding="utf-8")

        with patch(
            "agent_fs.write_atomic.compute_stable_fingerprint",
            return_value=StableFingerprintResult(
                stable=False, fingerprint=None, attempts=3, reason="changed_during_read"
            ),
        ):
            result = write_if_match(p, b"new", "a" * 64)

        assert result.status == "rejected"
        assert result.reason_code == "target_unstable"
        assert "stage=baseline" in (result.detail or "")
        assert p.read_text(encoding="utf-8") == "v1"

    def test_unstable_recheck_rejected(self, tmp_path):
        """P1-2：替换前复核期间内容被并发修改 → target_unstable 拒绝。"""
        p = tmp_path / "a.txt"
        p.write_text("v1", encoding="utf-8")
        base = compute_fingerprint(p).sha256

        real_stable = compute_stable_fingerprint
        calls = {"n": 0}

        def flaky(path, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_stable(path, **kwargs)  # 基线正常
            return StableFingerprintResult(
                stable=False, fingerprint=None, attempts=3, reason="changed_during_read"
            )  # 复核不稳定

        with patch("agent_fs.write_atomic.compute_stable_fingerprint", side_effect=flaky):
            result = write_if_match(p, b"new content", base)

        assert result.status == "rejected"
        assert result.reason_code == "target_unstable"
        assert "stage=recheck" in (result.detail or "")
        assert p.read_text(encoding="utf-8") == "v1"


class TestRestrictedTempFilePerms:
    def test_restrictive_mode_preserved_after_replace(self, tmp_path):
        """P1-3：0600 原文件受控替换后权限保持 0600（临时文件以受限权限创建）。"""
        import os as _os
        import stat as _stat

        p = tmp_path / "secret.txt"
        p.write_text("v1", encoding="utf-8")
        _os.chmod(p, 0o600)
        base = compute_fingerprint(p).sha256

        result = write_if_match(p, b"v2", base)

        assert result.status == "verified"
        assert _stat.S_IMODE(_os.stat(p).st_mode) == 0o600

    def test_chmod_failure_blocks_commit(self, tmp_path):
        """P1-3：临时文件无法保留原权限 → tmp_chmod_failed，阻止提交，原文保留。"""
        import os as _os
        import stat as _stat

        p = tmp_path / "secret.txt"
        p.write_text("v1", encoding="utf-8")
        _os.chmod(p, 0o600)
        base = compute_fingerprint(p).sha256

        real_chmod = _os.chmod

        def chmod_fails_on_tmp(path, mode, **kwargs):
            # 只让临时文件的 chmod 失败；锁文件的 chmod 正常（Path.chmod 底层
            # 也是 os.chmod，全局失效会让锁获取直接失败）
            if "agentfs-tmp" in str(path):
                raise OSError("chmod failed (simulated)")
            real_chmod(path, mode, **kwargs)

        # 临时文件优先用 fchmod（同一 fd，避免路径 TOCTOU）；模拟它失败以
        # 验证提交前阻止替换的既有契约。锁文件的 fchmod 失败会被忽略。
        with patch("os.fchmod", side_effect=OSError("chmod failed (simulated)")), patch(
            "os.chmod", side_effect=chmod_fails_on_tmp
        ):
            result = write_if_match(p, b"v2", base)

        assert result.status == "error"
        assert result.commit_state == "not_committed"
        assert result.reason_code == "tmp_chmod_failed"
        assert "0o600" in (result.detail or "")
        assert _stat.S_IMODE(_os.stat(p).st_mode) == 0o600  # 原文与权限都保留
        assert p.read_text(encoding="utf-8") == "v1"

    def test_mode_read_failure_blocks_commit(self, tmp_path):
        """P2-2：读取原权限失败 → mode_read_failed 阻止提交，0755 权限与原文保留。

        旧实现 stat 失败时 original_mode=None 跳过 chmod，替换后文件以临时
        文件的 0600 权限落地（丢失可执行位）却返回 verified。
        """
        import os as _os
        import stat as _stat

        p = tmp_path / "script.sh"
        p.write_text("#!/bin/sh\n", encoding="utf-8")
        _os.chmod(p, 0o755)
        base = compute_fingerprint(p).sha256

        with patch(
            "agent_fs.write_atomic._read_original_mode",
            side_effect=OSError("stat failed (simulated)"),
        ):
            result = write_if_match(p, b"#!/bin/sh\necho hi\n", base)

        assert result.status == "error"
        assert result.commit_state == "not_committed"
        assert result.reason_code == "mode_read_failed"
        assert result.previous_sha256 == base  # 基线已读过，如实回填
        assert "stat failed" in (result.detail or "")
        # 原文与可执行位都保留：没有发生替换
        assert p.read_text(encoding="utf-8") == "#!/bin/sh\n"
        assert _stat.S_IMODE(_os.stat(p).st_mode) == 0o755

    def test_read_original_mode_reads_target(self, tmp_path):
        """_read_original_mode 独立入口：返回目标权限位（含可执行位）。"""
        import os as _os

        from agent_fs.write_atomic import _read_original_mode

        p = tmp_path / "run.sh"
        p.write_text("x", encoding="utf-8")
        _os.chmod(p, 0o755)
        assert _read_original_mode(p) == 0o755


class TestHashCaseNormalizationWrite:
    def test_uppercase_baseline_and_target_accepted(self, tmp_path):
        """审查 P2-6：write-if-match 传大写 Hbase/Htarget 应正常写入 verified。"""
        p = tmp_path / "a.txt"
        p.write_text("v1", encoding="utf-8")
        base = compute_fingerprint(p).sha256
        target = hash_bytes(b"v2")

        result = write_if_match(p, b"v2", base.upper(), desired_sha256=target.upper())

        assert result.status == STATUS_VERIFIED
        assert result.desired_sha256 == target  # 输出归一化为小写
        assert p.read_text(encoding="utf-8") == "v2"


@pytest.mark.skipif(
    not (sys.platform == "darwin" or os.name == "nt"),
    reason="仅大小写不敏感文件系统上两个路径指向同一 inode",
)
def test_case_alias_shares_normalized_lock_key(tmp_path):
    """大小写不同的同一文件必须命中同一把规范化锁，否则双写会互相覆盖。"""
    upper = tmp_path / "Foo.txt"
    upper.write_text("x", encoding="utf-8")
    lower = tmp_path / "foo.txt"

    assert os.path.samefile(upper, lower)
    assert _lock_file_path(upper) == _lock_file_path(lower)


def test_lock_file_size_stays_one_byte_after_repeated_open(tmp_path):
    """锁文件必须定长：旧实现用 O_APPEND 写 '0'，Windows 上每次取锁增长 1 字节。"""
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    lock_path = _lock_file_path(target)

    for _ in range(5):
        handle = _open_lock_file(lock_path)
        handle.close()

    assert lock_path.stat().st_size == 1


def test_temp_file_exclusive_create_does_not_truncate_preexisting(tmp_path):
    """预置同名临时文件时不得截断/覆盖，写入应失败且目标原文保留。"""
    p = tmp_path / "a.txt"
    original = "original"
    p.write_text(original, encoding="utf-8")
    base = compute_fingerprint(p).sha256

    squatter = tmp_path / f".{p.name}.agentfs-tmp-deadbeef"
    squatter.write_text("do not touch", encoding="utf-8")

    class FixedUuid:
        hex = "deadbeef" + "0" * 24

    with patch("agent_fs.write_atomic.uuid.uuid4", return_value=FixedUuid()):
        result = write_if_match(p, b"new content", base)

    assert result.status == STATUS_ERROR
    assert result.reason_code == "write_temp_file_failed"
    assert p.read_text(encoding="utf-8") == original
    assert squatter.read_text(encoding="utf-8") == "do not touch"
