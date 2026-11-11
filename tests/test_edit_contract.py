"""edit-if-match dry-run 与 Htarget 校验契约测试（SPEC A03/A04/A05）。

覆盖：
- A03 预览后目标被第三方修改 → 实际写入重新校验并拒绝，不因 dry-run 成功而绕过
- A04 预览后 edits/内容材料被改动 → Htarget 校验失败（prepared_content_changed），目标不变
- A05 链式/自包含编辑：预览与提交结果一致，合法编辑不误拒绝
"""

from __future__ import annotations

import json

from agent_fs.cli import main
from agent_fs.edit_if_match import edit_if_match, prepare_edits
from agent_fs.fingerprint import compute_fingerprint, hash_bytes
from agent_fs.result import (
    COMMIT_COMMITTED,
    COMMIT_NOT_COMMITTED,
    STATUS_PREPARED,
    STATUS_REJECTED,
    STATUS_VERIFIED,
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return compute_fingerprint(path).sha256


def _edits_file(tmp_path, edits):
    f = tmp_path / "edits.json"
    f.write_text(json.dumps(edits), encoding="utf-8")
    return str(f)


class TestPrepareEdits:
    def test_dry_run_returns_prepared_with_hashes(self, tmp_path):
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        r = prepare_edits(target, _edits_file(tmp_path, [{"old": "alpha", "new": "beta"}]), sha)

        assert r.status == STATUS_PREPARED
        assert r.commit_state == COMMIT_NOT_COMMITTED
        assert r.reason_code == "prepared"
        assert r.expected_sha256 == sha  # Hbase
        assert r.desired_sha256 == hash_bytes(b"# T\n\nbeta\n")  # Htarget
        assert r.applied_edits == 1
        assert r.edits_file.endswith("edits.json")
        assert r.edits_file_sha256 is not None
        # 目标未被修改
        assert target.read_text(encoding="utf-8") == "# T\n\nalpha\n"

    def test_dry_run_does_not_touch_target_or_locks(self, tmp_path):
        """预览绝不修改目标；不留下任何锁痕迹（只读纯度）。"""
        import os

        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        r = prepare_edits(target, _edits_file(tmp_path, [{"old": "alpha", "new": "beta"}]), sha)

        assert r.status == STATUS_PREPARED
        assert target.read_text(encoding="utf-8") == "# T\n\nalpha\n"
        home = os.environ["AGENT_FS_HOME"]
        locks_dir = os.path.join(home, "locks")
        assert not os.path.exists(locks_dir)

    def test_dry_run_baseline_mismatch_rejected(self, tmp_path):
        target = tmp_path / "doc.md"
        old_sha = _write(target, "# T\n\nalpha\n")
        _write(target, "# T\n\nCHANGED\n")
        r = prepare_edits(target, _edits_file(tmp_path, [{"old": "alpha", "new": "x"}]), old_sha)

        assert r.status == STATUS_REJECTED
        assert r.reason_code == "hash_mismatch"
        assert r.previous_sha256 == compute_fingerprint(target).sha256

    def test_dry_run_anchor_failures_rejected(self, tmp_path):
        target = tmp_path / "doc.md"
        sha = _write(target, "dup\n\ndup\n")
        r = prepare_edits(target, _edits_file(tmp_path, [{"old": "dup", "new": "x"}]), sha)

        assert r.status == STATUS_REJECTED
        assert r.reason_code == "anchor_not_unique"
        assert target.read_text(encoding="utf-8") == "dup\n\ndup\n"

    def test_dry_run_invalid_edits_file(self, tmp_path):
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n")
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        r = prepare_edits(target, str(bad), sha)

        assert r.status == "error"
        assert r.reason_code == "invalid_edits_file"


class TestA03TargetChangedAfterPreview:
    def test_actual_write_revalidates_after_external_change(self, tmp_path):
        """A03：dry-run 成功后目标被修改，实写必须拒绝，不得绕过基线校验。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        edits = _edits_file(tmp_path, [{"old": "alpha", "new": "beta"}])

        preview = prepare_edits(target, edits, sha)
        assert preview.status == STATUS_PREPARED

        # 预览后协作者修改目标
        _write(target, "# T\n\nalpha-CHANGED\n")

        result = edit_if_match(target, edits, sha)
        assert result.status == STATUS_REJECTED
        assert result.reason_code == "hash_mismatch"
        assert result.commit_state == COMMIT_NOT_COMMITTED
        # 协作者内容保留
        assert "alpha-CHANGED" in target.read_text(encoding="utf-8")

    def test_cli_dry_run_then_changed_target_rejected(self, tmp_path, capsys):
        """CLI 端到端：dry-run 通过不授权后续写入绕过校验。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        edits = _edits_file(tmp_path, [{"old": "alpha", "new": "beta"}])

        rc = main(
            ["edit-if-match", str(target), "--expected-sha256", sha,
             "--edits-file", edits, "--dry-run"]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["status"] == "prepared"

        _write(target, "# T\n\nalpha-CHANGED\n")
        rc = main(
            ["edit-if-match", str(target), "--expected-sha256", sha, "--edits-file", edits]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out["reason_code"] == "hash_mismatch"


class TestA04MaterialChangedAfterPreview:
    def test_edits_file_changed_after_preview_rejected(self, tmp_path):
        """A04：预览后 edits 材料被改动 → 实际变换结果与声明的 Htarget 不一致。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        edits_path = tmp_path / "edits.json"
        edits_path.write_text(json.dumps([{"old": "alpha", "new": "beta"}]), encoding="utf-8")

        preview = prepare_edits(target, str(edits_path), sha)
        assert preview.status == STATUS_PREPARED
        declared_target = preview.desired_sha256

        # 预览后有人改了编辑材料（换成不同的替换文本）
        edits_path.write_text(json.dumps([{"old": "alpha", "new": "GAMMA"}]), encoding="utf-8")

        result = edit_if_match(target, str(edits_path), sha, desired_sha256=declared_target)
        assert result.status == STATUS_REJECTED
        assert result.reason_code == "prepared_content_changed"
        assert result.commit_state == COMMIT_NOT_COMMITTED
        # 目标不变
        assert target.read_text(encoding="utf-8") == "# T\n\nalpha\n"

    def test_content_file_changed_after_preview_rejected(self, tmp_path):
        """A04（write 变体）：预览后内容材料被改动 → Htarget 校验失败，目标不变。"""
        from agent_fs.write_atomic import write_if_match

        content_path = tmp_path / "new-content.txt"
        content_path.write_text("v2-content", encoding="utf-8")
        target = tmp_path / "a.txt"
        sha = _write(target, "v1")

        declared_target = hash_bytes(b"v2-content")
        content_path.write_text("v2-TAMPERED", encoding="utf-8")  # 材料被改

        result = write_if_match(
            target, content_path.read_bytes(), sha, desired_sha256=declared_target
        )
        assert result.status == STATUS_REJECTED
        assert result.reason_code == "prepared_content_changed"
        assert target.read_text(encoding="utf-8") == "v1"

    def test_cli_dry_run_ignores_declared_target_with_notice(self, tmp_path, capsys):
        """--dry-run 与 --expected-result-sha256 同给：忽略声明值并在输出说明。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        edits = _edits_file(tmp_path, [{"old": "alpha", "new": "beta"}])

        rc = main(
            ["edit-if-match", str(target), "--expected-sha256", sha,
             "--edits-file", edits, "--dry-run", "--expected-result-sha256", "e" * 64]
        )
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["status"] == "prepared"
        # 输出的 Htarget 是实际变换结果，不是声明值
        assert out["desired_sha256"] == hash_bytes(b"# T\n\nbeta\n")
        assert "忽略" in out.get("detail", "")


class TestA05PreviewMatchesCommit:
    def test_dry_run_target_equals_committed_observed(self, tmp_path):
        """A05：dry-run 的 Htarget == 实际提交后的稳定回读哈希。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        edits = _edits_file(tmp_path, [{"old": "alpha", "new": "beta"}])

        preview = prepare_edits(target, edits, sha)
        result = edit_if_match(target, edits, sha)

        assert result.status == STATUS_VERIFIED
        assert result.commit_state == COMMIT_COMMITTED
        assert preview.desired_sha256 == result.observed_sha256
        assert result.observed_sha256 == compute_fingerprint(target).sha256

    def test_chained_edit_preview_and_commit_agree(self, tmp_path):
        """A05：链式编辑（a→b 后 c→a）预览与提交结果一致，不误拒绝。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "aaa\n\nccc\n")
        edits = _edits_file(
            tmp_path, [{"old": "aaa", "new": "bbb"}, {"old": "ccc", "new": "aaa"}]
        )

        preview = prepare_edits(target, edits, sha)
        assert preview.status == STATUS_PREPARED

        result = edit_if_match(target, edits, sha)
        assert result.status == STATUS_VERIFIED
        assert preview.desired_sha256 == result.observed_sha256
        assert target.read_text(encoding="utf-8") == "bbb\n\naaa\n"

    def test_self_contained_edit_preview_and_commit_agree(self, tmp_path):
        """A05：前缀自包含编辑（abc 中 a→ab）预览与提交一致，不误判 post_check_failed。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "abc")
        edits = _edits_file(tmp_path, [{"old": "a", "new": "ab"}])

        preview = prepare_edits(target, edits, sha)
        assert preview.status == STATUS_PREPARED

        result = edit_if_match(target, edits, sha)
        assert result.status == STATUS_VERIFIED
        assert preview.desired_sha256 == result.observed_sha256
        assert target.read_text(encoding="utf-8") == "abbc"

    def test_edit_with_declared_target_verified(self, tmp_path):
        """声明的 Htarget 与实际变换结果一致 → 正常提交 verified。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        edits = _edits_file(tmp_path, [{"old": "alpha", "new": "beta"}])

        declared = hash_bytes(b"# T\n\nbeta\n")
        result = edit_if_match(target, edits, sha, desired_sha256=declared)

        assert result.status == STATUS_VERIFIED
        assert result.observed_sha256 == declared


class TestA04SingleReadMaterial:
    def test_edits_file_read_exactly_once(self, tmp_path, monkeypatch):
        """审查 P2-4：edits 材料只读一次——解析与哈希必须来自同一份字节。

        read_bytes 注入单次返回：若实现读取两次（解析一次、算哈希一次），
        第二次调用会 StopIteration 使本测试失败。"""
        target = tmp_path / "doc.md"
        sha = _write(target, "# T\n\nalpha\n")
        raw = json.dumps([{"old": "alpha", "new": "beta"}]).encode("utf-8")
        # 材料文件不存在，只走注入路径
        missing = tmp_path / "injected-edits.json"

        original_read = __import__("pathlib").Path.read_bytes
        reads = {"n": 0}

        def single_read(self, *args, **kwargs):
            if self != missing:
                return original_read(self, *args, **kwargs)  # 其他文件正常读取
            reads["n"] += 1
            return raw

        monkeypatch.setattr("pathlib.Path.read_bytes", single_read)
        r = prepare_edits(target, str(missing), sha)

        assert reads["n"] == 1, "edits 材料必须只读取一次"

        assert r.status == STATUS_PREPARED
        assert r.edits_file_sha256 == hash_bytes(raw)  # 与解析同一份字节
        assert r.desired_sha256 == hash_bytes(b"# T\n\nbeta\n")
