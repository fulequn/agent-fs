"""agent-fs CLI 入口：snapshot / read-fresh / verify / changed / write-if-match /
edit-if-match / reconcile / events / guard。

所有命令统一以 JSON 输出到 stdout，方便 Agent 解析；退出码用于快速判定成败：
- 0：操作成功且判定为"稳定/匹配/未变化/写入成功/预览成功"
- 1：操作成功但判定为"不稳定/不匹配/已变化/写入被拒绝"（不是程序错误，是业务结论）
- 2：程序执行出错（文件不存在、参数错误等）
- 3：写入结果未知（已替换但结果不符/不可读，或不能判定是否提交）——
  只允许进入核实流程（reconcile），不允许直接重放写入

消费方必须同时识别命令与 status/commit_state，不能只看退出码或 written=true。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

from agent_fs.changed import compute_changed
from agent_fs.edit_if_match import edit_if_match
from agent_fs.events import AnomalyEvent, normalize_error, record_event
from agent_fs.fingerprint import DEFAULT_STABLE_MAX_RETRIES, compute_stable_fingerprint
from agent_fs.guard import (
    guard_git_commit,
    guard_git_object,
    guard_multiedit,
    guard_pathspec,
    guard_text_write,
    guard_worktree,
)
from agent_fs.ops import list_operations, record_operation
from agent_fs.read_fresh import DEFAULT_MAX_RETRIES, read_fresh
from agent_fs.reconcile import reconcile, reconcile_argument_error
from agent_fs.result import (
    COMMAND_EDIT_IF_MATCH,
    COMMAND_WRITE_IF_MATCH,
    COMMIT_COMMITTED,
    SCHEMA_VERSION,
    argument_error,
    current_call_context,
    error_outcome,
    reason_to_exit,
    record_call_context,
    reset_call_context,
    unknown_outcome,
)
from agent_fs.result import current_stage as current_result_stage
from agent_fs.result import reset_stage as reset_result_stage
from agent_fs.store import (
    SnapshotCorruptedError,
    load_snapshot,
    make_snapshot_id,
    prune_snapshots,
    save_snapshot,
)
from agent_fs.write_atomic import write_if_match

EXIT_OK = 0
EXIT_NEGATIVE = 1
EXIT_ERROR = 2
EXIT_UNKNOWN = 3  # 写入结果未知：只进入核实流程，不自动重试

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _normalize_hash(value: str) -> str:
    """校验通过后统一转为小写：文件指纹始终是小写十六进制，大小写混用会让
    等值比较误判冲突（审查 P2-6：大写 Htarget 使 reconcile 误报 diverged）。"""
    return value.lower()


def _print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _paths_alias(left: Path, right: Path) -> bool:
    """两个路径是否指向同一文件（含符号链接/硬链接/大小写别名）。

    `Path.resolve()` 只能消除符号链接与 `..`，不能识别硬链接；macOS/Windows
    的大小写不敏感文件系统上同一文件也可能得到不同字符串。因此优先用
    `os.path.samefile`（比较 inode/文件索引），失败时再退回 resolve 比较。
    """
    try:
        if left.exists() and right.exists():
            try:
                return os.path.samefile(left, right)
            except OSError:
                pass
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError):
        return os.path.abspath(str(left)) == os.path.abspath(str(right))


def _write_text_atomic(path: Path, content: str) -> None:
    """把文本原子落盘：同目录临时文件 + fsync + os.replace。

    直接 `Path.write_text` 会先截断目标再写入，进程中断或磁盘写满会留下
    半截文件。输出路径若是符号链接，按 `write_text` 的既有语义写入其指向
    的真实文件（而不是把链接本身替换成普通文件）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    target = path.resolve() if path.is_symlink() else path
    if target.exists() and target.is_dir():
        raise IsADirectoryError(f"输出路径是目录: {target}")
    try:
        # 只继承普通权限位（0o777），不继承 setuid/setgid/sticky：输出文件是
        # 源文件副本，沿用特殊位会把原文件的提权语义带到一份新内容上。
        mode = target.stat().st_mode & 0o777
    except OSError:
        mode = 0o600  # 新文件默认仅所有者可读写（内容是源文件副本）
    tmp_path = target.parent / f".{target.name}.agentfs-out-{uuid.uuid4().hex[:8]}"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    # 目录项变更落盘失败不影响已完成的替换，仅尽力而为（与 write_atomic 一致）
    try:
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _validate_sha256(value: str, path: Path) -> dict | None:
    """校验哈希格式，不合法则返回可直接输出的错误 dict，合法返回 None。"""
    if not _SHA256_RE.match(value):
        return argument_error(
            "write-if-match",
            str(path),
            "invalid_sha256_format",
            f"invalid_sha256_format: expected 64 位十六进制字符串，实际长度 {len(value)}",
        )
    return None


def cmd_snapshot(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if args.file == "prune":
        return _snapshot_prune(args)
    result = compute_stable_fingerprint(path, max_retries=args.max_retries)

    if not result.stable:
        _print_json(
            {
                "stable": False,
                "path": str(path),
                "attempts": result.attempts,
                "reason": result.reason,
                "next_action": (
                    "检查路径拼写；不要隐式新建文件"
                    if result.reason == "file_not_found"
                    else "稍后重试；期间不要基于旧内容进行写入"
                ),
            }
        )
        # 文件不存在属于程序性错误；"反复读取都不稳定"属于业务性结论，
        # 用退出码 1 提示调用方"暂时无法采集到可信快照，请稍后重试"。
        return EXIT_ERROR if result.reason == "file_not_found" else EXIT_NEGATIVE

    fp = result.fingerprint
    assert fp is not None
    snapshot_id = make_snapshot_id(path, fp.taken_at)
    save_snapshot(snapshot_id, fp.to_dict())
    _print_json(
        {
            **fp.to_dict(),
            "snapshot_id": snapshot_id,
            "attempts": result.attempts,
            "next_action": (
                "把 snapshot_id 作为 verify/changed 的基线；"
                "写入先用 read-fresh 取最新内容与哈希，再走 write-if-match/edit-if-match"
            ),
        }
    )
    return EXIT_OK


def _snapshot_prune(args: argparse.Namespace) -> int:
    """`snapshot prune`：按条件清理本地快照，未给条件时默认不删任何文件。"""
    if args.older_than is None and args.keep_last is None:
        _print_json(
            argument_error(
                "snapshot-prune",
                _snapshots_dir_for_message(),
                "invalid_arguments",
                "invalid_arguments: 至少给出 --older-than（天）或 --keep-last（每个目标保留份数）"
                " 之一；未给条件时默认不删除任何快照",
                suggested_command="agent-fs snapshot prune --keep-last 10 --dry-run",
                next_action="先用 --dry-run 预览，再决定是否实际删除",
            )
        )
        return EXIT_ERROR
    for name, value in (("--older-than", args.older_than), ("--keep-last", args.keep_last)):
        if value is not None and value < 0:
            _print_json(
                argument_error(
                    "snapshot-prune",
                    _snapshots_dir_for_message(),
                    "invalid_arguments",
                    f"invalid_arguments: {name} 必须为非负数，实际 {value}",
                    next_action="用非负数值重试",
                )
            )
            return EXIT_ERROR

    try:
        result = prune_snapshots(
            older_than_days=args.older_than,
            keep_last=args.keep_last,
            dry_run=args.dry_run,
        )
    except ValueError as error:
        _print_json(
            argument_error(
                "snapshot-prune", _snapshots_dir_for_message(), "invalid_arguments", str(error)
            )
        )
        return EXIT_ERROR

    _print_json({"command": "snapshot-prune", **result})
    return EXIT_NEGATIVE if result["failed"] else EXIT_OK


def _snapshots_dir_for_message() -> str:
    """只用于错误输出：不触发建目录。"""
    return str(Path(os.environ.get("AGENT_FS_HOME", Path.home() / ".agent-fs")) / "snapshots")


def cmd_read_fresh(args: argparse.Namespace) -> int:
    path = Path(args.file)
    # 参数非法在触达文件系统前就拒绝：负 offset/limit 曾被当作“读取不稳定”
    # 走 exit 1，调用方无法区分“参数写错”与“文件正被并发修改”。
    for name, value in (("offset", args.offset), ("limit", args.limit)):
        if value is not None and value < 0:
            _print_json(
                argument_error(
                    "read-fresh",
                    str(path),
                    "invalid_offset_or_limit",
                    f"invalid_offset_or_limit: --{name} 必须为非负整数，实际 {value}",
                    next_action="用非负整数 offset/limit 重试",
                )
            )
            return EXIT_ERROR
    # 文件不存在/不是普通文件属于程序性错误（与 snapshot/verify/changed 的退出码语义保持一致），
    # 不应和"读取期间被并发修改"这种业务性不稳定结论共用退出码 1，避免调用方脚本误判。
    if not path.is_file():
        _print_json(
            argument_error(
                "read-fresh",
                str(path),
                "file_not_found" if not path.exists() else "not_a_regular_file",
                f"文件不存在或不是普通文件: {path}",
                next_action="检查路径拼写；不要隐式新建文件",
            )
        )
        return EXIT_ERROR

    result = read_fresh(
        path,
        offset=args.offset,
        limit=args.limit,
        max_retries=args.max_retries,
    )
    data = result.to_dict()

    # --max-bytes：按**字节**截断（参数名承诺的是 bytes；曾按字符数截断，
    # 中文 UTF-8 3 字节/字符会超出指定字节数）。在多字节字符边界处安全
    # 切断（不产生半个字符），并按截断前的完整内容记录总字节数。
    # 负数是调用方输入错误（Python 负切片语义会静默取尾部，不合法）。
    if args.max_bytes is not None and args.max_bytes < 0:
        _print_json(
            argument_error(
                "read-fresh",
                str(path),
                "invalid_max_bytes",
                f"invalid_max_bytes: 必须为非负整数，实际 {args.max_bytes}",
                suggested_command="agent-fs read-fresh <file> --max-bytes 4096",
                next_action="用非负字节值重试",
            )
        )
        return EXIT_ERROR
    content = data.get("content")
    if content is not None and args.max_bytes is not None:
        encoded = content.encode("utf-8")
        if len(encoded) > args.max_bytes:
            data["content"] = encoded[: args.max_bytes].decode("utf-8", errors="ignore")
            data["content_truncated"] = True
            data["content_total_bytes"] = len(encoded)
            data["next_action"] = "内容已按字节截断，需要剩余部分时用 --offset 分段读取"
    # --no-content：只取元信息（哈希/行数），正文走 --output 落盘
    if args.no_content:
        data["content"] = None
        data["content_omitted"] = True
        data.setdefault("next_action", "正文已省略；需要时用 --output 落盘或去掉 --no-content")
    # --output：正文落盘，stdout 只报元信息（大文件主路径）
    if args.output and content is not None:
        out_path = Path(args.output)
        # 输出路径与输入文件是同一文件（含符号链接/硬链接/大小写别名）时，
        # 写入会先截断源文件：读取成功但内容已被自己覆盖，且返回的哈希
        # 指向旧内容。必须拒绝，而不是把这种数据丢失包装成成功。
        if _paths_alias(out_path, path):
            _print_json(
                {
                    "error": (
                        "output_path_conflict: --output 与输入文件指向同一文件，"
                        "继续写入会截断源文件"
                    ),
                    "path": str(path),
                    "output": str(out_path),
                    "stable": result.stable,
                    "reason_code": "output_path_conflict",
                    "category": "caller_error",
                    "retryable": False,
                    "next_action": "改用不同的输出路径；需要原地改写请用 write-if-match/edit-if-match",
                }
            )
            return EXIT_ERROR
        try:
            _write_text_atomic(out_path, content)
        except OSError as e:
            _print_json(
                {
                    "error": f"output_write_failed: {e.__class__.__name__}: {e}",
                    "path": str(path),
                    "output": str(out_path),
                    "reason_code": "output_write_failed",
                    "category": "caller_error",
                    "retryable": False,
                    "next_action": "检查输出目录权限后重试",
                }
            )
            return EXIT_ERROR
        data["content"] = None
        data["content_omitted"] = True
        data["output"] = str(out_path)
        data["next_action"] = (
            f"正文已写入 {out_path}；哈希 {data.get('sha256', '?')} 可直接用于 edit-if-match"
        )

    if "next_action" not in data:
        data["next_action"] = (
            "读取成功，可基于该内容与 sha256 做后续操作"
            if result.stable
            else "读取不稳定：稍后重试或检查是否有进程正在写该文件"
        )

    # --compact：单行 JSON（默认 indent=2 的展开格式对大结果更友好）
    if args.compact:
        print(json.dumps(data, ensure_ascii=False))
    else:
        _print_json(data)
    return EXIT_OK if result.stable else EXIT_NEGATIVE


def _snapshot_matches_target(snapshot: dict, path: Path) -> bool:
    """快照记录的 path 是否属于当前目标文件（含软链接/大小写别名）。"""
    snap_path = snapshot.get("path")
    if not isinstance(snap_path, str) or not snap_path:
        return False
    try:
        return _paths_alias(Path(snap_path), path)
    except OSError:
        return str(Path(snap_path)) == str(path)


def _resolve_expected_sha256(
    args, path: Path, command: str = "verify"
) -> tuple[str | None, dict | None]:
    """解析期望哈希：--expected-sha256 / --sha256（兼容别名）/ --snapshot。

    返回 (sha256, error_dict)。snapshot ID 形如 snap-<16hex>-<epoch_ms>，
    从本地快照库加载目标路径的哈希（问题页上多次出现把 snapshot ID 当
    SHA-256 传给 --expected-sha256 的误用）。错误统一 schema-2 形态。
    """
    raw = args.expected_sha256
    if getattr(args, "sha256", None):
        raw = args.sha256
    if getattr(args, "snapshot", None):
        snap_id = args.snapshot
        try:
            snap = load_snapshot(snap_id)
        except FileNotFoundError:
            return None, argument_error(
                command,
                str(path),
                "snapshot_not_found",
                f"snapshot_not_found: {snap_id}",
                suggested_command="agent-fs snapshot <file>",
                next_action="先对目标文件执行 snapshot，再用返回的 snapshot_id",
            )
        except SnapshotCorruptedError as error:
            return None, argument_error(
                command,
                str(path),
                "snapshot_corrupted",
                str(error),
                next_action="重新执行 snapshot 采集",
            )
        if not _snapshot_matches_target(snap, path):
            return None, argument_error(
                command,
                str(path),
                "snapshot_path_mismatch",
                f"snapshot_path_mismatch: 快照属于 {snap.get('path')}，但校验目标是 {path}",
                next_action="用属于该文件的 snapshot，或改用 --expected-sha256",
            )
        sha = snap.get("sha256")
        if not sha:
            return None, argument_error(
                command,
                str(path),
                "snapshot_corrupted",
                "snapshot_corrupted: 快照缺少 sha256 字段",
                next_action="重新执行 snapshot 采集",
            )
        return _normalize_hash(sha), None

    if raw is None:
        return None, argument_error(
            command,
            str(path),
            "missing_hash",
            "missing_hash: 需要 --expected-sha256 / --sha256 / --snapshot 之一",
            suggested_command=f"agent-fs {command} <file> --snapshot <snapshot-id>",
            next_action="snapshot 采集后用 --snapshot，或直接传 --expected-sha256",
        )
    if not _SHA256_RE.match(raw):
        return None, argument_error(
            command,
            str(path),
            "invalid_sha256_format",
            (
                f"invalid_sha256_format: expected 64 位十六进制字符串，实际长度 {len(raw)}"
                + ("；若想用快照请传 --snapshot" if raw.startswith("snap-") else "")
            ),
            next_action="检查哈希格式；snapshot ID 请用 --snapshot 参数",
        )
    return _normalize_hash(raw), None


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.file)

    expected, err = _resolve_expected_sha256(args, path, command="verify")
    if err is not None:
        _print_json(err)
        return EXIT_ERROR

    result = compute_stable_fingerprint(path, max_retries=args.max_retries)
    if not result.stable:
        _print_json(
            {
                "stable": False,
                "path": str(path),
                "attempts": result.attempts,
                "reason": result.reason,
                "next_action": (
                    "检查路径拼写；不要隐式新建文件"
                    if result.reason == "file_not_found"
                    else "稍后重试；期间不要基于旧内容进行写入"
                ),
            }
        )
        return EXIT_ERROR if result.reason == "file_not_found" else EXIT_NEGATIVE

    fp = result.fingerprint
    assert fp is not None
    match = fp.sha256 == expected
    _print_json(
        {
            "match": match,
            "stable": True,
            "path": str(path),
            "expected_sha256": expected,
            "current_sha256": fp.sha256,
            "size": fp.size,
            "next_action": (
                "基线一致，可继续后续操作"
                if match
                else "文件已被修改：read-fresh 重读最新内容后基于新基线操作"
            ),
        }
    )
    return EXIT_OK if match else EXIT_NEGATIVE


def cmd_changed(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        snapshot = load_snapshot(args.since)
    except FileNotFoundError:
        _print_json(
            argument_error(
                "changed",
                str(path),
                "snapshot_not_found",
                f"snapshot_not_found: {args.since}",
                suggested_command="agent-fs snapshot <file>",
                next_action="先对目标文件执行 snapshot，再用返回的 snapshot_id",
            )
        )
        return EXIT_ERROR
    except SnapshotCorruptedError as error:
        _print_json(
            argument_error(
                "changed",
                str(path),
                "snapshot_corrupted",
                str(error),
                next_action="重新执行 snapshot 采集",
            )
        )
        return EXIT_ERROR
    if not _snapshot_matches_target(snapshot, path):
        _print_json(
            argument_error(
                "changed",
                str(path),
                "snapshot_path_mismatch",
                f"snapshot_path_mismatch: 快照属于 {snapshot.get('path')}，但比较目标是 {path}",
                next_action="用属于该文件的 snapshot",
            )
        )
        return EXIT_ERROR

    result = compute_changed(path, snapshot, args.since, max_retries=args.max_retries)
    payload = result.to_dict()
    payload["next_action"] = _changed_next_action(result)
    _print_json(payload)
    if not result.stable:
        # 读不稳定是"暂时无法给出可信结论"，不是参数错误：exit 1 提示稍后重试
        return EXIT_NEGATIVE
    return EXIT_OK if not result.changed else EXIT_NEGATIVE


def _changed_next_action(result) -> str:
    """把 changed 的 reason 翻成下一步动作：结论即行动，调用方无需自己查表。"""
    if not result.stable:
        return "稍后重试；期间不要基于旧结论写入"
    if result.reason == "file_missing_now":
        return "文件已消失：确认是否为预期；需要重建时显式创建并重新 snapshot"
    if result.reason == "content_unchanged":
        return "内容未变，可继续基于该基线操作"
    return "内容已变：read-fresh 重读最新内容后基于新基线操作"


def _run_guard(check) -> int:
    result = check()
    _print_json(result)
    if result["ok"]:
        return EXIT_OK
    return EXIT_ERROR if result.get("error") else EXIT_NEGATIVE


def cmd_guard_git_commit(args: argparse.Namespace) -> int:
    return _run_guard(lambda: guard_git_commit(args.cwd))


def cmd_guard_git_object(args: argparse.Namespace) -> int:
    return _run_guard(lambda: guard_git_object(args.commit, args.cwd))


def cmd_guard_pathspec(args: argparse.Namespace) -> int:
    return _run_guard(lambda: guard_pathspec(args.command))


def cmd_guard_worktree(args: argparse.Namespace) -> int:
    return _run_guard(lambda: guard_worktree(args.expected_root, args.expected_branch, args.cwd))


def cmd_guard_text_write(args: argparse.Namespace) -> int:
    return _run_guard(lambda: guard_text_write(args.content_file))


def cmd_guard_multiedit(args: argparse.Namespace) -> int:
    return _run_guard(lambda: guard_multiedit(args.target, args.edits_file))


def cmd_write_if_match(args: argparse.Namespace) -> int:
    path = Path(args.file)

    err = _validate_sha256(args.expected_sha256, path)
    if err is not None:
        _print_json(err)
        return EXIT_ERROR
    expected = _normalize_hash(args.expected_sha256)

    # Htarget（预期结果哈希）：新 Skill 的受控主流程必传；传入后写入核心会
    # 在替换前校验"实际准备字节 == 该值"，响应丢失后调用方才有完整的恢复条件。
    # 显式传入空字符串不是"未声明"：必须报参数错误并保留原文（审查 P2-7，
    # 旧实现的真值判断会让 --expected-result-sha256 '' 静默跳过 Htarget 校验）。
    desired = None
    raw_target = getattr(args, "expected_result_sha256", None)
    if raw_target is not None:
        if not _SHA256_RE.match(raw_target):
            _print_json(
                argument_error(
                    "write-if-match",
                    str(path),
                    "invalid_sha256_format",
                    (
                        "invalid_sha256_format: --expected-result-sha256 须为 64 位"
                        f"十六进制字符串，实际长度 {len(raw_target)}"
                        + ("" if raw_target else "（传入了空字符串；不校验请省略该参数）")
                    ),
                )
            )
            return EXIT_ERROR
        desired = _normalize_hash(raw_target)

    # 已校验的 Hbase/Htarget 登记，供 main() 兜底异常时还原统一结果结构
    record_call_context(
        command="write-if-match", path=str(path),
        expected_sha256=expected, desired_sha256=desired,
    )

    if args.content_file is not None:
        content_path = Path(args.content_file)
        if not content_path.is_file():
            _print_json(
                argument_error(
                    "write-if-match",
                    str(path),
                    "material_unreadable",
                    f"--content-file 指向的文件不存在: {content_path}",
                )
            )
            return EXIT_ERROR
        try:
            content = content_path.read_bytes()
        except OSError as e:
            _print_json(
                argument_error(
                    "write-if-match",
                    str(path),
                    "material_unreadable",
                    f"读取 --content-file 失败: {e.__class__.__name__}: {e}",
                )
            )
            return EXIT_ERROR
    elif args.content is not None:
        content = args.content.encode("utf-8")
    else:
        content = sys.stdin.buffer.read()

    # 不在命令层局部捕获异常：write_if_match 抛出的任何异常由 main() 兜底按
    # 执行阶段分流（pre_commit → error/2；post_commit → unknown/committed/3）。
    # 局部捕获无法感知提交阶段，会把"已替换后的异常"误报为 not_committed（审查 P1-1）。
    outcome = write_if_match(path, content, expected, desired_sha256=desired)

    record_operation(outcome)
    _print_json(outcome.to_dict())
    return reason_to_exit(outcome.status)


class _JsonArgumentError(Exception):
    """argparse 参数错误的结构化载体（由 main 捕获后输出 JSON）。"""

    def __init__(
        self,
        message: str,
        command: str | None = None,
        suggested_command: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.command = command
        self.suggested_command = suggested_command


class _JsonArgumentParser(argparse.ArgumentParser):
    """参数错误时抛 _JsonArgumentError 而非 SystemExit(2)+usage 文本。

    旧实现参数错误输出非 JSON 的 argparse usage，与"所有命令统一 JSON
    输出"的声明不一致（问题页上 verify --snapshot/--sha256 误用全部
    得到的是 usage 文本）。这里统一转为结构化错误：category/reason/
    suggested_command/retryable。
    """

    def error(self, message: str):
        # 子解析器 prog 形如 "agent-fs write-if-match"（guard 再嵌一层）；
        # 根解析器 prog 就是 "agent-fs"。从 prog 前缀剥离出子命令名，
        # 使 command 与 suggested_command 不再出现 "agent-fs agent-fs" 重复
        # （审查 P3：旧实现在 main 里用根 parser.prog 二次拼接导致前缀重复）。
        sub_name = (
            self.prog[len("agent-fs "):].strip()
            if self.prog.startswith("agent-fs ")
            else None
        )
        suggested = f"agent-fs {sub_name} --help" if sub_name else "agent-fs --help"
        raise _JsonArgumentError(message, sub_name, suggested)


def _normalize_since(raw: str) -> str | None:
    """把 --since 归一成事件文件里的时间格式（UTC、秒级、+00:00 后缀）。

    事件文件内的 last_seen 都是同一格式，归一后可以直接做字符串比较；
    非法时间返回 None，由调用方报参数错误。
    """
    from datetime import datetime, timezone

    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def cmd_events_list(args: argparse.Namespace) -> int:
    from agent_fs.events import SYNCABLE_CATEGORIES, load_events

    if args.limit is not None and args.limit < 0:
        _print_json(
            argument_error(
                "events-list",
                "",
                "invalid_arguments",
                f"invalid_arguments: --limit 必须为非负整数，实际 {args.limit}",
                next_action="用非负整数 --limit 重试",
            )
        )
        return EXIT_ERROR
    since = None
    if args.since is not None:
        since = _normalize_since(args.since)
        if since is None:
            _print_json(
                argument_error(
                    "events-list",
                    "",
                    "invalid_arguments",
                    f"invalid_arguments: --since 不是合法时间: {args.since}",
                    next_action="用 ISO-8601 时间（如 2026-10-01 或 2026-10-01T00:00:00Z）",
                )
            )
            return EXIT_ERROR

    events = load_events()
    if args.syncable:
        events = [e for e in events if e.get("category") in SYNCABLE_CATEGORIES]
    if since is not None:
        events = [e for e in events if e.get("last_seen", "") >= since]
    events.sort(key=lambda e: e.get("last_seen", ""), reverse=True)
    total = len(events)
    if args.limit is not None:
        events = events[: args.limit]
    _print_json(
        {
            "count": len(events),
            "total": total,
            "events": events,
            "next_action": (
                "仅 adapter_inconsistency / agent_fs_core 可同步学城；同步须用户显式触发"
                if events
                else "本地事件流为空（或当前过滤条件无匹配）"
            ),
        }
    )
    return EXIT_OK


def cmd_events_compact(args: argparse.Namespace) -> int:
    """显式压缩事件文件（按指纹聚合后原子重写）。"""
    from agent_fs.events import compact_events

    result = compact_events()
    _print_json(result)
    return EXIT_OK if result.get("compacted") else EXIT_NEGATIVE


def cmd_edit_if_match(args: argparse.Namespace) -> int:
    path = Path(args.file)

    expected, err = _resolve_expected_sha256(args, path, command="edit-if-match")
    if err is not None:
        _print_json(err)
        return EXIT_ERROR

    desired = None
    raw_target = getattr(args, "expected_result_sha256", None)
    # 与 write-if-match 同口径：空字符串是显式传入的非法 Htarget，报参数错误
    # 并保留原文（审查 P2-7），不能静默当作"未声明"跳过校验。
    if raw_target is not None:
        if not _SHA256_RE.match(raw_target):
            _print_json(
                argument_error(
                    "edit-if-match",
                    str(path),
                    "invalid_sha256_format",
                    (
                        "invalid_sha256_format: --expected-result-sha256 须为 64 位"
                        f"十六进制字符串，实际长度 {len(raw_target)}"
                        + ("" if raw_target else "（传入了空字符串；不校验请省略该参数）")
                    ),
                )
            )
            return EXIT_ERROR
        desired = _normalize_hash(raw_target)

    record_call_context(
        command="edit-if-match", path=str(path),
        expected_sha256=expected, desired_sha256=desired,
    )

    outcome = edit_if_match(
        path,
        args.edits_file,
        expected,
        desired_sha256=desired,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    record_operation(outcome)
    _print_json(outcome.to_dict())
    # 调用方输入错误（edits 文件非法等）→ exit 2；业务性拒绝 → exit 1；
    # 已替换但结果未知 → exit 3（由 status 统一映射）
    return reason_to_exit(outcome.status)


def cmd_reconcile(args: argparse.Namespace) -> int:
    """中断后的只读核实：仅比较 Hbase/Htarget/Hobserved，不写目标（SPEC §7.1）。"""
    path = Path(args.file)
    for label, value in (
        ("--base-sha256", args.base_sha256),
        ("--desired-sha256", args.desired_sha256),
    ):
        if not _SHA256_RE.match(value):
            _print_json(
                reconcile_argument_error(
                    f"invalid_sha256_format: {label} 须为 64 位十六进制字符串，"
                    f"实际长度 {len(value)}"
                )
            )
            return EXIT_ERROR

    outcome = reconcile(
        path,
        _normalize_hash(args.base_sha256),
        _normalize_hash(args.desired_sha256),
        max_retries=args.max_retries,
    )
    _print_json(outcome.to_dict())
    return outcome.exit_code


def cmd_ops_list(args: argparse.Namespace) -> int:
    """操作台账查询：发现「可能已写入但结果未核实」的目标（跨会话恢复证据）。

    pending 按目标最新一条受控写入判定（最新行为 unknown 才算待恢复）。
    核实走 reconcile，不自动重试写入。
    """
    limit = getattr(args, "limit", None)
    if limit is not None and limit < 0:
        _print_json(
            argument_error(
                "ops",
                None,
                "invalid_arguments",
                f"invalid_arguments: --limit 须为非负整数，实际 {limit}",
                next_action="使用非负 --limit 重试；0 表示不返回条目",
            )
        )
        return EXIT_ERROR
    summary = list_operations(
        pending=bool(getattr(args, "pending", False)),
        target=getattr(args, "target", None),
        limit=limit,
    )
    _print_json(summary)
    if summary.get("status") == "error":
        return EXIT_ERROR  # 台账不可读取：恢复证据不可用，不得误报为空
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="agent-fs",
        description="多 Agent 文件协作的新鲜度校验与乐观并发控制 CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_snapshot = sub.add_parser(
        "snapshot",
        help="采集文件的稳定快照；`snapshot prune` 清理本地快照（默认不删）",
    )
    p_snapshot.add_argument("file", help="目标文件路径；传 `prune` 时改为清理本地快照")
    p_snapshot.add_argument(
        "--max-retries", type=int, default=DEFAULT_STABLE_MAX_RETRIES, help="不稳定时的最大重试次数"
    )
    p_snapshot.add_argument(
        "--older-than",
        type=float,
        metavar="DAYS",
        help="prune：删除修改时间早于 N 天的快照",
    )
    p_snapshot.add_argument(
        "--keep-last",
        type=int,
        metavar="N",
        help="prune：每个目标文件保留最新 N 份快照，只删除窗口外的",
    )
    p_snapshot.add_argument(
        "--dry-run",
        action="store_true",
        help="prune：只列出将删除的快照，不实际删除",
    )
    p_snapshot.set_defaults(func=cmd_snapshot)

    p_read = sub.add_parser(
        "read-fresh", help="新鲜读取：stat-read-stat 三明治校验，防止读到中间态"
    )
    p_read.add_argument("file", help="目标文件路径")
    p_read.add_argument("--offset", type=int, default=None, help="起始行号（从 0 开始）")
    p_read.add_argument("--limit", type=int, default=None, help="读取行数")
    p_read.add_argument(
        "--max-bytes",
        type=int,
        default=None,
        help="截断正文到指定字节数（防止大文件刷爆上下文；stdout 中 content 被截断并标记 content_truncated）",
    )
    p_read.add_argument(
        "--no-content",
        action="store_true",
        help="stdout 只输出元信息（哈希/行数），不含正文",
    )
    p_read.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="正文写入该文件，stdout 只报元信息与落盘路径（大文件推荐路径）",
    )
    p_read.add_argument(
        "--compact",
        action="store_true",
        help="单行 JSON 输出（默认为 indent=2 展开格式）",
    )
    p_read.add_argument(
        "--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="不稳定时的最大重试次数"
    )
    p_read.set_defaults(func=cmd_read_fresh)

    p_verify = sub.add_parser(
        "verify",
        help="校验当前文件哈希是否与期望哈希一致（写前检查，不是原子 CAS，见 write-if-match）",
    )
    p_verify.add_argument("file", help="目标文件路径")
    hash_group = p_verify.add_mutually_exclusive_group(required=True)
    hash_group.add_argument("--expected-sha256", help="预期的 SHA-256 哈希")
    hash_group.add_argument("--sha256", help="--expected-sha256 的兼容别名")
    hash_group.add_argument("--snapshot", help="快照 ID（加载该快照记录的哈希做校验）")
    p_verify.add_argument(
        "--max-retries", type=int, default=DEFAULT_STABLE_MAX_RETRIES, help="不稳定时的最大重试次数"
    )
    p_verify.set_defaults(func=cmd_verify)

    p_changed = sub.add_parser("changed", help="对比某个历史快照与当前文件状态")
    p_changed.add_argument("file", help="目标文件路径")
    p_changed.add_argument("--since", required=True, help="快照 ID（由 snapshot 命令生成）")
    p_changed.add_argument(
        "--max-retries", type=int, default=DEFAULT_STABLE_MAX_RETRIES, help="不稳定时的最大重试次数"
    )
    p_changed.set_defaults(func=cmd_changed)

    p_write = sub.add_parser(
        "write-if-match",
        help="仅当当前文件哈希等于 expected-sha256 时才写入新内容（单进程内的受控替换）",
    )
    p_write.add_argument("file", help="目标文件路径")
    p_write.add_argument("--expected-sha256", required=True, help="写入前必须匹配的哈希（乐观锁，Hbase）")
    p_write.add_argument(
        "--expected-result-sha256",
        default=None,
        help="本次修改完成后预期完整文件字节的 SHA-256（Htarget）；替换前与实际准备字节强校验",
    )
    content_group = p_write.add_mutually_exclusive_group()
    content_group.add_argument("--content", help="直接以字符串形式提供新内容（UTF-8 编码）")
    content_group.add_argument("--content-file", help="从指定文件读取新内容的字节")
    p_write.set_defaults(func=cmd_write_if_match)

    p_edit = sub.add_parser(
        "edit-if-match",
        help="锁内一次完成多点受控编辑（稳定读取→锚点校验→应用→复核→原子替换；替代 guard multiedit+verify+MultiEdit 三段式）",
    )
    p_edit.add_argument("file", help="目标文件路径")
    edit_hash_group = p_edit.add_mutually_exclusive_group(required=True)
    edit_hash_group.add_argument("--expected-sha256", help="基线哈希（乐观锁）")
    edit_hash_group.add_argument("--sha256", help="--expected-sha256 的兼容别名")
    edit_hash_group.add_argument("--snapshot", help="快照 ID（加载该快照的哈希做基线）")
    p_edit.add_argument(
        "--edits-file",
        required=True,
        help='JSON 编辑列表文件：[{"old": "锚点文本", "new": "替换文本"}, ...]（按顺序应用，锚点须唯一）',
    )
    p_edit.add_argument(
        "--expected-result-sha256",
        default=None,
        help="全部编辑应用后的预期完整文件字节 SHA-256（Htarget）；与实际变换结果强校验",
    )
    p_edit.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "只读预览：不取锁、不修改目标，返回 status=prepared 与预期结果哈希"
            "（Hbase/Htarget/编辑数/edits 文件哈希）；实际写入仍会重新校验全部前提"
        ),
    )
    p_edit.set_defaults(func=cmd_edit_if_match)

    p_reconcile = sub.add_parser(
        "reconcile",
        help="中断后的只读核实：比较 Hbase/Htarget/当前哈希，不写目标、不回滚、不重放",
    )
    p_reconcile.add_argument("file", help="目标文件路径")
    p_reconcile.add_argument(
        "--base-sha256", required=True, help="构造本次修改所依据的原文哈希（Hbase）"
    )
    p_reconcile.add_argument(
        "--desired-sha256", required=True, help="本次修改完成后的预期结果哈希（Htarget）"
    )
    p_reconcile.add_argument(
        "--max-retries", type=int, default=DEFAULT_STABLE_MAX_RETRIES, help="不稳定时的最大重试次数"
    )
    p_reconcile.set_defaults(func=cmd_reconcile)

    p_events = sub.add_parser("events", help="本地事件流（异常分类记录与指纹去重）")
    events_sub = p_events.add_subparsers(dest="events_command", required=True)

    p_events_list = events_sub.add_parser(
        "list", help="列出本地事件（--syncable 只看允许同步学城的）"
    )
    p_events_list.add_argument(
        "--syncable",
        action="store_true",
        help="只列出 adapter_inconsistency / agent_fs_core 类事件",
    )
    p_events_list.add_argument(
        "--since",
        help="只列出 last_seen 不早于该时间的事件（ISO-8601，如 2026-10-01T00:00:00Z）",
    )
    p_events_list.add_argument(
        "--limit",
        type=int,
        help="按 last_seen 倒序取前 N 条（先过滤 --since/--syncable，再截断）",
    )
    p_events_list.set_defaults(func=cmd_events_list)

    p_events_compact = events_sub.add_parser(
        "compact", help="显式压缩事件流（按指纹聚合；并发写入期间可能拒绝执行）"
    )
    p_events_compact.set_defaults(func=cmd_events_compact)

    p_ops = sub.add_parser("ops", help="受控写入操作台账（跨会话恢复证据）")
    ops_sub = p_ops.add_subparsers(dest="ops_command", required=True)
    p_ops_list = ops_sub.add_parser(
        "list", help="列出受控写入记录；--pending 只看「可能已写入但未核实」的目标"
    )
    p_ops_list.add_argument(
        "--pending", action="store_true", help="只列出 status=unknown（结果未核实）的写入"
    )
    p_ops_list.add_argument(
        "--target", default=None, help="按目标路径过滤（匹配调用路径与真实路径别名）"
    )
    p_ops_list.add_argument("--limit", type=int, default=50, help="最多返回条数（默认 50）")
    p_ops_list.set_defaults(func=cmd_ops_list)

    p_guard = sub.add_parser("guard", help="高频 Git 与文本编辑操作的无副作用预检")
    guard_sub = p_guard.add_subparsers(dest="guard_command", required=True)

    p_guard_commit = guard_sub.add_parser(
        "git-commit", help="检查工作区和暂存区的 git diff --check"
    )
    p_guard_commit.add_argument("--cwd", help="Git 仓库目录，默认当前目录")
    p_guard_commit.set_defaults(func=cmd_guard_git_commit)

    p_guard_object = guard_sub.add_parser("git-object", help="确认指定对象是本地存在的 commit")
    p_guard_object.add_argument("commit", help="待操作的 commit SHA 或 revision")
    p_guard_object.add_argument("--cwd", help="Git 仓库目录，默认当前目录")
    p_guard_object.set_defaults(func=cmd_guard_git_object)

    p_guard_pathspec = guard_sub.add_parser("pathspec", help="检查 Git 命令的 pathspec 分隔符")
    p_guard_pathspec.add_argument("command", nargs=argparse.REMAINDER, help="待检查的 Git 命令参数")
    p_guard_pathspec.add_argument("--cwd", help=argparse.SUPPRESS)
    p_guard_pathspec.set_defaults(func=cmd_guard_pathspec)

    p_guard_worktree = guard_sub.add_parser("worktree", help="核验当前 worktree 根目录和分支")
    p_guard_worktree.add_argument("--expected-root", required=True, help="预期 worktree 绝对路径")
    p_guard_worktree.add_argument("--expected-branch", required=True, help="预期分支名")
    p_guard_worktree.add_argument("--cwd", help="Git 仓库目录，默认当前目录")
    p_guard_worktree.set_defaults(func=cmd_guard_worktree)

    p_guard_text = guard_sub.add_parser("text-write", help="检查待写内容的 UTF-8 和替换字符")
    p_guard_text.add_argument("--content-file", required=True, help="待写入内容文件")
    p_guard_text.set_defaults(func=cmd_guard_text_write)

    p_guard_multiedit = guard_sub.add_parser(
        "multiedit", help="预检 MultiEdit 锚点唯一性和编辑区间"
    )
    p_guard_multiedit.add_argument("target", help="待编辑目标文件")
    p_guard_multiedit.add_argument(
        "--edits-file", required=True, help="JSON 编辑列表，元素为 old/new 字符串"
    )
    p_guard_multiedit.set_defaults(func=cmd_guard_multiedit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _JsonArgumentError as e:
        # 参数错误也返回 JSON：调用方（Agent/脚本）不必解析 usage 文本。
        # command/suggested_command 由 _JsonArgumentParser.error 从子解析器
        # prog 剥离（审查 P3：不再出现 "agent-fs agent-fs --help" 重复前缀）。
        _print_json(
            argument_error(
                e.command or "agent-fs",
                None,
                "invalid_arguments",
                f"invalid_arguments: {e.message}",
                suggested_command=e.suggested_command or "agent-fs --help",
                next_action="查看命令帮助后用正确参数重试",
            )
        )
        return EXIT_ERROR
    try:
        reset_result_stage()
        reset_call_context()
        # 基线上下文（命令名/路径）在进入命令函数前登记，命令函数随后补记
        # 已校验的哈希——兜底异常时据此还原统一结果结构（审查 P2-4）。
        record_call_context(
            command=getattr(args, "command", None) or "agent-fs",
            path=(
                str(args.file)
                if getattr(args, "file", None) is not None
                else None
            ),
        )
        return args.func(args)
    except SystemExit:
        # argparse 子解析器内部的 SystemExit（如 --help）正常透传
        raise
    except Exception as e:  # 最后一道防线：任何未预料异常也返回结构化 JSON
        return _record_and_print_error(e)


def _record_and_print_error(e: Exception) -> int:
    """未预料异常：按已到达的执行阶段确定 commit_state（SPEC §6.2）。

    不能在最外层统一提示"重试一次"——提交后（post_commit）异常时替换
    可能已发生，结果未知，只允许进入核实流程。

    写入类命令（write-if-match / edit-if-match）的兜底结果复用统一结果
    结构（WriteOutcome），保留 path 与已校验的 Hbase/Htarget（审查 P2-4：
    旧实现 command 误报 "unknown" 且丢失 path/哈希/reason/durability）。
    读取类命令没有写入契约，维持简表但同样补记调用上下文。
    """
    stage = current_result_stage()
    post_commit = stage == "post_commit"
    reason_code = "unhandled_post_commit" if post_commit else "unhandled_pre_commit"
    reason = f"{reason_code}: {e.__class__.__name__}: {e}"
    ctx = current_call_context()
    try:
        record_event(
            AnomalyEvent(
                component="cli.unhandled",
                operation=ctx.get("command") or "unknown",
                reason_code=reason_code,
                normalized_error=normalize_error(reason),
                category="agent_fs_core",
                detail=str(e)[:300],
                file_state="unknown/committed" if post_commit else "not_committed",
                recovery_action="reconcile" if post_commit else "verify_then_rewrite",
            )
        )
    except Exception:
        pass  # 事件记录失败不掩盖原始错误

    command = ctx.get("command") or "unknown"
    path = ctx.get("path")
    if command in (COMMAND_WRITE_IF_MATCH, COMMAND_EDIT_IF_MATCH) and path:
        detail = str(e)[:300]
        if post_commit:
            outcome = unknown_outcome(
                command,
                path,
                reason_code,
                commit_state=COMMIT_COMMITTED,
                expected_sha256=ctx.get("expected_sha256"),
                desired_sha256=ctx.get("desired_sha256"),
                previous_sha256=ctx.get("expected_sha256"),
                detail=detail,
            )
        else:
            outcome = error_outcome(
                command,
                path,
                reason_code,
                expected_sha256=ctx.get("expected_sha256"),
                desired_sha256=ctx.get("desired_sha256"),
                detail=detail,
            )
        record_operation(outcome)
        payload = outcome.to_dict()
        # 旧键保留（与 argument_error 同策略）：仍读取 error/category 的消费方不破坏
        payload["error"] = reason
        payload["category"] = "agent_fs_core"
        _print_json(payload)
        return EXIT_UNKNOWN if post_commit else EXIT_ERROR

    _print_json(
        {
            "schema_version": SCHEMA_VERSION,
            "command": command,
            "path": path,
            "status": "unknown" if post_commit else "error",
            "commit_state": "committed" if post_commit else "not_committed",
            "reason_code": reason_code,
            "reason": reason,
            "error": reason,
            "category": "agent_fs_core",
            "retryable": False,
            "durability": "not_applicable",
            "next_action": (
                # 已替换但结果未知：只进行只读核实，绝不自动重试写入
                "结果未知：只进行只读核实（agent-fs reconcile），不自动重试写入"
                if post_commit
                else "命令在提交前异常终止：修正原因后重新校验基线；再次写入前先核实文件状态"
            ),
        }
    )
    return EXIT_UNKNOWN if post_commit else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
