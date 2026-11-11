"""edit-if-match 行为测试（锁内多点编辑闭环）。

覆盖：
- 正常路径：多锚点一次应用 + 结果校验 + 新哈希返回
- 并发修改：基线不匹配时拒绝覆盖
- 锚点失配：最新内容中锚点不存在/不唯一时拒绝
- edits 文件格式非法拒绝
- 参数误用只返回结构化建议（CLI 层）
"""

import json
from unittest.mock import patch

import pytest

from agent_fs.cli import main
from agent_fs.edit_if_match import edit_if_match
from agent_fs.fingerprint import compute_fingerprint
from agent_fs.result import COMMIT_COMMITTED, COMMIT_NOT_COMMITTED, STATUS_VERIFIED


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return compute_fingerprint(path).sha256


def _edits_file(tmp_path, edits):
    f = tmp_path / "edits.json"
    f.write_text(json.dumps(edits), encoding="utf-8")
    return str(f)


class TestEditIfMatchCore:
    def test_multi_edit_applied_atomically(self, tmp_path):
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n\nbeta\n")
        r = edit_if_match(
            target,
            _edits_file(
                tmp_path,
                [
                    {"old": "# T", "new": "# 标题"},
                    {"old": "alpha\n\nbeta", "new": "gamma\n\ndelta"},
                ],
            ),
            sha,
        )
        assert r.commit_state == COMMIT_COMMITTED
        assert r.reason_code == "edits_applied"
        assert r.applied_edits == 2
        assert target.read_text(encoding="utf-8") == "# 标题\n\ngamma\n\ndelta\n"
        # 稳定回读哈希等于预期结果
        assert r.observed_sha256 == r.desired_sha256
        assert r.observed_sha256 == compute_fingerprint(target).sha256

    def test_baseline_mismatch_rejects_overwrite(self, tmp_path):
        target = tmp_path / "doc.md"
        old_sha = _write(target, "# T\n\nalpha\n")
        # 协作者修改
        _write(target, "# T\n\nCHANGED\n")
        r = edit_if_match(
            target,
            _edits_file(tmp_path, [{"old": "alpha", "new": "x"}]),
            old_sha,
        )
        assert r.commit_state == COMMIT_NOT_COMMITTED
        assert r.reason_code == "hash_mismatch"
        assert "CHANGED" in target.read_text(encoding="utf-8")  # 协作者内容未被覆盖

    def test_anchor_missing_rejects(self, tmp_path):
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        r = edit_if_match(
            target,
            _edits_file(tmp_path, [{"old": "nonexistent", "new": "x"}]),
            sha,
        )
        assert r.commit_state == COMMIT_NOT_COMMITTED
        assert r.reason_code == "anchor_not_found"

    def test_anchor_not_unique_rejects(self, tmp_path):
        target = tmp_path / "doc.md"
        sha = _write(target, "dup\n\ndup\n")
        r = edit_if_match(
            target,
            _edits_file(tmp_path, [{"old": "dup", "new": "x"}]),
            sha,
        )
        assert r.commit_state == COMMIT_NOT_COMMITTED
        assert r.reason_code == "anchor_not_unique"

    def test_invalid_edits_file(self, tmp_path):
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n")
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        r = edit_if_match(target, str(bad), sha)
        assert r.commit_state == COMMIT_NOT_COMMITTED
        assert r.status == "error"
        assert r.reason_code == "invalid_edits_file"

    def test_lock_busy_when_held(self, tmp_path, monkeypatch):
        """另一协作者持锁时拒绝（模拟持锁后调用）。"""
        from agent_fs.write_atomic import _acquire_target_lock

        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n")
        lock = _acquire_target_lock(target)
        assert lock is not None
        try:
            r = edit_if_match(
                target,
                _edits_file(tmp_path, [{"old": "# T", "new": "# X"}]),
                sha,
            )
            assert r.commit_state == COMMIT_NOT_COMMITTED
            assert r.reason_code == "lock_busy"
        finally:
            lock.close()

    def test_lock_release_failure_does_not_mask_verified(self, tmp_path):
        """解锁抛异常时写入已成功：必须保留 verified 并记录附属失败。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n")

        with patch(
            "agent_fs.edit_if_match._release_target_lock",
            side_effect=OSError("unlock failed (simulated)"),
        ):
            result = edit_if_match(
                target,
                _edits_file(tmp_path, [{"old": "# T", "new": "# X"}]),
                sha,
            )

        assert result.status == STATUS_VERIFIED
        assert result.commit_state == COMMIT_COMMITTED
        assert result.reason_code == "edits_applied"
        assert any("lock_release_failed" in f for f in result.ancillary_failures)
        assert target.read_text(encoding="utf-8") == "# X\n"


class TestEditIfMatchCli:
    def test_cli_happy_path_returns_json_with_next_action(self, tmp_path, capsys):
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        rc = main(
            [
                "edit-if-match",
                str(target),
                "--expected-sha256",
                sha,
                "--edits-file",
                _edits_file(tmp_path, [{"old": "alpha", "new": "beta"}]),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["edited"] is True
        assert "next_action" in out

    def test_cli_snapshot_as_baseline(self, tmp_path, capsys, monkeypatch):
        """--snapshot 直接作基线（snapshot ID 不再需要手动转哈希）。"""
        monkeypatch.setenv("AGENT_FS_HOME", str(tmp_path / "home"))
        target = tmp_path / "doc.md"
        target.write_text("# T\n", encoding="utf-8")
        rc = main(["snapshot", str(target)])
        snap = json.loads(capsys.readouterr().out)
        assert rc == 0

        rc = main(
            [
                "edit-if-match",
                str(target),
                "--snapshot",
                snap["snapshot_id"],
                "--edits-file",
                _edits_file(tmp_path, [{"old": "# T", "new": "# X"}]),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 0 and out["edited"] is True

    def test_cli_argument_error_is_json(self, tmp_path, capsys):
        """参数错误输出结构化 JSON（不再是 argparse usage 文本）。"""
        rc = main(["verify", str(tmp_path / "f.md")])  # 缺哈希参数
        out = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert out["category"] == "caller_error"
        assert "suggested_command" in out or "next_action" in out
        assert "usage" not in out.get("error", "")

    def test_cli_verify_snapshot_not_found_structured(self, tmp_path, capsys):
        rc = main(["verify", str(tmp_path / "f.md"), "--snapshot", "snap-nonexistent"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert "snapshot_not_found" in out["error"]
        assert out["category"] == "caller_error"


class TestReadFreshOutputControl:
    def test_max_bytes_truncates_with_marker(self, tmp_path, capsys):
        target = tmp_path / "big.md"
        target.write_text("x" * 5000, encoding="utf-8")
        rc = main(["read-fresh", str(target), "--max-bytes", "100", "--compact"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["content_truncated"] is True
        assert len(out["content"].encode("utf-8")) == 100
        assert out["content_total_bytes"] == 5000

    def test_max_bytes_truncates_multibyte_safely(self, tmp_path, capsys):
        """中文按字节截断：不超指定字节数，且不产生半个字符。"""
        target = tmp_path / "cn.md"
        target.write_text("汉" * 100, encoding="utf-8")  # 每字 3 字节
        rc = main(["read-fresh", str(target), "--max-bytes", "10", "--compact"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["content_truncated"] is True
        assert len(out["content"].encode("utf-8")) <= 10  # 不超字节数
        assert out["content_total_bytes"] == 300
        # 10 字节 = 3 个完整汉字（9 字节），第 4 个被安全丢弃
        assert out["content"] == "汉汉汉"

    def test_no_content_omits_body(self, tmp_path, capsys):
        target = tmp_path / "f.md"
        target.write_text("hello\n", encoding="utf-8")
        rc = main(["read-fresh", str(target), "--no-content", "--compact"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["content"] is None
        assert out.get("sha256")

    def test_output_writes_file(self, tmp_path, capsys):
        target = tmp_path / "f.md"
        target.write_text("hello world\n", encoding="utf-8")
        out_file = tmp_path / "out.md"
        rc = main(["read-fresh", str(target), "--output", str(out_file), "--compact"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["content"] is None
        assert out_file.read_text(encoding="utf-8") == "hello world\n"


class TestWriteAtomicSemantics:
    def test_symlink_final_path_rejected(self, tmp_path):
        from agent_fs.write_atomic import write_if_match

        real = tmp_path / "real.md"
        real.write_text("content\n", encoding="utf-8")
        link = tmp_path / "link.md"
        link.symlink_to(real)
        sha = compute_fingerprint(real).sha256

        r = write_if_match(link, b"new\n", sha)
        assert r.commit_state == COMMIT_NOT_COMMITTED
        assert r.reason_code == "final_path_is_symlink"
        # 链接与真实文件都未被破坏
        assert link.is_symlink()
        assert real.read_text(encoding="utf-8") == "content\n"

    def test_executable_bit_preserved(self, tmp_path):
        import os

        from agent_fs.write_atomic import write_if_match

        target = tmp_path / "script.sh"
        target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        os.chmod(target, 0o755)
        sha = compute_fingerprint(target).sha256

        r = write_if_match(target, b"#!/bin/sh\necho new\n", sha)
        assert r.status == STATUS_VERIFIED
        assert os.stat(target).st_mode & 0o111  # 可执行位保留

    def test_special_permission_bits_not_inherited(self, tmp_path):
        """只继承 0o777：setuid/setgid 属于原文件的提权语义，不应带到新内容。"""
        import os

        from agent_fs.write_atomic import write_if_match

        target = tmp_path / "script.sh"
        target.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
        os.chmod(target, 0o4755)
        if not os.stat(target).st_mode & 0o4000:
            pytest.skip("当前文件系统/用户不支持设置 setuid 位")
        sha = compute_fingerprint(target).sha256

        r = write_if_match(target, b"#!/bin/sh\necho new\n", sha)
        assert r.status == STATUS_VERIFIED
        mode = os.stat(target).st_mode
        assert mode & 0o777 == 0o755  # 普通权限位保留
        assert not mode & 0o7000  # setuid/setgid/sticky 被清除


class TestChainedEdits:
    def test_chained_edit_not_false_positive(self, tmp_path):
        """链式编辑 a→b 后 c→a：old 文本重新出现是合法编辑，不是失配。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "aaa\n\nccc\n")
        r = edit_if_match(
            target,
            _edits_file(
                tmp_path,
                [
                    {"old": "aaa", "new": "bbb"},  # a→b
                    {"old": "ccc", "new": "aaa"},  # c→a：a 文本被重新引入
                ],
            ),
            sha,
        )
        assert r.commit_state == COMMIT_COMMITTED
        assert target.read_text(encoding="utf-8") == "bbb\n\naaa\n"

    def test_invalid_edits_file_cli_exit_code_2(self, tmp_path, capsys):
        """invalid_edits_file 属调用方输入错误 → 退出码 2（曾误返 1）。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n")
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        rc = main(
            [
                "edit-if-match",
                str(target),
                "--expected-sha256",
                sha,
                "--edits-file",
                str(bad),
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert out["reason_code"] == "invalid_edits_file"
        assert "next_action" in out


class TestSelfContainedEdit:
    def test_prefix_self_contained_edit(self, tmp_path):
        """终审反例：abc 执行 a→ab——old 是 new 的子串，替换确实发生，
        旧查法误判 post_check_failed。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "abc")
        r = edit_if_match(
            target,
            _edits_file(tmp_path, [{"old": "a", "new": "ab"}]),
            sha,
        )
        assert r.commit_state == COMMIT_COMMITTED
        assert target.read_text(encoding="utf-8") == "abbc"


class TestWriteIfMatchCliContract:
    def test_file_not_found_exit_code_2_with_next_action(self, tmp_path, capsys):
        """write-if-match 对不存在文件 → exit 2（caller_error 约定）+ next_action。"""
        missing = tmp_path / "missing.md"
        rc = main(
            [
                "write-if-match",
                str(missing),
                "--expected-sha256",
                "a" * 64,
                "--content",
                "x",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert out["reason_code"] == "file_not_found"
        assert "next_action" in out

    def test_negative_max_bytes_rejected(self, tmp_path, capsys):
        """--max-bytes 负数是调用方输入错误（负切片语义不合法）→ exit 2。"""
        target = tmp_path / "f.md"
        target.write_text("hello\n", encoding="utf-8")
        rc = main(["read-fresh", str(target), "--max-bytes", "-5", "--compact"])
        out = json.loads(capsys.readouterr().out)
        assert rc == 2
        assert "invalid_max_bytes" in out["error"]
        assert out["category"] == "caller_error"
