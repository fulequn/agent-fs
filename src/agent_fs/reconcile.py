"""中断后的只读核实：reconcile。

背景（2026-09-29 可靠编辑 SPEC §7）：写入调用超时、断线或响应缺失后，
调用方无法区分"没写入"与"写入结果未知"。`reconcile` 用稳定读取获取当前
内容，仅比较三个哈希（Hbase / Htarget / Hobserved），供调用方在原调用
确认结束后决定：不重复应用、受控恢复，或重新准备修改。

**只读承诺**：本命令不获取协作锁、不写目标文件、不回滚、不重放补丁，
也不根据文件内容推断原调用是否结束——原调用状态必须由调用方从宿主获取
（SPEC §7.2：超时、取消请求已发送、PID 不可查询均不构成结束证据）。
即使核实发现 desired_present，原调用未确认结束前也不得启动依赖该文件
稳定结果的后续写入。

哈希只能判断当前字节：desired_present 不保证历史上只执行过一次；
base_present 不证明原调用从未执行（可能写入后又被恢复）；
diverged 需要读取最新内容分析后重新准备，不能只更换 expected_sha256。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_fs.fingerprint import DEFAULT_STABLE_MAX_RETRIES, compute_stable_fingerprint
from agent_fs.result import (
    COMMAND_RECONCILE,
    SCHEMA_VERSION,
    argument_error,
    next_action_text,
    reason_text,
)

# reconcile 使用自己的状态集合（SPEC §7.1），不与写入命令的 status 混用
STATUS_DESIRED_PRESENT = "desired_present"
STATUS_BASE_PRESENT = "base_present"
STATUS_DIVERGED = "diverged"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"

# observed_status → 退出码
_RECONCILE_EXIT = {
    STATUS_DESIRED_PRESENT: 0,
    STATUS_BASE_PRESENT: 1,
    STATUS_DIVERGED: 1,
    STATUS_UNAVAILABLE: 3,
    STATUS_ERROR: 2,
}

_RECONCILE_NEXT_ACTION = {
    STATUS_DESIRED_PRESENT: "预期结果当前已存在：禁止重复应用；继续内容验收",
    STATUS_BASE_PRESENT: (
        "当前仍是原文（不证明原调用从未执行）：原调用确认结束后，"
        "可保留原 Hbase/Htarget 做受控恢复；核实与写入之间的新修改会被拒绝"
    ),
    STATUS_DIVERGED: (
        "存在其他改动或部分编辑：读取最新完整内容分析差异，"
        "保留他人改动后重新准备修改，不能只更换 expected_sha256"
    ),
    STATUS_UNAVAILABLE: "无法核实：禁止自动创建、覆盖或重试；先排查目标可达性",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _StableFailure:
    """稳定读取失败时的轻量替身（与 StableFingerprintResult 的失败形态对齐）。"""

    def __init__(self, reason: str):
        self.stable = False
        self.fingerprint = None
        self.reason = reason


@dataclass(frozen=True)
class ReconcileOutcome:
    """reconcile 结果（schema_version=2 信封，状态集合独立于写入命令）。"""

    path: str
    observed_status: str
    base_sha256: str | None
    desired_sha256: str | None
    observed_sha256: str | None
    observed_at: str
    reason_code: str
    detail: str | None = None

    @property
    def exit_code(self) -> int:
        return _RECONCILE_EXIT.get(self.observed_status, 2)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "command": COMMAND_RECONCILE,
            "path": self.path,
            "status": self.observed_status,
            "base_sha256": self.base_sha256,
            "desired_sha256": self.desired_sha256,
            "observed_sha256": self.observed_sha256,
            "observed_at": self.observed_at,
            "reason_code": self.reason_code,
            "reason": reason_text(self.reason_code),
            "next_action": _RECONCILE_NEXT_ACTION.get(self.observed_status, ""),
            "retryable": False,
        }
        if self.detail is not None:
            data["detail"] = self.detail
        return data


def reconcile(
    path: Path,
    base_sha256: str,
    desired_sha256: str,
    max_retries: int = DEFAULT_STABLE_MAX_RETRIES,
) -> ReconcileOutcome:
    """只读三哈希比较（SPEC §7.1）。

    Hobserved = Htarget → desired_present（预期结果当前已存在，禁止重复应用；
    Hbase == Htarget 时天然优先，A14）。Hobserved = Hbase → base_present
    （当前仍是原文）。两者均不同 → diverged。无法取得稳定内容（缺失/不可读/
    反复不稳定）→ unavailable，不把"文件不存在"解释成空文件或路径错误。
    """
    # 文件指纹是小写十六进制；库级调用方传大写时先归一化再比较（审查 P2-6）
    base_sha256 = base_sha256.lower()
    desired_sha256 = desired_sha256.lower()
    observed_at = _now_iso()
    try:
        stable = compute_stable_fingerprint(path, max_retries=max_retries)
    except OSError as error:
        # compute_stable_fingerprint 只捕获 FileNotFoundError；权限不足等
        # OSError 在这里收敛为"无法核实"，不向外抛异常（G5：本地可完成核实）。
        stable = _StableFailure(reason=f"{error.__class__.__name__}: {error}")

    if not stable.stable or stable.fingerprint is None:
        reason_code = "unavailable_target_missing" if stable.reason == "file_not_found" else (
            "unavailable_unreadable"
        )
        return ReconcileOutcome(
            path=str(path),
            observed_status=STATUS_UNAVAILABLE,
            base_sha256=base_sha256,
            desired_sha256=desired_sha256,
            observed_sha256=None,
            observed_at=observed_at,
            reason_code=reason_code,
            detail=f"readback_reason={stable.reason}",
        )

    observed = stable.fingerprint.sha256
    if observed == desired_sha256:
        # Hbase == Htarget 时同样落到这里：预期已存在优先于"仍是原文"（A14）
        status = STATUS_DESIRED_PRESENT
        reason_code = "desired_present"
    elif observed == base_sha256:
        status = STATUS_BASE_PRESENT
        reason_code = "base_present"
    else:
        status = STATUS_DIVERGED
        reason_code = "diverged"
    return ReconcileOutcome(
        path=str(path),
        observed_status=status,
        base_sha256=base_sha256,
        desired_sha256=desired_sha256,
        observed_sha256=observed,
        observed_at=observed_at,
        reason_code=reason_code,
    )


def reconcile_argument_error(message: str, suggested_command: str | None = None) -> dict:
    """reconcile 参数错误（哈希非法/缺失）的统一 schema-2 形态。"""
    return argument_error(
        COMMAND_RECONCILE, None, "invalid_sha256_format", message,
        suggested_command=suggested_command or "agent-fs reconcile --help",
        next_action=next_action_text("invalid_sha256_format"),
    )
