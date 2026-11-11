"""无副作用的高频操作预检。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from agent_fs.edits import apply_edits, describe_failure, load_edits
from agent_fs.read_fresh import read_fresh


def _run_git(args: list[str], cwd: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


# preflight 的 reason → 下一步动作。调用方（尤其是模型）拿到 ok=false 后
# 不需要再自行推断该修什么；未收录的新 reason 走通用兜底文案。
_GUARD_NEXT_ACTION = {
    "git_diff_check_failed": "按 git diff --check 列出的位置修复空白错误后重试",
    "git_object_missing": "确认对象已拉到本地（必要时先 fetch），不要引用不存在的 commit",
    "git_command_required": "以完整的 git 命令数组形式传入预检",
    "pathspec_separator_missing": "在 revision 与 pathspec 之间加唯一一个 -- 分隔符",
    "git_subcommand_missing": "补齐 git 子命令与 revision 后再预检",
    "pathspec_missing_or_option": "在 -- 之后给出至少一个非选项形式的 pathspec",
    "not_a_git_worktree": "在目标仓库工作区内执行，或显式传入 --cwd",
    "worktree_identity_mismatch": "切换到预期的 worktree 与分支后重试；不要在此处写入",
    "content_file_unreadable": "检查 content_file 路径与读权限后重试",
    "invalid_utf8": "把待写内容重新以 UTF-8 编码保存后重试",
    "replacement_character_detected": "内容含 U+FFFD 替换字符，回源修正编码后再写",
    "target_unstable": "稍后重试：目标正在被并发写入，此时预检结论不可信",
    "target_unreadable": "检查目标路径与读权限后重试",
    "invalid_edits_file": "检查 edits 文件路径与 JSON 结构后重试",
    "empty_anchor": "为每条 edit 提供非空 old 文本",
    "old_and_new_identical": "删除 old 与 new 相同的无意义编辑",
    "anchor_not_found": "read-fresh 重读最新内容，按当前文本重写锚点",
    "anchor_not_unique": "扩大锚点上下文使其在最新内容中唯一",
    "post_check_failed": "检查编辑是否互相抵消或锚点被前面的编辑覆盖",
}
_DEFAULT_GUARD_NEXT_ACTION = "按 reason 修正前置条件后重试预检；预检未通过前不要执行对应写操作"


def _failure(reason: str, *, error: bool = False, **details: Any) -> dict[str, Any]:
    details.setdefault("next_action", _GUARD_NEXT_ACTION.get(reason, _DEFAULT_GUARD_NEXT_ACTION))
    return {"ok": False, "reason": reason, "error": error, **details}


def _success(**details: Any) -> dict[str, Any]:
    return {"ok": True, **details}


def guard_git_commit(cwd: str | None) -> dict[str, Any]:
    """同时检查工作区与暂存区的空白错误，不执行任何 Git 写操作。"""
    checks = {
        "worktree": _run_git(["diff", "--check"], cwd),
        "staged": _run_git(["diff", "--cached", "--check"], cwd),
    }
    failures = {
        name: result.stdout + result.stderr
        for name, result in checks.items()
        if result.returncode != 0
    }
    if failures:
        return _failure("git_diff_check_failed", checks=failures)
    return _success(checks={name: "passed" for name in checks})


def guard_git_object(commit: str, cwd: str | None) -> dict[str, Any]:
    """确认 commit 对象在本地对象库存在。"""
    result = _run_git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd)
    if result.returncode != 0:
        return _failure(
            "git_object_missing",
            commit=commit,
            detail=(result.stderr or result.stdout).strip(),
        )
    return _success(commit=commit, object_type="commit")


def guard_pathspec(command: list[str]) -> dict[str, Any]:
    """校验 Git 命令的分隔符，并返回解析后的 revision/pathspec。"""
    if not command or command[0] != "git":
        return _failure("git_command_required", command=command)
    if command.count("--") != 1:
        return _failure("pathspec_separator_missing", command=command)
    separator = command.index("--")
    if separator < 2:
        return _failure("git_subcommand_missing", command=command)
    subcommand = command[1]
    revisions = command[2:separator]
    pathspecs = command[separator + 1 :]
    if not pathspecs or any(not path or path == "--" or path.startswith("-") for path in pathspecs):
        return _failure("pathspec_missing_or_option", command=command)
    return _success(
        command=command,
        subcommand=subcommand,
        revisions=revisions,
        pathspecs=pathspecs,
    )


def guard_worktree(expected_root: str, expected_branch: str, cwd: str | None) -> dict[str, Any]:
    """核验当前仓库根目录和分支，阻止在错误 worktree 中继续写入。"""
    root_result = _run_git(["rev-parse", "--show-toplevel"], cwd)
    branch_result = _run_git(["branch", "--show-current"], cwd)
    if root_result.returncode != 0 or branch_result.returncode != 0:
        return _failure(
            "not_a_git_worktree",
            root_error=(root_result.stderr or root_result.stdout).strip(),
            branch_error=(branch_result.stderr or branch_result.stdout).strip(),
        )

    actual = {
        "root": str(Path(root_result.stdout.strip()).resolve()),
        "branch": branch_result.stdout.strip(),
    }
    expected = {"root": str(Path(expected_root).resolve()), "branch": expected_branch}
    if actual != expected:
        return _failure("worktree_identity_mismatch", expected=expected, actual=actual)
    return _success(expected=expected, actual=actual)


def guard_text_write(content_file: str) -> dict[str, Any]:
    """验证待写文件是无替换字符的 UTF-8 文本，不执行写入。"""
    path = Path(content_file)
    try:
        raw = path.read_bytes()
    except OSError as error:
        return _failure("content_file_unreadable", error=True, path=str(path), detail=str(error))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        return _failure("invalid_utf8", error=True, path=str(path), detail=str(error))
    if "\ufffd" in text:
        return _failure("replacement_character_detected", path=str(path))
    return _success(
        path=str(path),
        bytes=len(raw),
        next_step="use agent-fs write-if-match --content-file after verifying target hash",
    )


def guard_multiedit(target: str, edits_file: str) -> dict[str, Any]:
    """预检 MultiEdit：与 edit-if-match 共用编辑材料解析与顺序应用语义。

    预检结论必须与实际写入一致，因此这里不再另算一遍锚点：材料解析和
    顺序应用都走 `edits.py`，`ok` 以「实际顺序应用是否成功」为准。
    原始内容里的锚点区间重叠只作为 warning——顺序应用下前一个编辑改变
    后一个锚点位置是合法用法，不足以判定失败。
    """
    target_path = Path(target)
    stable_read = read_fresh(target_path)
    if not stable_read.stable:
        return _failure("target_unstable", path=target, detail=stable_read.reason)
    if stable_read.content is None or stable_read.sha256 is None:
        return _failure(
            "target_unreadable", error=True, path=target, detail="binary_or_undecodable"
        )
    material = load_edits(edits_file)
    if material.edits is None:
        # 材料不可读/格式非法是调用方错误；空锚点与 old==new 是材料语义
        # 问题，带上 catalog 下标便于调用方直接定位修复。
        if material.error_code in {"empty_anchor", "old_and_new_identical"}:
            return _failure(material.error_code, edit_index=material.error_index)
        return _failure("invalid_edits_file", error=True, path=edits_file)
    edits = material.edits

    application = apply_edits(stable_read.content, edits)
    if not application.ok:
        return _failure(
            application.reason_code,
            edit_index=application.failed_index,
            occurrences=application.occurrences,
            positions=list(application.positions),
            lines=list(application.line_numbers),
            detail=describe_failure(application, edits),
        )
    warnings = _overlap_warnings(stable_read.content, edits)
    result = _success(
        target=str(target_path.resolve()),
        edit_count=len(edits),
        sha256=stable_read.sha256,
        next_step="verify target hash before applying external edits",
    )
    if warnings:
        result["warnings"] = warnings
    return result


def _overlap_warnings(content: str, edits: list[dict[str, str]]) -> list[str]:
    """报告原始内容上的锚点区间重叠（仅供调用方知情，不参与判定）。"""
    warnings: list[str] = []
    intervals: list[tuple[int, int, int]] = []  # (edit_index, start, end)
    for index, edit in enumerate(edits):
        old = edit["old"]
        if content.count(old) != 1:
            continue
        start = content.index(old)
        end = start + len(old)
        for previous_index, previous_start, previous_end in intervals:
            if start < previous_end and previous_start < end:
                warnings.append(
                    f"edit[{index}] 与 edit[{previous_index}] 的锚点在原始内容中区间重叠；"
                    "顺序应用仍可成功，结论以实际应用为准"
                )
        intervals.append((index, start, end))
    return warnings
