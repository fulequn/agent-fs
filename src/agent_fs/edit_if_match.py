"""受控多点编辑：edit-if-match。

背景（2026-09-16 重构）：旧流程是三段式 `guard multiedit → verify →
外部工具 MultiEdit/apply_patch`——verify 与外部写入之间存在 TOCTOU 窗口，
协作者在窗口内修改会导致锚点失配或静默覆盖，是问题页上"verify 成功 →
另一协作者修改 → apply_patch 失败"这一高频模式的根因。

`edit_if_match` 在**同一把协作锁内**完成：

    稳定读取 → 顺序应用全部编辑（edits.py，锚点唯一性对最新内容复查）
    → 结果自校验 → 复核目标哈希未漂移 → 原子替换 → 稳定回读核实

三段式流程降级为兼容模式，主流程收敛为一次命令。锁复用
write_atomic 的 per-path 协作锁；替换语义（mode 保留、目录 fsync、
symlink 拒绝、提交事实与回读分离）同样复用 write_atomic 的实现。
结果为 schema-2 统一契约（result.py）；成功 reason_code 为 edits_applied。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_fs.edits import apply_edits, describe_failure, load_edits
from agent_fs.fingerprint import hash_bytes
from agent_fs.read_fresh import read_fresh
from agent_fs.result import (
    COMMAND_EDIT_IF_MATCH,
    COMMIT_NOT_COMMITTED,
    STATUS_PREPARED,
    STATUS_VERIFIED,
    WriteOutcome,
    error_outcome,
    rejected,
)
from agent_fs.write_atomic import (
    _acquire_target_lock,
    _release_target_lock,
    _write_if_match_unlocked,
)


def _load_material(edits_file: str | Path):
    """读取编辑材料（唯一实现在 edits.py，与 guard 共用同一套校验）。"""
    return load_edits(edits_file)


def _material_error(material) -> tuple[str, str | None]:
    """材料问题的 reason_code 与 detail。

    空锚点 / old==new 是材料语义错误，保留 edits.py 的细分码，便于调用方
    直接定位该改哪条 edit；读不到或 JSON 结构非法仍统一归为
    invalid_edits_file。三者都是 caller_error / exit 2，不改变退出码契约。
    """
    if material.error_code in {"empty_anchor", "old_and_new_identical"}:
        return material.error_code, f"edits[{material.error_index}] {material.error_code}"
    return "invalid_edits_file", None


def prepare_edits(path: Path, edits_file: str | Path, expected_sha256: str) -> WriteOutcome:
    """--dry-run 预览（SPEC §5.1）：只读，不取锁、不修改目标文件。

    复用与实际写入完全相同的 load_edits/apply_edits 变换与锚点校验，
    不建设第二套编辑算法。返回 status=prepared、Hbase、Htarget、编辑数量
    及所依据的 edits 文件哈希；退出码 0 只表示"可构造该结果"，不表示已编辑。
    预览与实际写入之间可能发生其他修改——实际写入会重新校验全部前提，
    不复用预览通过的结论。
    """
    expected_sha256 = expected_sha256.lower()  # 指纹为小写十六进制（审查 P2-6）
    material = _load_material(edits_file)
    edits, edits_sha = material.edits, material.sha256
    if edits is None:
        reason_code, detail = _material_error(material)
        return error_outcome(
            COMMAND_EDIT_IF_MATCH, str(path), reason_code,
            expected_sha256=expected_sha256, detail=detail,
        )

    fresh = read_fresh(path)
    if not fresh.stable or fresh.content is None or fresh.sha256 is None:
        return rejected(
            COMMAND_EDIT_IF_MATCH, str(path), "target_unstable",
            expected_sha256=expected_sha256,
            detail=f"read_fresh_reason={fresh.reason}",
        )
    if fresh.sha256 != expected_sha256:
        return rejected(
            COMMAND_EDIT_IF_MATCH, str(path), "hash_mismatch",
            expected_sha256=expected_sha256, previous_sha256=fresh.sha256,
        )

    application = apply_edits(fresh.content, edits)
    new_content, applied = application.content, application.applied_edits
    if new_content is None:
        return rejected(
            COMMAND_EDIT_IF_MATCH, str(path), application.reason_code,
            expected_sha256=expected_sha256, previous_sha256=fresh.sha256,
            detail=describe_failure(application, edits),
            applied_edits=applied,
        )

    edits_path = Path(edits_file)
    return WriteOutcome(
        command=COMMAND_EDIT_IF_MATCH,
        path=str(path),
        status=STATUS_PREPARED,
        commit_state=COMMIT_NOT_COMMITTED,
        reason_code="prepared",
        expected_sha256=fresh.sha256,
        desired_sha256=hash_bytes(new_content.encode("utf-8")),
        applied_edits=applied,
        edits_file=str(edits_path),
        edits_file_sha256=edits_sha,
    )


def _edit_if_match_locked(
    path: Path,
    edits: list[dict[str, str]],
    edits_sha: str | None,
    edits_file: str | Path,
    expected_sha256: str,
    desired_sha256: str | None,
) -> WriteOutcome:
    """持锁执行编辑主流程（锁的获取/释放由调用方负责）。"""
    # 锁内稳定读取（与 guard_multiedit 相同的新鲜度语义）
    fresh = read_fresh(path)
    if not fresh.stable or fresh.content is None or fresh.sha256 is None:
        return rejected(
            COMMAND_EDIT_IF_MATCH, str(path), "target_unstable",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            detail=f"read_fresh_reason={fresh.reason}",
        )

    # 基线校验：调用方持有的哈希必须就是当前状态，否则协作者已改
    if fresh.sha256 != expected_sha256:
        return rejected(
            COMMAND_EDIT_IF_MATCH, str(path), "hash_mismatch",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            previous_sha256=fresh.sha256,
        )

    # 锁内应用全部编辑（锚点唯一性对最新内容复查）
    application = apply_edits(fresh.content, edits)
    new_content, applied = application.content, application.applied_edits
    if new_content is None:
        return rejected(
            COMMAND_EDIT_IF_MATCH, str(path), application.reason_code,
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            previous_sha256=fresh.sha256,
            detail=describe_failure(application, edits),
            applied_edits=applied,
        )

    # write_if_match 复用同一把锁的路径做替换（锁可重入场景下 flock
    # 同进程不阻塞）；_write_if_match_unlocked 内含 Htarget 校验、
    # 替换前哈希复核、原子替换与稳定回读。
    outcome = _write_if_match_unlocked(
        path,
        new_content.encode("utf-8"),
        expected_sha256,
        desired_sha256,
    )
    # 写入核心以 write-if-match 身份产出结果；这里改写为本命令身份，
    # 成功时 reason_code 用 edits_applied，并携带编辑材料信息
    # （edits_sha 来自与解析同源的那一次读取，审查 P2-4）。
    edits_path = Path(edits_file)
    if outcome.status == STATUS_VERIFIED:
        return replace(
            outcome,
            command=COMMAND_EDIT_IF_MATCH,
            reason_code="edits_applied",
            applied_edits=applied,
            edits_file=str(edits_path),
            edits_file_sha256=edits_sha,
        )
    return replace(
        outcome,
        command=COMMAND_EDIT_IF_MATCH,
        applied_edits=applied,
        edits_file=str(edits_path),
        edits_file_sha256=edits_sha,
    )


def edit_if_match(
    path: Path,
    edits_file: str | Path,
    expected_sha256: str,
    desired_sha256: str | None = None,
    dry_run: bool = False,
) -> WriteOutcome:
    """锁内完成多点受控编辑（主流程命令，替代三段式）。

    desired_sha256 语义与 write-if-match 一致：调用方对编辑完成后的
    预期字节哈希声明；传入时与实际变换结果强校验（材料漂移即停止）。
    dry_run=True 时走只读预览（prepare_edits），声明值被忽略并说明。
    """
    expected_sha256 = expected_sha256.lower()  # 指纹为小写十六进制（审查 P2-6）
    if desired_sha256 is not None:
        desired_sha256 = desired_sha256.lower()
    if dry_run:
        outcome = prepare_edits(path, edits_file, expected_sha256)
        if desired_sha256 is not None and outcome.status == STATUS_PREPARED:
            outcome = replace(
                outcome,
                detail=(
                    "dry-run 忽略 --expected-result-sha256（预览不校验声明值）；"
                    "实际写入时会以该值强校验材料一致性"
                ),
            )
        return outcome
    material = _load_material(edits_file)
    edits, edits_sha = material.edits, material.sha256
    if edits is None:
        reason_code, detail = _material_error(material)
        return error_outcome(
            COMMAND_EDIT_IF_MATCH, str(path), reason_code,
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            detail=detail,
        )

    try:
        lock_file = _acquire_target_lock(path)
    except OSError as error:
        return error_outcome(
            COMMAND_EDIT_IF_MATCH, str(path), "lock_failed",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            detail=f"{error.__class__.__name__}: {error}",
        )
    if lock_file is None:
        return rejected(
            COMMAND_EDIT_IF_MATCH, str(path), "lock_busy",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
        )
    try:
        outcome = _edit_if_match_locked(
            path, edits, edits_sha, edits_file, expected_sha256, desired_sha256
        )
    except BaseException:
        # 主流程异常优先上抛：释放失败的诊断不能掩盖原始异常。
        try:
            _release_target_lock(lock_file)
        except OSError:
            pass
        raise
    try:
        _release_target_lock(lock_file)
    except OSError as error:
        # 释放失败不影响已确定的主结论（写入可能已 verified），只作为
        # 附属失败附加——旧实现会让解锁异常覆盖成功结果，把"已提交"
        # 误报成 unknown/exit 3。
        outcome = replace(
            outcome,
            ancillary_failures=outcome.ancillary_failures
            + (f"lock_release_failed: {error.__class__.__name__}: {error}",),
        )
    return outcome
