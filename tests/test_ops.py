"""操作台账（ops）契约测试：record_operation / list_operations / CLI 集成。

对应 2026-10-31 三轮评审修订后的契约：
- 台账尽力记录：任何台账失败（短写、序列化、路径解析、错误字符串化）
  不抛出、不掩盖主结果；无换行残尾在下次追加前被截断丢弃，不吞后续记录；
- pending = 归一化目标身份的最新一条受控写入 status=unknown（无身份行
  不参与）；默认视图为全量历史（最新在前，按文件顺序）；
- 读取端逐行隔离坏行（非 UTF-8、坏 JSON、非法身份字段），无换行末段
  整体跳过；
- --target 查询覆盖双向别名、目录符号链接、相对路径与 `..` 组合；
  --limit 负数被 CLI 拒绝。

注意：测试内不要用 monkeypatch.undo()——那会连同 conftest 的
AGENT_FS_HOME 隔离一起撤销，把测试写入引到真实用户台账。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys

import pytest

from agent_fs import cli, ops
from agent_fs.ops import (
    list_operations,
    load_operations,
    record_operation,
)
from agent_fs.result import WriteOutcome


def _outcome(status="verified", commit_state="committed", path="/tmp/x.md",
             command="write-if-match", reason_code="ok", **extra):
    return WriteOutcome(
        command=command, path=path, status=status, commit_state=commit_state,
        reason_code=reason_code, **extra,
    )


def _unknown(path):
    return _outcome(status="unknown", commit_state="committed", path=str(path),
                    reason_code="unhandled_post_commit")


def _ops_file(tmp_path):
    from agent_fs.ops import _ops_path
    ops_file = _ops_path()
    ops_file.parent.mkdir(parents=True, exist_ok=True)
    return ops_file


def test_record_verified_write_and_permissions(tmp_path):
    target = tmp_path / "a.md"
    target.write_text("hello")
    result = record_operation(_outcome(path=str(target)))
    assert result["recorded"] is True
    ops_file = _ops_file(tmp_path)
    assert stat.S_IMODE(ops_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(ops_file.parent.stat().st_mode) == 0o700
    row = load_operations()[0]
    assert row["command"] == "write-if-match"
    assert row["status"] == "verified"
    assert row["path"] == str(target)
    assert row["path_real"] == os.path.realpath(target)


def test_record_skips_prepared_and_non_write(tmp_path):
    assert record_operation(
        _outcome(status="prepared", commit_state="not_committed")
    )["recorded"] is False
    assert record_operation(_outcome(command="read-fresh"))["recorded"] is False
    assert load_operations() == []


def test_record_never_raises_and_isolates_failures(tmp_path, monkeypatch):
    target = tmp_path / "s.md"
    # 1) 目标路径解析失败 → 记录失败但不抛出
    def boom(p):
        raise RuntimeError("boom")

    monkeypatch.setattr(ops, "_target_identity", boom)
    assert record_operation(_outcome(path=str(target)))["recorded"] is False
    # 2) surrogate 路径（非 UTF-8 字节）：ensure_ascii 转义后必然成功记录
    monkeypatch.setattr(ops, "_target_identity", lambda p: os.path.realpath(p))
    weird = str(tmp_path / "bad\udce4.md")
    assert record_operation(_outcome(path=weird))["recorded"] is True
    # 3) ops_path 抛出 → 记录失败不抛出
    monkeypatch.setattr(ops, "_ops_path", boom)
    assert record_operation(_outcome(path=str(target)))["recorded"] is False
    # 4) 错误对象 __str__ 自身抛错 → 异常处理器仍不抛出
    class EvilStr(Exception):
        def __str__(self):
            raise RuntimeError("evil str")

    monkeypatch.setattr(ops, "_ops_path",
                        lambda: (_ for _ in ()).throw(EvilStr()))
    result = record_operation(_outcome(path=str(target)))
    assert result["recorded"] is False
    assert result["reason"].startswith("record_failed:")


def test_short_write_reported_and_next_record_survives(tmp_path):
    """短写：本条记为失败，残片不吞下一条完整记录。

    os.write 手工保存/恢复，不用 monkeypatch.undo()（见模块 docstring）。
    """
    _ops_file(tmp_path)
    real_write = os.write

    def partial_write(fd, data):
        return real_write(fd, data[:17])  # 只写前 17 字节（短写）

    os.write = partial_write
    try:
        result = record_operation(_outcome(path=str(tmp_path / "x.md")))
    finally:
        os.write = real_write
    assert result["recorded"] is False
    assert "short_write" in result["reason"]
    assert load_operations() == []  # 残片无换行 = 末段，跳过

    # 下一条完整记录：锁内先截断残尾再追加，可正常读取
    t2 = tmp_path / "y.md"
    record_operation(_unknown(t2))
    rows = load_operations()
    assert len(rows) == 1 and rows[0]["path"] == str(t2)
    assert list_operations(pending=True)["pending_count"] == 1


def test_pending_is_latest_per_target_and_default_is_history(tmp_path):
    t = tmp_path / "d.md"
    record_operation(_unknown(t))
    record_operation(_outcome(path=str(t)))  # 同目标 verified 消解 unknown
    assert list_operations(pending=True)["pending_count"] == 0
    t2 = tmp_path / "e.md"
    record_operation(_unknown(t2))
    summary = list_operations(pending=True)
    assert summary["pending_count"] == 1
    assert summary["operations"][0]["path"] == str(t2)
    # 默认视图 = 全量历史（3 条，最新在前）
    history = list_operations()
    assert history["total"] == 3
    assert [r["path"] for r in history["operations"]] == [str(t2), str(t), str(t)]
    # 重复目标排序：A 旧、B、A 新 → limit=1 应返回 A 的最新一条
    summary = list_operations(pending=True, limit=1)
    assert summary["operations"][0]["path"] == str(t2)


def test_ordering_dedup_keeps_newest_under_limit(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    record_operation(_unknown(a))   # A 旧
    record_operation(_unknown(b))   # B
    record_operation(_unknown(a))   # A 新（字典覆盖不改插入序的回归点）
    summary = list_operations(pending=True, limit=1)
    assert summary["shown"] == 1
    assert summary["operations"][0]["path"] == str(a)  # 必须是 A 的最新一条


def test_target_filter_bidirectional_alias_relative_and_dir_alias(tmp_path, monkeypatch):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    real = real_dir / "real.md"
    real.write_text("x")
    link_dir = tmp_path / "link_dir"
    link_dir.symlink_to(real_dir)  # 目录符号链接（macOS /var 场景等价物）
    alias = link_dir / "alias.md"
    alias.symlink_to(real)

    record_operation(_outcome(path=str(real)))  # 按物理路径写入

    assert list_operations(target=str(alias))["total"] == 1          # 反向别名
    assert list_operations(target=str(link_dir / "real.md"))["total"] == 1
    monkeypatch.chdir(real_dir)                                       # 相对路径
    assert list_operations(target="real.md")["total"] == 1
    assert list_operations(target="./real.md")["total"] == 1
    assert list_operations(target=str(tmp_path / "other.md"))["total"] == 0


def test_target_query_survives_symlink_dir_with_dotdot(tmp_path):
    """回归 R1：符号链接目录 + `..` 的两种解析顺序都必须命中。

    sym -> elsewhere/deep；invoked = sym/../f.md 经 realpath 解析到
    elsewhere/f.md；abspath 则折叠为 tmp/f.md（另一个文件，不同目标）。
    查询按原调用串与实际位置都应命中同一条记录，且互不混入。
    """
    deep = tmp_path / "elsewhere" / "deep"
    deep.mkdir(parents=True)
    target = tmp_path / "elsewhere" / "f.md"
    target.write_text("x")
    link = tmp_path / "sym"
    link.symlink_to(deep)
    invoked = str(link / ".." / "f.md")
    unrelated = tmp_path / "f.md"   # abspath 折叠出的另一个文件（不同目标）
    unrelated.write_text("y")

    record_operation(_unknown(invoked))
    record_operation(_outcome(path=str(unrelated)))

    # 按调用串查询：只命中调用串记录，不把 abspath 折叠出的无关文件混入
    s = list_operations(target=invoked)
    assert s["total"] == 1 and s["operations"][0]["path"] == invoked
    # 按实际位置（realpath 归一）查询：命中同一条调用串记录
    s = list_operations(target=str(target))
    assert s["total"] == 1 and s["operations"][0]["path"] == invoked
    # 无关文件自己的查询不受影响
    s = list_operations(target=str(unrelated))
    assert s["total"] == 1 and s["operations"][0]["path"] == str(unrelated)
    assert s["total"] == 1 and s["operations"][0]["path"] == str(unrelated)


def test_relative_dotdot_query_no_false_match(tmp_path):
    """回归 R1：相对软链 + `..` 查询不得命中 abspath 折叠出的另一文件。

    sym -> elsewhere/deep；sym/../f.md 经 realpath 指向 elsewhere/f.md，
    而 abspath 折叠为 cwd/f.md（另一个文件）。两者不得互为别名。
    """
    deep = tmp_path / "elsewhere" / "deep"
    deep.mkdir(parents=True)
    real = deep / "f.md"
    real.write_text("x")
    link = tmp_path / "sym"
    link.symlink_to(deep)
    unrelated = tmp_path / "f.md"
    unrelated.write_text("y")

    record_operation(_unknown(str(link / ".." / "f.md")))
    record_operation(_outcome(path=str(unrelated)))

    s = list_operations(target=str(link / ".." / "f.md"))
    assert s["total"] == 1
    assert s["operations"][0]["path"] == str(link / ".." / "f.md")
    s = list_operations(target=str(unrelated))
    assert s["total"] == 1 and s["operations"][0]["path"] == str(unrelated)


def test_lock_failure_is_not_reported_as_busy(tmp_path, monkeypatch):
    """回归 R2 nit：非竞争类锁错误 → lock_failed（不误报 lock_busy 竞争）。"""
    import errno

    _ops_file(tmp_path)

    def broken_flock(fd, mode):
        raise OSError(errno.EBADF, "not a fd")

    monkeypatch.setattr(ops.fcntl, "flock", broken_flock)
    result = record_operation(_outcome(path=str(tmp_path / "x.md")))
    assert result["recorded"] is False
    assert "lock_failed" in result["reason"]


def test_case_alias_groups_share_pending_state(tmp_path):
    """darwin casefold：大小写别名视为同一目标（unknown 被 verified 消解）。"""
    if sys.platform != "darwin":
        pytest.skip("casefold 归一仅 darwin 分支")
    upper = tmp_path / "CaseTest.md"
    upper.write_text("x")
    record_operation(_unknown(upper))
    lower = tmp_path / "casetest.md"  # 同一文件的大小写别名
    record_operation(_outcome(path=str(lower)))
    assert list_operations(pending=True)["pending_count"] == 0


def test_load_isolates_corrupt_and_invalid_identity_rows(tmp_path):
    good = {"schema_version": 1, "command": "write-if-match", "path": "/t/x",
            "path_real": "/t/x", "status": "unknown", "commit_state": "committed"}
    bad_path_type = {"command": "write-if-match", "path": ["bad"],
                     "path_real": {"k": 1}, "status": "unknown"}   # 身份字段非法 → 整行跳过
    bad_real = {"command": "write-if-match", "path": "/t/y",
                "path_real": {"k": 1}, "status": "verified"}        # 仅 real 非法 → 降级 None
    ops_file = _ops_file(tmp_path)
    payload = (json.dumps(good) + "\n"
               + json.dumps(bad_path_type) + "\n"
               + json.dumps(bad_real) + "\n"
               + "{not json\n")
    ops_file.write_text(payload)
    with open(ops_file, "ab") as f:
        f.write(b"\xff\xfe binary line\n")  # 非 UTF-8 行
    rows = load_operations()
    assert len(rows) == 2  # good + bad_real（path_real 降级）
    assert rows[1]["path"] == "/t/y" and rows[1]["path_real"] is None
    # 无 TypeError；good 行可被 pending 检索
    summary = list_operations(pending=True)
    assert summary["pending_count"] == 1
    assert summary["operations"][0]["path"] == "/t/x"


def test_rows_without_identity_do_not_merge_or_block(tmp_path):
    """无身份行不参与 pending、不互相合并；默认历史仍可见。"""
    ops_file = _ops_file(tmp_path)
    rows = [
        {"schema_version": 1, "command": "write-if-match", "status": "unknown",
         "commit_state": "committed"},
        {"schema_version": 1, "command": "write-if-match", "status": "verified",
         "commit_state": "committed"},
    ]
    ops_file.write_text("".join(json.dumps(r) + "\n" for r in rows))
    summary = list_operations(pending=True)
    assert summary["pending_count"] == 0  # 无身份不参与 pending
    assert list_operations()["total"] == 2  # 默认历史仍完整


def test_trailing_fragment_without_newline_is_skipped(tmp_path):
    ops_file = _ops_file(tmp_path)
    good = {"schema_version": 1, "command": "write-if-match", "path": "/t/g",
            "path_real": "/t/g", "status": "verified", "commit_state": "committed"}
    with open(ops_file, "wb") as f:
        f.write((json.dumps(good) + "\n").encode("utf-8"))
        f.write(b"torn fragment no newline")            # 残迹（坏 JSON）
    assert len(load_operations()) == 1
    # 完整 JSON 但缺换行的尾段：契约同样跳过（可能来自恰好差一byte的短写）
    with open(ops_file, "ab") as f:
        f.write(json.dumps({"command": "write-if-match", "path": "/t/h",
                            "status": "verified"}).encode("utf-8"))  # 无换行
    assert len(load_operations()) == 1


def test_limit_zero_returns_empty_and_negative_rejected_by_cli(tmp_path, capsys):
    for i in range(3):
        record_operation(_outcome(path=str(tmp_path / f"f{i}.md")))
    summary = list_operations(limit=0)
    assert summary["shown"] == 0 and summary["total"] == 3
    summary = list_operations()
    assert summary["shown"] == 3  # 默认 50
    code = cli.main(["ops", "list", "--limit", "-1"])
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error"


def test_failed_fragment_is_truncated_not_resurrected(tmp_path):
    """回归 R1/R2：A verified 短写只缺末尾换行（recorded=false）后，
    B 的追加必须截断丢弃该残片——残片不得复活，A 的 pending 不被错误消解。"""
    a = tmp_path / "a.md"
    record_operation(_unknown(a))  # A unknown → pending 1
    ops_file = _ops_file(tmp_path)
    # 手工写入 A 的 verified 完整 JSON，但**没有末尾换行**（模拟恰好短写 1 字节）
    fragment = json.dumps({"schema_version": 1, "command": "write-if-match",
                           "path": str(a), "path_real": str(a),
                           "status": "verified", "commit_state": "committed",
                           "reason_code": "ok"})
    with open(ops_file, "ab") as f:
        f.write(fragment.encode("utf-8"))  # 无换行
    assert list_operations(pending=True)["pending_count"] == 1  # 残迹不生效

    # B 成功追加：追加前应截断残尾
    b = tmp_path / "b.md"
    record_operation(_outcome(path=str(b)))
    rows = load_operations()
    paths = [r["path"] for r in rows]
    assert str(b) in paths and fragment[:40] not in "".join(paths)
    summary = list_operations(pending=True)
    assert summary["pending_count"] == 1  # A 仍待核实，未被复活行消解
    assert summary["operations"][0]["path"] == str(a)


def test_huge_integer_row_is_isolated(tmp_path):
    """回归：5000 位整数的行让 json.loads 抛 ValueError，不得击穿查询。"""
    ops_file = _ops_file(tmp_path)
    huge = "9" * 5000  # 手工构造字节（攻击者直接写文件，绕过 int 序列化上限）
    bomb = ('{"command": "write-if-match", "path": "/t/bomb", '
            '"status": "unknown", "commit_state": "committed", '
            '"payload": ' + huge + '}\n')
    ok = ('{"command": "write-if-match", "path": "/t/ok", "path_real": "/t/ok", '
          '"status": "unknown", "commit_state": "committed"}\n')
    with open(ops_file, "wb") as f:
        f.write(bomb.encode("utf-8"))
        f.write(ok.encode("utf-8"))
    summary = list_operations(pending=True)
    assert summary["pending_count"] == 1
    assert summary["operations"][0]["path"] == "/t/ok"


def test_symlink_repoint_keeps_history_queryable(tmp_path):
    """回归：软链改指后，按原调用路径仍可检索历史记录；
    pending 分组按记录时真实路径（改指视为不同目标）。"""
    a = tmp_path / "ta.md"
    a.write_text("a")
    alias = tmp_path / "alias.md"
    alias.symlink_to(a)
    record_operation(_unknown(alias))          # 记录时真实路径 = ta.md
    b = tmp_path / "tb.md"
    b.write_text("b")
    alias.unlink()
    alias.symlink_to(b)                        # 软链改指
    record_operation(_outcome(path=str(alias)))  # 新目标 verified
    # 原调用路径仍可检索历史（含改指前的 unknown 与改指后的 verified）
    summary = list_operations(target=str(alias))
    assert summary["total"] == 2
    # pending 按记录时真实路径分组：ta.md 的 unknown 仍是待恢复目标
    assert list_operations(pending=True)["pending_count"] == 1


def test_symlinked_ledger_path_is_refused(tmp_path):
    """安全边界：预置符号链接的台账路径必须拒绝（O_NOFOLLOW）。"""
    victim = tmp_path / "victim.md"
    victim.write_text("do not touch")
    victim_mode = stat.S_IMODE(victim.stat().st_mode)
    from agent_fs.ops import _ops_path
    ops_file = _ops_file(tmp_path)
    ops_file.parent.mkdir(parents=True, exist_ok=True)
    if ops_file.exists() or ops_file.is_symlink():
        ops_file.unlink()
    os.symlink(victim, ops_file)
    result = record_operation(_outcome(path=str(tmp_path / "z.md")))
    assert result["recorded"] is False
    assert "unsafe_target" in result["reason"]
    assert victim.read_text() == "do not touch"  # 内容未被改写
    assert stat.S_IMODE(victim.stat().st_mode) == victim_mode  # 权限未被改


def test_relative_invocation_survives_symlink_repoint(tmp_path, monkeypatch):
    """回归 R1：相对调用串改指后，按原调用串仍可检索全部历史。

    记录侧保存调用串字面形式；查询侧身份集合含调用串字面归一——
    软链改指不影响按调用串命中的历史检索；pending 分组按记录时真实
    路径（改指视为不同目标），旧目标的 unknown 仍待核实。
    """
    base = tmp_path / "base"
    dir_a = base / "a"
    dir_a.mkdir(parents=True)
    (dir_a / "f.md").write_text("a")
    alias = base / "alias"
    alias.symlink_to(dir_a)
    monkeypatch.chdir(base)

    record_operation(_unknown("alias/f"))            # 记录时真实路径 = base/a/f.md
    dir_b = base / "b"
    dir_b.mkdir()
    (dir_b / "f.md").write_text("b")
    alias.unlink()
    alias.symlink_to(dir_b)                          # 软链改指
    record_operation(_outcome(path="alias/f"))       # verified，真实路径 = base/b/f.md

    summary = list_operations(target="alias/f")
    assert summary["total"] == 2                     # 两条历史都可检索
    pending = list_operations(pending=True)
    assert pending["pending_count"] == 1             # 旧目标（a/f）仍待核实
    assert "a" in pending["operations"][0]["path_real"]


def test_no_nofollow_platform_refuses_symlink_ledger(tmp_path, monkeypatch):
    """回归 R1/R2：O_NOFOLLOW 缺失平台不得静默跟随软链截断无关文件。

    通过 delattr 模拟缺失该常量的平台：lstat 预检必须拒绝预置符号链接，
    受害文件的内容与权限保持不变。
    """
    victim = tmp_path / "victim.md"
    victim.write_text("valuable data\n")
    victim_mode = stat.S_IMODE(victim.stat().st_mode)
    ops_file = _ops_file(tmp_path)
    ops_file.parent.mkdir(parents=True, exist_ok=True)
    if ops_file.exists() or ops_file.is_symlink():
        ops_file.unlink()
    os.symlink(victim, ops_file)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    result = record_operation(_outcome(path=str(tmp_path / "z.md")))
    assert result["recorded"] is False
    assert "unsafe_target" in result["reason"]
    assert victim.read_text() == "valuable data\n"
    assert stat.S_IMODE(victim.stat().st_mode) == victim_mode


def test_ledger_lock_busy_when_held_elsewhere(tmp_path):
    """回归 R2：他人持有台账 flock 时，记录返回 lock_busy 而非强行写入。"""
    import fcntl
    ops_file = _ops_file(tmp_path)
    holder = open(ops_file, "a+b")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        result = record_operation(_outcome(path=str(tmp_path / "x.md")))
        assert result["recorded"] is False
        assert "lock_busy" in result["reason"]
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    # 释放后可正常记录
    assert record_operation(_outcome(path=str(tmp_path / "x.md")))["recorded"] is True



def test_windows_binary_flag_present_in_open(tmp_path, monkeypatch):
    """回归 R1-P1：打开 flags 必须含 O_BINARY（有该常量的平台）。"""
    captured = {}
    real_open = os.open

    def spy_open(path, flags, *a, **kw):
        captured["flags"] = flags
        return real_open(path, flags, *a, **kw)

    monkeypatch.setattr(ops.os, "open", spy_open)
    record_operation(_outcome(path=str(tmp_path / "x.md")))
    assert captured["flags"] & getattr(os, "O_BINARY", 0) == getattr(os, "O_BINARY", 0)


def test_unreadable_ledger_reported_not_silent(tmp_path, capsys):
    """回归 R1-P2：台账存在但不可读取 → 显式 error（退码 2），
    不得误报为「没有待恢复操作」。"""
    import pytest as _pytest
    ops_file = _ops_file(tmp_path)
    record_operation(_unknown(tmp_path / "d.md"))
    os.chmod(ops_file, 0o000)
    try:
        # 先验证本环境确实不可读（root/特殊平台可能仍可读，则跳过）
        try:
            open(ops_file, "rb").read(1)
            readable = True
        except OSError:
            readable = False
        if readable:
            _pytest.skip("当前环境无法构造不可读台账（root 或权限语义差异）")
        code = cli.main(["ops", "list", "--pending"])
        assert code == 2
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert out["reason_code"] == "ledger_unreadable"
        assert out["pending_count"] is None
    finally:
        os.chmod(ops_file, 0o600)



def test_cli_write_rejected_is_recorded(tmp_path, capsys):
    target = tmp_path / "cli.md"
    target.write_text("v1")
    wrong = "0" * 64
    code = cli.main(["write-if-match", str(target), "--expected-sha256", wrong,
                     "--content", "v2"])
    assert code == 1  # rejected
    row = list_operations()["operations"][0]
    assert row["status"] == "rejected"
    assert row["commit_state"] == "not_committed"
    assert list_operations(pending=True)["pending_count"] == 0


def test_cli_param_validation_failure_not_recorded(tmp_path, capsys):
    target = tmp_path / "p.md"
    target.write_text("v1")
    code = cli.main(["write-if-match", str(target), "--expected-sha256", "zz",
                     "--content", "v2"])
    assert code == 2  # caller_error
    assert list_operations()["total"] == 0  # 参数预检失败不落台账


def test_cli_ops_list_command(tmp_path, capsys):
    record_operation(_unknown(tmp_path / "p.md"))
    code = cli.main(["ops", "list", "--pending"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pending_count"] == 1
    assert out["operations"][0]["status"] == "unknown"


def test_cli_edit_verified_is_recorded(tmp_path, capsys):
    target = tmp_path / "e.md"
    target.write_text("alpha")
    hbase = hashlib.sha256(target.read_bytes()).hexdigest()
    edits_file = tmp_path / "edits.json"
    edits_file.write_text(json.dumps([{"old": "alpha", "new": "beta"}],
                                     ensure_ascii=False))
    code = cli.main(["edit-if-match", str(target), "--expected-sha256", hbase,
                     "--edits-file", str(edits_file)])
    assert code == 0
    row = list_operations()["operations"][0]
    assert row["command"] == "edit-if-match"
    assert row["status"] == "verified"
    assert row["applied_edits"] == 1
    assert target.read_text() == "beta"
