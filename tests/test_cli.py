"""cli.py 端到端测试：通过 main() 驱动，校验 JSON 输出与退出码语义。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_fs.cli import main
from agent_fs.read_fresh import ReadFreshResult

# AGENT_FS_HOME 隔离由 tests/conftest.py 的 autouse fixture 统一提供


def _run(capsys, argv):
    exit_code = main(argv)
    out = capsys.readouterr().out
    return exit_code, json.loads(out)


def test_snapshot_then_changed_roundtrip(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("v1", encoding="utf-8")

    code, snap = _run(capsys, ["snapshot", str(p)])
    assert code == 0
    assert "snapshot_id" in snap

    code, res = _run(capsys, ["changed", str(p), "--since", snap["snapshot_id"]])
    assert code == 0
    assert res["changed"] is False

    p.write_text("v2-changed", encoding="utf-8")
    code, res2 = _run(capsys, ["changed", str(p), "--since", snap["snapshot_id"]])
    assert code == 1
    assert res2["changed"] is True


def test_verify_match_and_mismatch(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("content-a", encoding="utf-8")

    code, snap = _run(capsys, ["snapshot", str(p)])
    base_hash = snap["sha256"]

    code, res = _run(capsys, ["verify", str(p), "--expected-sha256", base_hash])
    assert code == 0
    assert res["match"] is True

    code, res2 = _run(capsys, ["verify", str(p), "--expected-sha256", "0" * 64])
    assert code == 1
    assert res2["match"] is False


def test_verify_rejects_malformed_hash_with_error_exit_code(tmp_path, capsys):
    """黑盒评审发现的 Low 问题：verify 之前对格式错误的哈希（长度不对/非法字符）不报错，
    只会得到 match=False，调用方无法区分“真实不匹配”与“参数传错”。
    修复后应返回 EXIT_ERROR(2) 并在输出中明确提示格式错误。
    """
    p = tmp_path / "a.txt"
    p.write_text("content-a", encoding="utf-8")

    code, res = _run(capsys, ["verify", str(p), "--expected-sha256", "too-short"])
    assert code == 2
    assert "error" in res
    assert "invalid_sha256_format" in res["error"]

    code, res2 = _run(capsys, ["verify", str(p), "--expected-sha256", "g" * 64])
    assert code == 2
    assert "error" in res2


def test_read_fresh_success(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("hello\nworld\n", encoding="utf-8")

    code, res = _run(capsys, ["read-fresh", str(p)])
    assert code == 0
    assert res["stable"] is True
    assert res["content"] == "hello\nworld\n"


def test_read_fresh_missing_file_returns_error_exit_code(tmp_path, capsys):
    """文件不存在属于程序性错误，应与 snapshot/verify/changed 保持一致的 EXIT_ERROR(2)，
    不能和“读取期间被并发修改”的业务性不稳定结论共用退出码 1。"""
    missing = tmp_path / "nope.txt"
    code, res = _run(capsys, ["read-fresh", str(missing)])
    assert code == 2
    assert "error" in res


def test_read_fresh_directory_path_does_not_crash(tmp_path, capsys):
    """传入目录而非文件路径时应得到可控的错误，而不是未捕获异常。"""
    code, res = _run(capsys, ["read-fresh", str(tmp_path)])
    assert code == 2
    assert "error" in res


def test_read_fresh_negative_offset_rejected(tmp_path, capsys):
    """黑盒评审发现的 Low 问题：负数 offset 之前会导致静默返回空内容而非明确拒绝。"""
    p = tmp_path / "a.txt"
    p.write_text("l1\nl2\nl3\n", encoding="utf-8")

    code, res = _run(capsys, ["read-fresh", str(p), "--offset", "-1"])
    # 参数写错是 caller_error（exit 2），不能与"读取期间内容不稳定"（exit 1）混淆
    assert code == 2
    assert res["reason_code"] == "invalid_offset_or_limit"
    assert res["category"] == "caller_error"
    assert res["next_action"]


def test_read_fresh_missing_path_is_caller_error(tmp_path, capsys):
    """不存在的路径不能被当成"读不稳定"：明确报 file_not_found 且不创建文件。"""
    missing = tmp_path / "nope.txt"

    code, res = _run(capsys, ["read-fresh", str(missing)])
    assert code == 2
    assert res["reason_code"] == "file_not_found"
    assert res["category"] == "caller_error"
    assert not missing.exists()


def test_read_fresh_directory_is_caller_error(tmp_path, capsys):
    """目录不是普通文件：报 not_a_regular_file，而不是读失败或不稳定。"""
    code, res = _run(capsys, ["read-fresh", str(tmp_path)])
    assert code == 2
    assert res["reason_code"] == "not_a_regular_file"
    assert res["category"] == "caller_error"


def test_read_fresh_output_same_path_refused(tmp_path, capsys):
    """--output 指向输入文件本身时，写入会截断源文件：必须拒绝且不改动内容。"""
    p = tmp_path / "a.txt"
    original = "L1\nL2\nL3\n"
    p.write_text(original, encoding="utf-8")

    code, res = _run(
        capsys, ["read-fresh", str(p), "--limit", "1", "--output", str(p)]
    )
    assert code == 2
    assert res["reason_code"] == "output_path_conflict"
    assert p.read_text(encoding="utf-8") == original


def test_read_fresh_output_hardlink_refused(tmp_path, capsys):
    """硬链接与输入文件共享 inode，resolve() 无法识别，需用 samefile 拦截。"""
    p = tmp_path / "a.txt"
    link = tmp_path / "alias.txt"
    original = "keep\n"
    p.write_text(original, encoding="utf-8")
    os.link(p, link)

    code, res = _run(capsys, ["read-fresh", str(p), "--output", str(link)])
    assert code == 2
    assert res["reason_code"] == "output_path_conflict"
    assert p.read_text(encoding="utf-8") == original


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 创建符号链接需要额外权限")
def test_read_fresh_output_symlink_to_source_refused(tmp_path, capsys):
    p = tmp_path / "a.txt"
    link = tmp_path / "alias.txt"
    original = "keep\n"
    p.write_text(original, encoding="utf-8")
    os.symlink(p, link)

    code, res = _run(capsys, ["read-fresh", str(p), "--output", str(link)])
    assert code == 2
    assert res["reason_code"] == "output_path_conflict"
    assert p.read_text(encoding="utf-8") == original
    assert link.is_symlink()


def test_read_fresh_output_writes_other_path_atomically(tmp_path, capsys):
    p = tmp_path / "a.txt"
    out = tmp_path / "nested" / "out.txt"
    p.write_text("hello\nworld\n", encoding="utf-8")

    code, res = _run(capsys, ["read-fresh", str(p), "--output", str(out)])
    assert code == 0
    assert res["content"] is None
    assert res["output"] == str(out)
    assert out.read_text(encoding="utf-8") == "hello\nworld\n"
    # 输出目录中只应留下最终文件，不留临时文件
    assert [x.name for x in out.parent.iterdir()] == ["out.txt"]


def test_read_fresh_output_drops_special_mode_bits(tmp_path, capsys):
    """输出是源文件副本：保留 0o777 普通权限位，但不继承 setuid/setgid。"""
    p = tmp_path / "a.txt"
    out = tmp_path / "out.txt"
    p.write_text("hello\n", encoding="utf-8")
    out.write_text("old\n", encoding="utf-8")
    os.chmod(out, 0o4644)
    if not os.stat(out).st_mode & 0o4000:
        pytest.skip("当前文件系统/用户不支持设置 setuid 位")

    code, _ = _run(capsys, ["read-fresh", str(p), "--output", str(out)])
    assert code == 0
    mode = os.stat(out).st_mode
    assert mode & 0o777 == 0o644
    assert not mode & 0o7000


def test_events_list_filters_by_since_and_limit(tmp_path, capsys):
    """--since/--limit 先过滤再截断：调用方不必把整个事件流拉进上下文。"""
    events_file = Path(os.environ["AGENT_FS_HOME"]) / "events.jsonl"
    events_file.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"fingerprint": "a", "last_seen": "2026-10-01T00:00:00+00:00", "category": "agent_"},
        {"fingerprint": "b", "last_seen": "2026-10-02T00:00:00+00:00", "category": "agent_"},
        {"fingerprint": "c", "last_seen": "2026-10-03T00:00:00+00:00", "category": "agent_"},
    ]
    events_file.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    code, res = _run(
        capsys, ["events", "list", "--since", "2026-10-02T00:00:00Z", "--limit", "1"]
    )
    assert code == 0
    assert res["total"] == 2  # 过滤后 2 条
    assert res["count"] == 1  # 截断后 1 条
    assert res["events"][0]["fingerprint"] == "c"


def test_events_list_rejects_invalid_filters(capsys):
    code, res = _run(capsys, ["events", "list", "--since", "not-a-time"])
    assert code == 2
    assert res["reason_code"] == "invalid_arguments"

    code, res = _run(capsys, ["events", "list", "--limit", "-1"])
    assert code == 2
    assert res["reason_code"] == "invalid_arguments"


def test_snapshot_prune_requires_criterion(tmp_path, capsys):
    """未给条件时默认不删：直接报参数错误，避免误清空全部历史快照。"""
    code, res = _run(capsys, ["snapshot", "prune"])
    assert code == 2
    assert res["reason_code"] == "invalid_arguments"


def test_snapshot_prune_keep_last_per_target(tmp_path, capsys):
    """--keep-last 按目标文件保留：先 dry-run 预览，再实际删除最旧的一份。"""
    import time

    from agent_fs.store import save_snapshot

    target = tmp_path / "doc.md"
    target.write_text("v1", encoding="utf-8")
    now = time.time()
    paths = []
    for index, age in enumerate((300, 200, 100)):
        saved = save_snapshot(
            f"doc-{index}", {"path": str(target), "sha256": "a" * 64, "size": index}
        )
        os.utime(saved, (now - age, now - age))
        paths.append(saved)

    code, preview = _run(
        capsys, ["snapshot", "prune", "--keep-last", "2", "--dry-run"]
    )
    assert code == 0
    assert preview["dry_run"] is True
    assert preview["matched"] == 1
    assert preview["removed"] == ["doc-0"]
    assert all(path.exists() for path in paths)  # dry-run 不删文件

    code, result = _run(capsys, ["snapshot", "prune", "--keep-last", "2"])
    assert code == 0
    assert result["removed"] == ["doc-0"]
    assert not paths[0].exists()
    assert paths[1].exists() and paths[2].exists()


def test_snapshot_prune_older_than_keeps_recent(tmp_path, capsys):
    import time

    from agent_fs.store import save_snapshot

    target = tmp_path / "doc.md"
    target.write_text("v1", encoding="utf-8")
    now = time.time()
    old = save_snapshot("old", {"path": str(target), "sha256": "a" * 64, "size": 1})
    new = save_snapshot("new", {"path": str(target), "sha256": "b" * 64, "size": 1})
    os.utime(old, (now - 3 * 86400, now - 3 * 86400))
    os.utime(new, (now - 60, now - 60))

    code, result = _run(capsys, ["snapshot", "prune", "--older-than", "1"])
    assert code == 0
    assert result["removed"] == ["old"]
    assert not old.exists()
    assert new.exists()


def test_snapshot_missing_file_returns_error_exit_code(tmp_path, capsys):
    """snapshot 现已改用 compute_stable_fingerprint，输出格式为
    {"stable": False, "reason": ..., ...}，而非旧版的 {"error": ...}。"""
    missing = tmp_path / "nope.txt"
    code, res = _run(capsys, ["snapshot", str(missing)])
    assert code == 2
    assert res["stable"] is False
    assert res["reason"] == "file_not_found"


def test_changed_missing_snapshot_returns_error_exit_code(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    code, res = _run(capsys, ["changed", str(p), "--since", "no-such-snapshot"])
    assert code == 2
    assert res["reason_code"] == "snapshot_not_found"
    assert res["category"] == "caller_error"


def test_changed_rejects_snapshot_of_another_file(tmp_path, capsys):
    """跨目标快照不能用于比较：否则会把 A 的哈希当成 B 的历史哈希。"""
    a = tmp_path / "a.txt"
    a.write_text("a", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("b", encoding="utf-8")
    _, snap = _run(capsys, ["snapshot", str(a)])

    code, res = _run(capsys, ["changed", str(b), "--since", snap["snapshot_id"]])
    assert code == 2
    assert res["reason_code"] == "snapshot_path_mismatch"


def test_changed_rejects_corrupted_snapshot(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    snap_dir = Path(os.environ["AGENT_FS_HOME"]) / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "broken-snap.json").write_text("{not json", encoding="utf-8")

    code, res = _run(capsys, ["changed", str(p), "--since", "broken-snap"])
    assert code == 2
    assert res["reason_code"] == "snapshot_corrupted"


def test_changed_rejects_traversal_snapshot_id(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    code, res = _run(capsys, ["changed", str(p), "--since", "../escape"])
    assert code == 2
    assert res["reason_code"] == "snapshot_not_found"


def test_verify_rejects_corrupted_snapshot(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    snap_dir = Path(os.environ["AGENT_FS_HOME"]) / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "broken-verify.json").write_text("{not json", encoding="utf-8")

    code, res = _run(capsys, ["verify", str(p), "--snapshot", "broken-verify"])
    assert code == 2
    assert res["reason_code"] == "snapshot_corrupted"


def test_full_write_protocol_scenario(tmp_path, capsys):
    """模拟完整写前写后协议：读取记哈希 -> 写入前 verify 通过 -> 写入 -> 再次 snapshot。"""
    p = tmp_path / "shared.txt"
    p.write_text("original", encoding="utf-8")

    # 写前：记录基线哈希
    code, before = _run(capsys, ["snapshot", str(p)])
    base_hash = before["sha256"]

    # 写入前二次校验：应通过（无人修改过）
    code, verify_res = _run(capsys, ["verify", str(p), "--expected-sha256", base_hash])
    assert code == 0
    assert verify_res["match"] is True

    # 模拟另一个 Agent 抢先修改了文件
    p.write_text("modified-by-someone-else", encoding="utf-8")

    # 本 Agent 真正写入前再校验一次：应发现不匹配，必须停止
    code, verify_res2 = _run(capsys, ["verify", str(p), "--expected-sha256", base_hash])
    assert code == 1
    assert verify_res2["match"] is False


def test_write_if_match_via_content_flag(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("old", encoding="utf-8")

    code, snap = _run(capsys, ["snapshot", str(p)])
    sha = snap["sha256"]

    code, res = _run(
        capsys, ["write-if-match", str(p), "--expected-sha256", sha, "--content", "new-value"]
    )
    assert code == 0
    assert res["written"] is True
    assert p.read_text(encoding="utf-8") == "new-value"


def test_write_if_match_via_content_file_flag(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("old", encoding="utf-8")
    src = tmp_path / "src.txt"
    src.write_text("from-file", encoding="utf-8")

    code, snap = _run(capsys, ["snapshot", str(p)])
    sha = snap["sha256"]

    code, res = _run(
        capsys,
        ["write-if-match", str(p), "--expected-sha256", sha, "--content-file", str(src)],
    )
    assert code == 0
    assert res["written"] is True
    assert p.read_text(encoding="utf-8") == "from-file"


def test_write_if_match_content_file_missing_returns_error(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("old", encoding="utf-8")
    code, snap = _run(capsys, ["snapshot", str(p)])
    sha = snap["sha256"]

    code, res = _run(
        capsys,
        [
            "write-if-match",
            str(p),
            "--expected-sha256",
            sha,
            "--content-file",
            str(tmp_path / "no-such-source.txt"),
        ],
    )
    assert code == 2
    assert "error" in res
    assert p.read_text(encoding="utf-8") == "old"


def test_write_if_match_rejects_malformed_hash(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("old", encoding="utf-8")

    code, res = _run(
        capsys,
        ["write-if-match", str(p), "--expected-sha256", "bad-hash", "--content", "x"],
    )
    assert code == 2
    assert "error" in res
    assert p.read_text(encoding="utf-8") == "old"


def test_write_if_match_conflict_returns_negative_exit_code(tmp_path, capsys):
    p = tmp_path / "a.txt"
    p.write_text("original", encoding="utf-8")

    code, res = _run(
        capsys,
        ["write-if-match", str(p), "--expected-sha256", "0" * 64, "--content", "attempted"],
    )
    assert code == 1
    assert res["written"] is False
    assert p.read_text(encoding="utf-8") == "original"


def _init_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return repo


def test_guard_git_object_rejects_missing_commit(tmp_path, capsys):
    repo = _init_git_repo(tmp_path)

    code, res = _run(capsys, ["guard", "git-object", "deadbeef", "--cwd", str(repo)])

    assert code == 1
    assert res["ok"] is False
    assert res["reason"] == "git_object_missing"


def test_guard_pathspec_rejects_path_before_separator(tmp_path, capsys):
    repo = _init_git_repo(tmp_path)

    code, res = _run(capsys, ["guard", "pathspec", "git", "log", "tracked.txt", "--cwd", str(repo)])

    assert code == 1
    assert res["ok"] is False
    assert res["reason"] == "pathspec_separator_missing"


def test_guard_pathspec_rejects_non_git_command(tmp_path, capsys):
    code, res = _run(capsys, ["guard", "pathspec", "svn", "log", "--", "tracked.txt"])

    assert code == 1
    assert res["ok"] is False
    assert res["reason"] == "git_command_required"


def test_guard_pathspec_returns_parsed_arguments(tmp_path, capsys):
    code, res = _run(capsys, ["guard", "pathspec", "git", "log", "HEAD", "--", "tracked.txt"])

    assert code == 0
    assert res["ok"] is True
    assert res["revisions"] == ["HEAD"]
    assert res["pathspecs"] == ["tracked.txt"]


def test_guard_multiedit_rejects_unstable_target(monkeypatch, tmp_path, capsys):
    target = tmp_path / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps([{"old": "old", "new": "new"}]), encoding="utf-8")

    monkeypatch.setattr(
        "agent_fs.guard.read_fresh",
        lambda path: ReadFreshResult(
            stable=False,
            path=str(path),
            content=None,
            sha256=None,
            size=None,
            mtime_ns=None,
            attempts=3,
            encoding="utf-8",
            truncated=False,
            reason="changed_during_read",
        ),
    )

    code, res = _run(capsys, ["guard", "multiedit", str(target), "--edits-file", str(edits)])

    assert code == 1
    assert res["ok"] is False
    assert res["reason"] == "target_unstable"


def test_guard_multiedit_returns_target_hash(tmp_path, capsys):
    target = tmp_path / "target.txt"
    target.write_text("old\n", encoding="utf-8")
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps([{"old": "old", "new": "new"}]), encoding="utf-8")

    code, res = _run(capsys, ["guard", "multiedit", str(target), "--edits-file", str(edits)])

    assert code == 0
    assert res["ok"] is True
    assert len(res["sha256"]) == 64
    assert res["next_step"] == "verify target hash before applying external edits"


def test_guard_worktree_rejects_unexpected_branch(tmp_path, capsys):
    repo = _init_git_repo(tmp_path)
    actual_root = str(repo.resolve())

    code, res = _run(
        capsys,
        [
            "guard",
            "worktree",
            "--expected-root",
            actual_root,
            "--expected-branch",
            "wrong-branch",
            "--cwd",
            actual_root,
        ],
    )

    assert code == 1
    assert res["ok"] is False
    assert res["reason"] == "worktree_identity_mismatch"
    assert res["actual"]["root"] == actual_root


def test_guard_multiedit_rejects_duplicate_old_text(tmp_path, capsys):
    target = tmp_path / "target.txt"
    target.write_text("same\nsame\n", encoding="utf-8")
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps([{"old": "same", "new": "replacement"}]), encoding="utf-8")

    code, res = _run(
        capsys,
        ["guard", "multiedit", str(target), "--edits-file", str(edits)],
    )

    assert code == 1
    assert res["ok"] is False
    # 诊断与 edit-if-match 同源（P1-7）：同一份材料给出同一个 reason_code
    assert res["reason"] == "anchor_not_unique"
    assert res["occurrences"] == 2
    assert res["positions"] == [0, 5]
    assert res["edit_index"] == 0


def test_guard_text_write_rejects_replacement_character(tmp_path, capsys):
    content = tmp_path / "new-content.txt"
    content.write_text("bad\ufffdcontent", encoding="utf-8")

    code, res = _run(capsys, ["guard", "text-write", "--content-file", str(content)])

    assert code == 1
    assert res["ok"] is False
    assert res["reason"] == "replacement_character_detected"


def test_guard_text_write_reports_missing_content_file_as_error(tmp_path, capsys):
    code, res = _run(
        capsys,
        ["guard", "text-write", "--content-file", str(tmp_path / "missing-content.txt")],
    )

    assert code == 2
    assert res["ok"] is False
    assert res["reason"] == "content_file_unreadable"


def test_guard_git_commit_and_worktree_pass_on_clean_repository(tmp_path, capsys):
    repo = _init_git_repo(tmp_path)
    root = str(repo.resolve())
    branch = subprocess.run(
        ["git", "-C", root, "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    code, commit_res = _run(capsys, ["guard", "git-commit", "--cwd", root])
    assert code == 0
    assert commit_res["ok"] is True

    code, worktree_res = _run(
        capsys,
        [
            "guard",
            "worktree",
            "--expected-root",
            root,
            "--expected-branch",
            branch,
            "--cwd",
            root,
        ],
    )
    assert code == 0
    assert worktree_res["ok"] is True


class TestA20CapabilityProbe:
    def test_unknown_command_returns_json_not_usage(self, tmp_path, capsys):
        """A20：未知命令/旧 CLI 缺少子命令时，返回结构化 JSON 而非 argparse usage 文本。"""
        code = main(["no-such-command"])
        out = json.loads(capsys.readouterr().out)
        assert code == 2
        assert out["category"] == "caller_error"
        assert out["reason_code"] == "invalid_arguments"
        assert "usage" not in out.get("error", "")
        assert "usage" not in json.dumps(out)

    def test_reconcile_missing_args_is_structured_json(self, tmp_path, capsys):
        """A20：reconcile 缺参数 → JSON caller_error（调用方可探测能力后修正）。"""
        code = main(["reconcile", str(tmp_path / "f.md")])
        out = json.loads(capsys.readouterr().out)
        assert code == 2
        assert out["category"] == "caller_error"
        assert out["reason_code"] == "invalid_arguments"
        # 审查 P3：command 取子命令名，帮助命令不再出现 "agent-fs agent-fs" 重复前缀
        assert out["command"] == "reconcile"
        assert out["suggested_command"] == "agent-fs reconcile --help"
        assert "agent-fs agent-fs" not in out["suggested_command"]


class TestUnhandledStageSplit:
    """main() 兜底按执行阶段分流 commit_state（SPEC §6.2）。"""

    def test_pre_commit_unhandled_returns_error_exit_2(self, tmp_path, capsys):
        from agent_fs.fingerprint import compute_fingerprint

        p = tmp_path / "a.txt"
        p.write_text("original", encoding="utf-8")
        base = compute_fingerprint(p).sha256

        real_open = os.open

        def fail_on_temp(path, *args, **kwargs):
            if "agentfs-tmp" in str(path):
                raise RuntimeError("boom")
            return real_open(path, *args, **kwargs)

        with patch("os.open", side_effect=fail_on_temp):
            code = main(
                ["write-if-match", str(p), "--expected-sha256", base, "--content", "x"]
            )
        out = json.loads(capsys.readouterr().out)
        assert code == 2
        assert out["status"] == "error"
        assert out["commit_state"] == "not_committed"
        assert out["reason_code"] == "unhandled_pre_commit"
        assert out["retryable"] is False
        # 不再统一提示"重试一次"
        assert "重试一次" not in out["next_action"]
        assert p.read_text(encoding="utf-8") == "original"

    def test_post_commit_unhandled_returns_unknown_exit_3(self, tmp_path, capsys):
        from agent_fs.fingerprint import compute_fingerprint

        p = tmp_path / "a.txt"
        p.write_text("original", encoding="utf-8")
        base = compute_fingerprint(p).sha256

        # 替换成功（stage 已置 post_commit）后回读抛非 OSError 异常 → 逃逸到 main 兜底
        # 调用序：基线(1) → 替换前复核(2) → 回读(3)；只让回读抛 RuntimeError
        from agent_fs.fingerprint import compute_stable_fingerprint as real_stable

        calls = {"n": 0}

        def flaky_stable(path, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return real_stable(path, **kwargs)
            raise RuntimeError("unexpected readback crash")

        with patch(
            "agent_fs.write_atomic.compute_stable_fingerprint", side_effect=flaky_stable
        ):
            code = main(
                ["write-if-match", str(p), "--expected-sha256", base, "--content", "x"]
            )
        out = json.loads(capsys.readouterr().out)
        assert code == 3
        assert out["status"] == "unknown"
        assert out["commit_state"] == "committed"
        assert out["reason_code"] == "unhandled_post_commit"
        assert "不自动重试" in out["next_action"]
        # 替换确实发生了
        assert p.read_text(encoding="utf-8") == "x"

        # 事件记录包含 file_state 与 recovery_action（与根因分开）
        from agent_fs.events import load_events

        events = load_events()
        unhandled = [e for e in events if e.get("reason_code") == "unhandled_post_commit"]
        assert unhandled
        assert unhandled[-1]["file_state"] == "unknown/committed"
        assert unhandled[-1]["recovery_action"] == "reconcile"

    def test_post_commit_unhandled_keeps_unified_fields(self, tmp_path, capsys):
        """审查 P2-4：提交后未预料异常的结果必须是统一结构并保留调用上下文。

        旧实现手拼 dict：command 误报 "unknown"，且丢失 path/三个哈希/reason/
        durability，调用方无法凭响应进入 reconcile（不知道 Hbase/Htarget）。
        """
        from agent_fs.fingerprint import compute_fingerprint, hash_bytes

        p = tmp_path / "a.txt"
        p.write_text("original", encoding="utf-8")
        base = compute_fingerprint(p).sha256
        target = hash_bytes(b"x")

        from agent_fs.fingerprint import compute_stable_fingerprint as real_stable

        calls = {"n": 0}

        def flaky_stable(path, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                return real_stable(path, **kwargs)
            raise RuntimeError("unexpected readback crash")

        with patch(
            "agent_fs.write_atomic.compute_stable_fingerprint", side_effect=flaky_stable
        ):
            code = main(
                [
                    "write-if-match", str(p),
                    "--expected-sha256", base,
                    "--expected-result-sha256", target,
                    "--content", "x",
                ]
            )
        out = json.loads(capsys.readouterr().out)
        assert code == 3
        # 统一结构：调用上下文完整保留
        assert out["command"] == "write-if-match"
        assert out["path"] == str(p)
        assert out["expected_sha256"] == base
        assert out["desired_sha256"] == target
        assert out["observed_sha256"] is None
        assert out["previous_sha256"] == base
        assert out["reason"]  # reason 非空（reason_code 的人类可读文本）
        assert out["durability"] == "unconfirmed"
        assert out["commit_state"] == "committed"
        assert "unhandled_post_commit" in out["error"]
        assert out["written"] is True

    def test_pre_commit_unhandled_keeps_unified_fields(self, tmp_path, capsys):
        """审查 P2-4：提交前未预料异常同样走统一结构（error/not_committed/exit 2）。"""
        from agent_fs.fingerprint import compute_fingerprint, hash_bytes

        p = tmp_path / "a.txt"
        p.write_text("original", encoding="utf-8")
        base = compute_fingerprint(p).sha256
        target = hash_bytes(b"x")

        real_open = os.open

        def fail_on_temp(path, *args, **kwargs):
            if "agentfs-tmp" in str(path):
                raise RuntimeError("boom")
            return real_open(path, *args, **kwargs)

        with patch("os.open", side_effect=fail_on_temp):
            code = main(
                [
                    "write-if-match", str(p),
                    "--expected-sha256", base,
                    "--expected-result-sha256", target,
                    "--content", "x",
                ]
            )
        out = json.loads(capsys.readouterr().out)
        assert code == 2
        assert out["command"] == "write-if-match"
        assert out["path"] == str(p)
        assert out["expected_sha256"] == base
        assert out["desired_sha256"] == target
        assert out["observed_sha256"] is None
        assert out["reason"]
        assert out["durability"] == "not_applicable"
        assert out["commit_state"] == "not_committed"
        assert p.read_text(encoding="utf-8") == "original"


class TestEmptyHtargetRejected:
    """审查 P2-7：显式传入空 Htarget 是参数错误，不是"未声明"。"""

    def test_write_empty_htarget_is_argument_error(self, tmp_path, capsys):
        """--expected-result-sha256 '' → invalid_sha256_format / exit 2，原文保留。"""
        from agent_fs.fingerprint import compute_fingerprint

        p = tmp_path / "a.txt"
        p.write_text("original", encoding="utf-8")
        base = compute_fingerprint(p).sha256

        code = main(
            [
                "write-if-match", str(p),
                "--expected-sha256", base,
                "--expected-result-sha256", "",
                "--content", "x",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 2
        assert out["reason_code"] == "invalid_sha256_format"
        assert "空字符串" in out["error"]
        assert out["written"] is False
        assert p.read_text(encoding="utf-8") == "original"  # 原文未被写入

    def test_edit_empty_htarget_is_argument_error(self, tmp_path, capsys):
        """edit-if-match --expected-result-sha256 '' → 同口径拒绝，原文保留。"""
        import json as _json

        from agent_fs.fingerprint import compute_fingerprint

        p = tmp_path / "a.txt"
        p.write_text("original", encoding="utf-8")
        base = compute_fingerprint(p).sha256
        edits = tmp_path / "edits.json"
        edits.write_text(_json.dumps([{"old": "orig", "new": "X"}]), encoding="utf-8")

        code = main(
            [
                "edit-if-match", str(p),
                "--expected-sha256", base,
                "--edits-file", str(edits),
                "--expected-result-sha256", "",
            ]
        )
        out = json.loads(capsys.readouterr().out)
        assert code == 2
        assert out["reason_code"] == "invalid_sha256_format"
        assert "空字符串" in out["error"]
        assert out["written"] is False
        assert p.read_text(encoding="utf-8") == "original"
