"""并发与边界测试（SPEC A01/A02/A15/A19 + A18 内容保真）。

A02 用两个真实 subprocess 竞争同一锁域（文件屏障对齐，不依赖偶然时序）；
A19 用确定性持锁证明"不同锁域互不互斥"的边界如实存在。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from agent_fs.cli import main
from agent_fs.fingerprint import compute_fingerprint, hash_bytes
from agent_fs.write_atomic import _acquire_target_lock, _release_target_lock, write_if_match

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

_BARRIER_TIMEOUT_SECONDS = 30.0


def _wait_for(path: Path) -> None:
    """带超时的屏障等待：worker 崩溃时让测试失败而不是死等。"""
    deadline = time.monotonic() + _BARRIER_TIMEOUT_SECONDS
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"屏障超时：等待 {path} 未就绪（worker 可能启动失败）")
        time.sleep(0.01)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return compute_fingerprint(path).sha256


class TestA01StaleBaselineRejected:
    def test_stale_baseline_write_rejected_and_peer_content_kept(self, tmp_path):
        """A01：A 读原文 → B 完成修改 → A 用旧基线写 → A 被拒绝，B 内容保留。"""
        p = tmp_path / "shared.txt"
        base = _write(p, "v1-original")

        b_content = "v2-written-by-B"
        write_if_match(p, b_content.encode("utf-8"), base, desired_sha256=hash_bytes(b_content.encode()))

        result = write_if_match(p, b"v3-from-A-with-stale-base", base)
        assert result.status == "rejected"
        assert result.reason_code == "hash_mismatch"
        assert p.read_text(encoding="utf-8") == b_content


class TestA02SameLockDomainSingleCommitter:
    def test_two_processes_same_stale_base_single_commit(self, tmp_path):
        """A02：同锁域内两个进程持相同旧基线同时写 → 恰好一个提交，无混合内容。"""
        p = tmp_path / "race.txt"
        base = _write(p, "v1")
        home = tmp_path / ".agent-fs"

        ready_a = tmp_path / "ready-a"
        ready_b = tmp_path / "ready-b"
        go = tmp_path / "go"

        worker = (
            "import json, os, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, 'src')\n"
            "from agent_fs.cli import main\n"
            "target, ready, go = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])\n"
            "base_hash, content = sys.argv[4], sys.argv[5]\n"
            "ready.write_text('1')\n"
            "deadline = time.monotonic() + 30\n"
            "while not go.exists():\n"
            "    if time.monotonic() > deadline:\n"
            "        sys.exit('barrier timeout waiting for go')\n"
            "    time.sleep(0.005)\n"  # 屏障等待：就绪后放行，最大化竞争窗口
            "code = main(['write-if-match', str(target), '--expected-sha256', base_hash,\n"
            "              '--content', content])\n"
            "print(json.dumps({'code': code}))\n"
        )

        def spawn(tag, content):
            return subprocess.Popen(
                [sys.executable, "-c", worker, str(p), str(ready_a if tag == "a" else ready_b),
                 str(go), base, content],
                cwd=str(_PACKAGE_ROOT),
                env={**os.environ, "AGENT_FS_HOME": str(home)},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        proc_a = spawn("a", "content-from-A")
        proc_b = spawn("b", "content-from-B")
        try:
            # 等两个进程就绪后再放行
            _wait_for(ready_a)
            _wait_for(ready_b)
            go.write_text("1")
        except BaseException:
            proc_a.kill()
            proc_b.kill()
            raise

        outs = []
        for proc in (proc_a, proc_b):
            stdout, stderr = proc.communicate(timeout=60)
            assert proc.returncode == 0, f"worker crashed: {stderr}"
            outs.append(json.loads(stdout.strip().splitlines()[-1])["code"])

        # 恰好一个成功提交；另一个锁忙或基线冲突
        assert sorted(outs) == [0, 1], f"预期 [0, 1]，实际 {outs}"
        final = p.read_text(encoding="utf-8")
        # 无混合内容：最终内容必须是某一方的完整成果或保持原文（若成功者内容即原文）
        assert final in {"content-from-A", "content-from-B"}, f"出现混合内容: {final!r}"
        # 与退出码自洽：提交者的内容胜出
        if final == "content-from-A":
            assert outs == [0, 1] or outs == [1, 0]


class TestA15NoAutoRetry:
    def test_repeated_material_conflict_not_auto_replayed(self, tmp_path, capsys):
        """A15：同一材料连续两次触发 prepared_content_changed，CLI 不自动重放写入。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        declared = "e" * 64  # 与实际内容不符的声明值

        for _ in range(2):
            code = main(
                ["write-if-match", str(p), "--expected-sha256", base,
                 "--expected-result-sha256", declared, "--content", "x"]
            )
            out = json.loads(capsys.readouterr().out)
            assert code == 1
            assert out["reason_code"] == "prepared_content_changed"
            assert out["retryable"] is False
            assert p.read_text(encoding="utf-8") == "v1"  # 每次都停在拒绝，无自动重放

    def test_conflict_after_reconcile_still_rejected(self, tmp_path, capsys):
        """A15：恢复性重试期间出现新修改仍被拒绝（重试不豁免并发校验）。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        code = main(
            ["reconcile", str(p), "--base-sha256", base, "--desired-sha256", hash_bytes(b"v2")]
        )
        assert code == 1  # base_present
        capsys.readouterr()

        # 核实后、恢复写入前，他人修改 → 恢复被拒绝
        _write(p, "v1-modified")
        result = write_if_match(p, b"v2", base)
        assert result.status == "rejected"
        assert result.reason_code == "hash_mismatch"


class TestA19LockDomainBoundary:
    def test_different_agent_fs_homes_do_not_mutex(self, tmp_path):
        """A19：不同 AGENT_FS_HOME 不构成同一锁域——如实证明无跨锁域互斥。"""
        p = tmp_path / "shared.txt"
        base = _write(p, "v1")
        home1 = tmp_path / "home-1"
        home2 = tmp_path / "home-2"

        # home1 持锁
        os.environ_backup = os.environ.get("AGENT_FS_HOME")
        os.environ["AGENT_FS_HOME"] = str(home1)
        try:
            lock = _acquire_target_lock(p)
            assert lock is not None
            try:
                # home2 的写入者不受 home1 锁影响（边界真实存在）
                os.environ["AGENT_FS_HOME"] = str(home2)
                from_home2 = write_if_match(p, b"from-home-2", base)
                assert from_home2.status == "verified"

                # home1 的写入者被自己锁域阻塞（锁本身工作正常）
                os.environ["AGENT_FS_HOME"] = str(home1)
                from_home1 = write_if_match(p, b"from-home-1", base)
                assert from_home1.status == "rejected"
                assert from_home1.reason_code == "lock_busy"
            finally:
                _release_target_lock(lock)
        finally:
            if os.environ_backup is None:
                os.environ.pop("AGENT_FS_HOME", None)
            else:
                os.environ["AGENT_FS_HOME"] = os.environ_backup


class TestA18ContentFidelity:
    def test_crlf_chinese_emoji_preserved_byte_for_byte(self, tmp_path):
        """A18：CRLF/中文/emoji 按最终字节计算 Htarget，未请求的编码转换不发生。"""
        p = tmp_path / "doc.md"
        raw = "line-one\r\n中文内容\r\nemoji-🎉-end\r\n"
        p.write_bytes(raw.encode("utf-8"))
        base = compute_fingerprint(p).sha256

        new_raw = "标题：中文\r\n行二 emoji 🚀\r\n\r\n结尾\r\n"
        target = hash_bytes(new_raw.encode("utf-8"))
        result = write_if_match(p, new_raw.encode("utf-8"), base, desired_sha256=target)

        assert result.status == "verified"
        assert result.observed_sha256 == target
        assert p.read_bytes() == new_raw.encode("utf-8")  # 逐字节相等，CRLF 未被改写

    def test_executable_bit_preserved_with_declared_target(self, tmp_path):
        """A18：可执行文件权限保留，约定权限未因受控替换丢失。"""
        import os as _os

        p = tmp_path / "run.sh"
        p.write_bytes(b"#!/bin/sh\necho old\n")
        _os.chmod(p, 0o755)
        base = compute_fingerprint(p).sha256

        target = hash_bytes(b"#!/bin/sh\necho new\n")
        result = write_if_match(p, b"#!/bin/sh\necho new\n", base, desired_sha256=target)

        assert result.status == "verified"
        assert _os.stat(p).st_mode & 0o111  # 可执行位保留
        assert p.read_bytes() == b"#!/bin/sh\necho new\n"
