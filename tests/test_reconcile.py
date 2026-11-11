"""reconcile 只读核实命令测试（SPEC A09–A14）。

重点锁定只读纯度（A11）：reconcile 绝不获取协作锁、绝不修改目标——
即使注入 os.replace/锁获取在调用时会抛错，reconcile 仍正常完成且不创建 locks 目录。
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from agent_fs.cli import main
from agent_fs.fingerprint import compute_fingerprint, hash_bytes
from agent_fs.reconcile import reconcile
from agent_fs.write_atomic import write_if_match


def _run(capsys, argv):
    exit_code = main(argv)
    out = capsys.readouterr().out
    return exit_code, json.loads(out)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return compute_fingerprint(path).sha256


class TestReconcileCore:
    def test_desired_present_after_successful_write(self, tmp_path):
        """A09：替换成功但响应丢失 → reconcile 判 desired_present。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        target = hash_bytes(b"v2")
        # 模拟"实际已写入但调用方没有收到响应"：直接提交受控写入，丢弃结果
        write_if_match(p, b"v2", base)

        r = reconcile(p, base, target)
        assert r.observed_status == "desired_present"
        assert r.exit_code == 0
        assert r.observed_sha256 == target
        assert r.observed_at  # 每次输出记录观察时间

    def test_base_present_when_never_written(self, tmp_path):
        """A10：写前中断（从未写入）→ base_present；原基线受控恢复仍可行。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        target = hash_bytes(b"v2")

        r = reconcile(p, base, target)
        assert r.observed_status == "base_present"
        assert r.exit_code == 1

        # 原调用确认结束后：用原 Hbase/Htarget 受控恢复
        result = write_if_match(p, b"v2", base, desired_sha256=target)
        assert result.status == "verified"

    def test_base_present_then_new_conflict_rejected(self, tmp_path):
        """A10 后半：恢复性重试期间出现新冲突仍被拒绝。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        target = hash_bytes(b"v2")

        assert reconcile(p, base, target).observed_status == "base_present"
        # 核实与写入之间发生新修改 → 恢复被拒绝
        _write(p, "v1-modified-by-someone")
        result = write_if_match(p, b"v2", base, desired_sha256=target)
        assert result.status == "rejected"
        assert result.reason_code == "hash_mismatch"

    def test_diverged_when_content_differs_from_both(self, tmp_path):
        """A12：他人修改或外部工具部分写入 → diverged，内容原样保留。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        target = hash_bytes(b"v2")
        other = _write(p, "someone-else-edit")

        r = reconcile(p, base, target)
        assert r.observed_status == "diverged"
        assert r.exit_code == 1
        assert r.observed_sha256 == other
        assert p.read_text(encoding="utf-8") == "someone-else-edit"  # 保留已有内容

    def test_unavailable_when_target_missing(self, tmp_path):
        """A13：目标消失 → unavailable，不隐式新建。"""
        missing = tmp_path / "nope.txt"
        r = reconcile(missing, "a" * 64, "b" * 64)
        assert r.observed_status == "unavailable"
        assert r.exit_code == 3
        assert r.reason_code == "unavailable_target_missing"
        assert not missing.exists()

    def test_unavailable_when_read_fails(self, tmp_path):
        """A13：目标不可读（注入 PermissionError）→ unavailable。"""
        p = tmp_path / "a.txt"
        _write(p, "v1")
        with patch(
            "agent_fs.reconcile.compute_stable_fingerprint",
            side_effect=PermissionError(13, "Permission denied"),
        ):
            r = reconcile(p, "a" * 64, "b" * 64)
        assert r.observed_status == "unavailable"
        assert r.exit_code == 3
        assert r.reason_code == "unavailable_unreadable"

    def test_unavailable_when_persistently_unstable(self, tmp_path):
        """A13：持续不稳定 → unavailable（不把中间态当结论）。"""
        from agent_fs.fingerprint import StableFingerprintResult

        p = tmp_path / "a.txt"
        _write(p, "v1")
        with patch(
            "agent_fs.reconcile.compute_stable_fingerprint",
            return_value=StableFingerprintResult(
                stable=False, fingerprint=None, attempts=3, reason="changed_during_read"
            ),
        ):
            r = reconcile(p, "a" * 64, "b" * 64)
        assert r.observed_status == "unavailable"
        assert r.reason_code == "unavailable_unreadable"

    def test_hbase_equals_htarget_prefers_desired_present(self, tmp_path):
        """A14：Hbase == Htarget（no-op 写）→ 优先判 desired_present，不产生重试建议。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")

        r = reconcile(p, base, base)
        assert r.observed_status == "desired_present"
        assert r.exit_code == 0
        assert "禁止重复应用" in r.to_dict()["next_action"]


class TestA11ReadOnlyPurity:
    def test_reconcile_never_locks_or_writes(self, tmp_path):
        """A11：锁获取或替换被调用即抛错，reconcile 仍成功且不建 locks 目录。"""

        def _forbidden(*args, **kwargs):
            raise AssertionError("reconcile 不得获取协作锁或执行替换")

        p = tmp_path / "a.txt"
        base = _write(p, "v1")

        with patch("agent_fs.write_atomic._acquire_target_lock", side_effect=_forbidden), patch(
            "os.replace", side_effect=_forbidden
        ):
            r = reconcile(p, base, hash_bytes(b"v2"))

        assert r.observed_status == "base_present"
        home = os.environ["AGENT_FS_HOME"]
        assert not os.path.exists(os.path.join(home, "locks"))

    def test_reconcile_cli_keeps_content_intact(self, tmp_path, capsys):
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        _write(p, "someone-else-edit")  # 当前内容既非 Hbase 也非 Htarget
        code, out = _run(
            capsys,
            ["reconcile", str(p), "--base-sha256", base, "--desired-sha256", "c" * 64],
        )
        assert code == 1
        assert out["status"] == "diverged"
        assert p.read_text(encoding="utf-8") == "someone-else-edit"


class TestReconcileCli:
    def test_cli_desired_present_exit_0(self, tmp_path, capsys):
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        write_if_match(p, b"v2", base)
        code, out = _run(
            capsys,
            ["reconcile", str(p), "--base-sha256", base,
             "--desired-sha256", hash_bytes(b"v2")],
        )
        assert code == 0
        assert out["status"] == "desired_present"
        assert out["command"] == "reconcile"
        assert out["schema_version"] == 2
        assert out["observed_at"]

    def test_cli_invalid_hash_exit_2(self, tmp_path, capsys):
        p = tmp_path / "a.txt"
        _write(p, "v1")
        code, out = _run(
            capsys,
            ["reconcile", str(p), "--base-sha256", "tooshort", "--desired-sha256", "b" * 64],
        )
        assert code == 2
        assert out["reason_code"] == "invalid_sha256_format"
        assert out["category"] == "caller_error"
        assert "usage" not in out.get("error", "")

    def test_cli_missing_target_exit_3(self, tmp_path, capsys):
        """A13：目标消失 → exit 3，不把"文件不存在"当路径错误降级。"""
        code, out = _run(
            capsys,
            ["reconcile", str(tmp_path / "nope.txt"),
             "--base-sha256", "a" * 64, "--desired-sha256", "b" * 64],
        )
        assert code == 3
        assert out["status"] == "unavailable"
        assert out["retryable"] is False


class TestHashCaseNormalization:
    def test_uppercase_hash_not_reported_as_diverged(self, tmp_path, capsys):
        """审查 P2-6：大写 Htarget/Hbase 校验通过后应归一化——文件已是预期内容
        时传大写哈希必须判 desired_present，不得误报 diverged。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        target = hash_bytes(b"v2")
        write_if_match(p, b"v2", base)

        code, out = _run(
            capsys,
            ["reconcile", str(p), "--base-sha256", base.upper(),
             "--desired-sha256", target.upper()],
        )
        assert code == 0
        assert out["status"] == "desired_present"

    def test_core_reconcile_lowercases_params(self, tmp_path):
        """库级调用传大写同样归一化（防御性）。"""
        p = tmp_path / "a.txt"
        base = _write(p, "v1")
        r = reconcile(p, base.upper(), hash_bytes(b"v2").upper())
        assert r.observed_status == "base_present"
