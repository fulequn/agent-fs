"""统一写入结果契约（schema_version=2）：write / edit / reconcile 共用。

背景（2026-09-29 可靠编辑 SPEC 第一期）：旧结果只有 written/edited + reason
字符串 + 退出码 0/1/2，无法区分"确定没写入"与"已替换但结果未知"——
替换后回读失败会返回 written=true、new_sha256=null 并提示重试，调用方据此
重放修改会覆盖或重复应用。本模块把结果收敛为单一事实源：

- status / commit_state / 退出码的判定与映射（SPEC §6.1）
- 三个哈希字段：expected_sha256(Hbase) / desired_sha256(Htarget) /
  observed_sha256(Hobserved) 分别保存、分别比较（SPEC §4.1）
- 兼容字段（written/edited/new_sha256/previous_sha256）的统一映射（SPEC §6.2）
- reason_code → 事件分类的统一登记（events.classify 委托本表，避免两处漂移）

退出码 3（EXIT_UNKNOWN）是新增的安全语义：已替换但结果不符/不可读，或不能
判定是否提交。它只允许进入核实流程（reconcile），不允许直接重放写入。
CLI、Skill 与测试都应消费 status/commit_state，而不是只看退出码或 written。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = 2

EXIT_OK = 0
EXIT_NEGATIVE = 1
EXIT_ERROR = 2
EXIT_UNKNOWN = 3  # 结果未知：只进入核实流程，不自动重试写入

# 写入类命令（write-if-match / edit-if-match）的 status 集合。
# prepared 仅用于 edit-if-match --dry-run；reconcile 使用自己的状态集合。
STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"
STATUS_PREPARED = "prepared"

COMMIT_COMMITTED = "committed"
COMMIT_NOT_COMMITTED = "not_committed"

# 命令身份（写入类命令的结果契约；读取类命令维持各自契约）
COMMAND_WRITE_IF_MATCH = "write-if-match"
COMMAND_EDIT_IF_MATCH = "edit-if-match"
COMMAND_RECONCILE = "reconcile"

# durability：内容验证与掉电持久化分别表达（SPEC §6.3）
DURABILITY_SYNCED = "synced"
DURABILITY_UNCONFIRMED = "unconfirmed"
DURABILITY_NOT_APPLICABLE = "not_applicable"

# status → 退出码（write/edit 主映射；reconcile 在本模块内另有对应行）
_STATUS_EXIT = {
    STATUS_VERIFIED: EXIT_OK,
    STATUS_PREPARED: EXIT_OK,
    STATUS_REJECTED: EXIT_NEGATIVE,
    STATUS_ERROR: EXIT_ERROR,
    STATUS_UNKNOWN: EXIT_UNKNOWN,
}


def reason_to_exit(status: str) -> int:
    """按 status 返回退出码；未知 status 视为程序错误。"""
    return _STATUS_EXIT.get(status, EXIT_ERROR)


# ---------------------------------------------------------------------------
# 执行阶段跟踪：main() 兜底异常按"提交前/提交后"分流判定 commit_state
# （SPEC §6.2：未知异常按已到达阶段确定 commit_state，不能统一提示重试）。

_stage = threading.local()


def reset_stage() -> None:
    """每次 CLI 命令开始前重置（单进程单命令模型；thread-local 隔离测试并发）。"""
    _stage.value = "pre_commit"


def set_stage(stage: str) -> None:
    _stage.value = stage


def current_stage() -> str:
    return getattr(_stage, "value", "pre_commit")


# ---------------------------------------------------------------------------
# 调用上下文跟踪：main() 兜底异常时保留调用方信息（命令名/路径/已校验哈希），
# 使兜底结果与正常路径同为统一结构（审查 P2-4：兜底不再丢失 path/哈希/reason/
# durability，command 也不再误报 "unknown"）。哈希在命令层校验通过后逐步补记。

_context = threading.local()


def reset_call_context() -> None:
    """每次 CLI 命令开始前重置（与 reset_stage 同一时机调用）。"""
    _context.value = {}


def record_call_context(**updates) -> None:
    ctx = getattr(_context, "value", None)
    if ctx is None:
        ctx = {}
        _context.value = ctx
    ctx.update(updates)


def current_call_context() -> dict[str, Any]:
    return dict(getattr(_context, "value", None) or {})


# ---------------------------------------------------------------------------
# reason_code 统一映射表：(status, commit_state, exit_code, event_category)
# event_category 为 None 表示不上报事件（成功结论/正常保护之外的正常结论）。
# 事件分类的唯一实现入口；events.classify() 委托本表。

# status / commit_state / exit / category 的四元组缩写。
# 注意：这是模块级赋值表达式，会在运行时求值——不能用 PEP 604 的 `str | None`
# （需 Python 3.10）。PEP 585 的内建泛型 tuple[...] 在 3.9 运行时合法，
# Optional 仍取自 typing，整体保持 requires-python >=3.9 兼容（审查 C-7）。
_R = tuple[str, str, int, Optional[str]]

REASON_MAP: dict[str, _R] = {
    # --- 成功结论 ---
    "replaced": (STATUS_VERIFIED, COMMIT_COMMITTED, EXIT_OK, None),
    "edits_applied": (STATUS_VERIFIED, COMMIT_COMMITTED, EXIT_OK, None),
    "prepared": (STATUS_PREPARED, COMMIT_NOT_COMMITTED, EXIT_OK, None),
    # --- 门禁正常阻断（rejected / exit 1 / expected_block）---
    "hash_mismatch": (STATUS_REJECTED, COMMIT_NOT_COMMITTED, EXIT_NEGATIVE, "expected_block"),
    "hash_mismatch_at_replace_time": (
        STATUS_REJECTED, COMMIT_NOT_COMMITTED, EXIT_NEGATIVE, "expected_block",
    ),
    "lock_busy": (STATUS_REJECTED, COMMIT_NOT_COMMITTED, EXIT_NEGATIVE, "expected_block"),
    "anchor_not_found": (STATUS_REJECTED, COMMIT_NOT_COMMITTED, EXIT_NEGATIVE, "expected_block"),
    "anchor_not_unique": (STATUS_REJECTED, COMMIT_NOT_COMMITTED, EXIT_NEGATIVE, "expected_block"),
    "post_check_failed": (STATUS_REJECTED, COMMIT_NOT_COMMITTED, EXIT_NEGATIVE, "expected_block"),
    "target_unstable": (STATUS_REJECTED, COMMIT_NOT_COMMITTED, EXIT_NEGATIVE, "expected_block"),
    # 实际准备出的字节与调用方声明的 Htarget 不一致：材料漂移，须重新准备
    "prepared_content_changed": (
        STATUS_REJECTED, COMMIT_NOT_COMMITTED, EXIT_NEGATIVE, "expected_block",
    ),
    # --- 调用方错误（error / exit 2 / caller_error）---
    "file_not_found": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "not_a_regular_file": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "final_path_is_symlink": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "invalid_edits_file": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "empty_anchor": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "old_and_new_identical": (
        STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error",
    ),
    "invalid_sha256_format": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "missing_hash": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "invalid_arguments": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "snapshot_not_found": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "snapshot_path_mismatch": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "snapshot_corrupted": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "invalid_max_bytes": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "invalid_offset_or_limit": (
        STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error",
    ),
    "output_path_conflict": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "output_write_failed": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    "material_unreadable": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "caller_error"),
    # --- 可确定发生在提交前的 I/O / 本体错误（error / exit 2）---
    "write_temp_file_failed": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
    "read_failed": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
    "recheck_failed": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
    "file_disappeared_before_replace": (
        STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core",
    ),
    "replace_failed": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
    "lock_failed": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
    # 提交前真实同步 I/O 错误：阻止替换（SPEC A17）
    "sync_io_error_before_commit": (
        STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core",
    ),
    # 无法在临时文件上保留原文件权限：阻止提交，避免替换扩大/丢失权限（审查 P1-3）
    "tmp_chmod_failed": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
    # 读取原文件权限位失败：阻止提交，避免替换后以临时文件的 0600 权限落地
    # （审查 P2-2：0755 丢执行位却返回 verified）
    "mode_read_failed": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
    "unhandled_pre_commit": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
    # --- 已替换但结果不符 / 不可核实（unknown / exit 3）---
    "replaced_but_post_check_failed": (
        STATUS_UNKNOWN, COMMIT_COMMITTED, EXIT_UNKNOWN, "adapter_inconsistency",
    ),
    "observed_unstable_after_replace": (
        STATUS_UNKNOWN, COMMIT_COMMITTED, EXIT_UNKNOWN, "adapter_inconsistency",
    ),
    # 替换后观察哈希 ≠ Htarget（外部写入者干扰）：如实报告 unknown
    "observed_mismatch": (STATUS_UNKNOWN, COMMIT_COMMITTED, EXIT_UNKNOWN, "expected_block"),
    "unhandled_post_commit": (STATUS_UNKNOWN, COMMIT_COMMITTED, EXIT_UNKNOWN, "agent_fs_core"),
    # 旧码保留映射（历史事件流兼容）
    "unhandled_exception": (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core"),
}


def reason_meta(reason_code: str) -> _R:
    """查 reason_code 四元组；未登记码按提交前错误兜底（调用方仍应登记新码）。"""
    return REASON_MAP.get(
        reason_code, (STATUS_ERROR, COMMIT_NOT_COMMITTED, EXIT_ERROR, "agent_fs_core")
    )


# reason 的默认人类可读文本（reason_code 是稳定枚举，reason 是给人看的说明）
REASON_TEXT: dict[str, str] = {
    "replaced": "受控替换已执行，稳定回读与预期结果一致",
    "edits_applied": "多点编辑已应用，稳定回读与预期结果一致",
    "prepared": "预览成功：可构造该结果，但目标文件未修改",
    "hash_mismatch": "当前文件哈希与基线不一致：协作者可能已修改",
    "hash_mismatch_at_replace_time": "替换前复核发现基线已漂移",
    "lock_busy": "其他协作者持有目标文件的协作锁",
    "anchor_not_found": "锚点在最新内容中不存在",
    "anchor_not_unique": "锚点在最新内容中出现多次",
    "post_check_failed": "编辑应用校验未通过",
    "target_unstable": "锁内稳定读取失败",
    "prepared_content_changed": "实际准备的内容与声明的预期结果哈希不一致",
    "file_not_found": "目标文件不存在",
    "not_a_regular_file": "目标不是普通文件",
    "final_path_is_symlink": "最终路径是符号链接，默认拒绝替换链接本身",
    "invalid_edits_file": "编辑列表格式非法",
    "empty_anchor": "编辑的 old 为空：空锚点会匹配任意位置",
    "old_and_new_identical": "编辑的 old 与 new 相同，不会产生任何变化",
    "invalid_sha256_format": "哈希格式非法",
    "missing_hash": "缺少基线哈希参数",
    "invalid_arguments": "命令参数非法",
    "snapshot_not_found": "快照不存在",
    "snapshot_path_mismatch": "快照与目标文件不匹配",
    "snapshot_corrupted": "快照记录损坏",
    "invalid_max_bytes": "max_bytes 参数非法",
    "invalid_offset_or_limit": "offset/limit 参数非法",
    "output_path_conflict": "--output 与输入文件是同一文件",
    "output_write_failed": "输出文件写入失败",
    "material_unreadable": "修改材料文件不可读",
    "write_temp_file_failed": "写入临时文件失败",
    "read_failed": "读取目标文件失败",
    "recheck_failed": "替换前复核失败",
    "file_disappeared_before_replace": "目标文件在替换前消失",
    "replace_failed": "原子替换失败",
    "lock_failed": "协作锁操作异常",
    "sync_io_error_before_commit": "提交前同步 I/O 错误，已阻止替换",
    "tmp_chmod_failed": "临时文件无法保留原文件权限，已阻止提交",
    "mode_read_failed": "无法读取原文件权限位，已阻止提交",
    "unhandled_pre_commit": "命令在提交前异常终止",
    "replaced_but_post_check_failed": "替换已执行但结果核实失败",
    "observed_unstable_after_replace": "替换后稳定回读失败",
    "observed_mismatch": "替换后当前内容与预期结果不一致",
    "unhandled_post_commit": "命令在提交后异常终止，结果未知",
    "unhandled_exception": "命令异常终止",
}


def reason_text(reason_code: str) -> str:
    return REASON_TEXT.get(reason_code, reason_code)


# next_action 默认文本（调用方可覆盖；稳定分支判断请用 status/reason_code）
NEXT_ACTION: dict[str, str] = {
    "replaced": "写入完成且结果已核实：继续内容验收（语法/业务/测试）；哈希一致不能代替内容验收",
    "edits_applied": "编辑已应用且结果已核实：继续内容验收",
    "prepared": "预览仅表示可构造该结果：确认后用同一材料执行实际写入，并传 --expected-result-sha256",
    "hash_mismatch": "read-fresh 重读最新内容，基于新基线重新准备修改",
    "hash_mismatch_at_replace_time": "read-fresh 重读最新内容，基于新基线重新准备修改",
    "lock_busy": "确认持锁者是否仍在工作；不要盲目重试",
    "anchor_not_found": "read-fresh 重读后重建锚点",
    "anchor_not_unique": "调整锚点使其唯一，或拆分编辑",
    "post_check_failed": "检查编辑列表是否与最新内容匹配",
    "target_unstable": "稍后重试，或检查是否有进程正在写该文件",
    "prepared_content_changed": "重新准备修改材料，使其与预期结果哈希一致后再写入",
    "file_not_found": "检查路径拼写；不要隐式新建文件",
    "not_a_regular_file": "目标是目录/设备文件：检查路径",
    "final_path_is_symlink": "resolve() 后传入物理路径",
    "invalid_edits_file": '检查 edits JSON 格式：[{"old": "..", "new": ".."}]',
    "empty_anchor": "为每条 edit 提供非空 old 文本",
    "old_and_new_identical": "删除 old 与 new 相同的无意义编辑",
    "invalid_sha256_format": "检查哈希格式（64 位十六进制）",
    "missing_hash": "提供 --expected-sha256 / --sha256 / --snapshot 之一",
    "invalid_arguments": "查看命令帮助后用正确参数重试",
    "snapshot_not_found": "先执行 snapshot，再用返回的 snapshot_id",
    "snapshot_path_mismatch": "使用属于该文件的 snapshot，或改用 --expected-sha256",
    "snapshot_corrupted": "重新执行 snapshot 采集",
    "invalid_max_bytes": "用非负字节值重试",
    "invalid_offset_or_limit": "用非负整数 offset/limit 重试",
    "output_path_conflict": (
        "改用不同的输出路径；需要原地改写请用 write-if-match/edit-if-match"
    ),
    "output_write_failed": "检查输出目录权限与磁盘空间后重试",
    "material_unreadable": "检查修改材料文件的路径与权限",
    "write_temp_file_failed": "检查磁盘空间与目录权限；重试前重新校验基线",
    "read_failed": "检查文件权限；重试前重新校验基线",
    "recheck_failed": "重新核实文件当前状态后再决定是否写入",
    "file_disappeared_before_replace": "核实文件去向；不要自动重建",
    "replace_failed": "目标内容未变；重新校验基线后重试",
    "lock_failed": "确认无残留锁后重试",
    "sync_io_error_before_commit": "排除存储问题后，重新校验基线再做受控写入",
    "tmp_chmod_failed": "检查文件系统对权限位的支持后重试；重试前重新校验基线",
    "mode_read_failed": "检查目标文件可达性与权限后重试；重试前重新校验基线",
    "unhandled_pre_commit": "修正原因后重新校验基线；再次写入前先核实文件状态",
    "replaced_but_post_check_failed": (
        "只进入核实流程（agent-fs reconcile），不自动重试写入"
    ),
    "observed_unstable_after_replace": (
        "只进行只读核实（agent-fs reconcile），不自动重试写入"
    ),
    "observed_mismatch": (
        "预期结果当前不存在：agent-fs reconcile 核实后分析差异，重新准备修改"
    ),
    "unhandled_post_commit": (
        "结果未知：只进行只读核实（agent-fs reconcile），不自动重试写入"
    ),
    "unhandled_exception": "带该 JSON 输出上报；再次执行前先核实文件状态",
}


def next_action_text(reason_code: str) -> str:
    return NEXT_ACTION.get(reason_code, "根据 status 与 reason_code 决定后续动作")


@dataclass(frozen=True)
class WriteOutcome:
    """write-if-match / edit-if-match 的统一结果（schema_version=2）。

    字段语义（SPEC §6.2）：
    - status/commit_state 是主判定；written/edited 只是兼容字段。
    - expected_sha256=Hbase、desired_sha256=Htarget、observed_sha256=Hobserved，
      三者独立保存：观察值从不冒充预期值。
    - previous_sha256 是本次操作观察到的写前值，冲突时不冒充输入基线。
    """

    command: str
    path: str
    status: str
    commit_state: str
    reason_code: str
    expected_sha256: str | None = None
    desired_sha256: str | None = None
    observed_sha256: str | None = None
    previous_sha256: str | None = None
    reason: str = ""
    next_action: str = ""
    retryable: bool = False
    durability: str = DURABILITY_NOT_APPLICABLE
    detail: str | None = None
    applied_edits: int = 0
    edits_file: str | None = None
    edits_file_sha256: str | None = None
    ancillary_failures: tuple[str, ...] = field(default_factory=tuple)
    observed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "command": self.command,
            "path": self.path,
            "status": self.status,
            "commit_state": self.commit_state,
            "reason_code": self.reason_code,
            "reason": self.reason or reason_text(self.reason_code),
            "expected_sha256": self.expected_sha256,
            "desired_sha256": self.desired_sha256,
            "observed_sha256": self.observed_sha256,
            "previous_sha256": self.previous_sha256,
            "next_action": self.next_action or next_action_text(self.reason_code),
            "retryable": self.retryable,
            "durability": self.durability,
        }
        # 兼容字段（SPEC §6.2）：written/edited 仅在本次替换确定执行时为 true；
        # new_sha256 仅 verified 时填 Htarget，实际观察值一律在 observed_sha256。
        committed = self.commit_state == COMMIT_COMMITTED
        data["written"] = committed
        if self.command == "edit-if-match":
            data["edited"] = committed and self.status != STATUS_PREPARED
            data["applied_edits"] = self.applied_edits
            data["edits_file"] = self.edits_file
            data["edits_file_sha256"] = self.edits_file_sha256
        data["new_sha256"] = (
            self.desired_sha256 if self.status == STATUS_VERIFIED else None
        )
        if self.detail is not None:
            data["detail"] = self.detail
        if self.ancillary_failures:
            data["ancillary_failures"] = list(self.ancillary_failures)
        if self.observed_at is not None:
            data["observed_at"] = self.observed_at
        return data


def verified(
    command: str,
    path: str,
    *,
    desired_sha256: str,
    observed_sha256: str,
    expected_sha256: str | None = None,
    previous_sha256: str | None = None,
    durability: str = DURABILITY_SYNCED,
    detail: str | None = None,
    ancillary_failures: tuple[str, ...] = (),
    observed_at: str | None = None,
) -> WriteOutcome:
    """替换已执行且回读等于 Htarget：verified / committed / exit 0。"""
    return WriteOutcome(
        command=command,
        path=path,
        status=STATUS_VERIFIED,
        commit_state=COMMIT_COMMITTED,
        reason_code="replaced",
        expected_sha256=expected_sha256,
        desired_sha256=desired_sha256,
        observed_sha256=observed_sha256,
        previous_sha256=previous_sha256,
        durability=durability,
        detail=detail,
        ancillary_failures=ancillary_failures,
        observed_at=observed_at,
    )


def rejected(
    command: str,
    path: str,
    reason_code: str,
    *,
    expected_sha256: str | None = None,
    desired_sha256: str | None = None,
    observed_sha256: str | None = None,
    previous_sha256: str | None = None,
    detail: str | None = None,
    next_action: str | None = None,
    applied_edits: int = 0,
    edits_file: str | None = None,
    edits_file_sha256: str | None = None,
) -> WriteOutcome:
    """未替换的拒绝结论（基线冲突/锁忙/锚点拒绝/材料漂移）：rejected / exit 1。"""
    return WriteOutcome(
        command=command,
        path=path,
        status=STATUS_REJECTED,
        commit_state=COMMIT_NOT_COMMITTED,
        reason_code=reason_code,
        expected_sha256=expected_sha256,
        desired_sha256=desired_sha256,
        observed_sha256=observed_sha256,
        previous_sha256=previous_sha256,
        detail=detail,
        next_action=next_action or "",
        applied_edits=applied_edits,
        edits_file=edits_file,
        edits_file_sha256=edits_file_sha256,
    )


def error_outcome(
    command: str,
    path: str,
    reason_code: str,
    *,
    expected_sha256: str | None = None,
    desired_sha256: str | None = None,
    previous_sha256: str | None = None,
    detail: str | None = None,
    next_action: str | None = None,
) -> WriteOutcome:
    """可确定发生在提交前的错误：error / not_committed / exit 2。"""
    return WriteOutcome(
        command=command,
        path=path,
        status=STATUS_ERROR,
        commit_state=COMMIT_NOT_COMMITTED,
        reason_code=reason_code,
        expected_sha256=expected_sha256,
        desired_sha256=desired_sha256,
        previous_sha256=previous_sha256,
        detail=detail,
        next_action=next_action or "",
    )


def unknown_outcome(
    command: str,
    path: str,
    reason_code: str,
    *,
    commit_state: str,
    expected_sha256: str | None = None,
    desired_sha256: str | None = None,
    observed_sha256: str | None = None,
    previous_sha256: str | None = None,
    durability: str = DURABILITY_UNCONFIRMED,
    detail: str | None = None,
    ancillary_failures: tuple[str, ...] = (),
    observed_at: str | None = None,
) -> WriteOutcome:
    """已替换但结果不符/不可读，或不能判定是否提交：unknown / exit 3。

    只允许进入核实流程；绝不回退为 not_committed，也不提供重试建议。
    """
    return WriteOutcome(
        command=command,
        path=path,
        status=STATUS_UNKNOWN,
        commit_state=commit_state,
        reason_code=reason_code,
        expected_sha256=expected_sha256,
        desired_sha256=desired_sha256,
        observed_sha256=observed_sha256,
        previous_sha256=previous_sha256,
        durability=durability,
        detail=detail,
        ancillary_failures=ancillary_failures,
        observed_at=observed_at,
    )


def argument_error(
    command: str,
    path: str | None,
    reason_code: str,
    message: str,
    *,
    suggested_command: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    """调用方参数/输入错误的统一 schema-2 形态（参数错误也输出同一字段结构）。

    必需字段全量输出，无法取得的哈希值明确填 null（审查 P2-5：消费方按统一
    结构解析时不应在错误处理阶段再次失败）。保留 error/category 旧键，
    避免破坏仍读取这两个字段的现有消费方。
    """
    _, commit_state, _, category = reason_meta(reason_code)
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "path": path,
        "status": STATUS_ERROR,
        "commit_state": commit_state,
        "reason_code": reason_code,
        "reason": reason_text(reason_code),
        "expected_sha256": None,
        "desired_sha256": None,
        "observed_sha256": None,
        "previous_sha256": None,
        "error": message,
        "category": category or "caller_error",
        "retryable": False,
        "durability": DURABILITY_NOT_APPLICABLE,
        "next_action": next_action or next_action_text(reason_code),
        "written": False,
    }
    if suggested_command:
        data["suggested_command"] = suggested_command
    return data
