"""MultiEdit 材料的唯一解析与顺序应用实现。

背景（2026-10 优化 P1-7）：此前 `guard multiedit` 与 `edit-if-match` 各有一套
编辑算法——guard 在**原始内容**上按区间重叠做预检，edit-if-match 在最新内容上
**顺序应用**。两套语义在链式编辑（`a→b` 后 `c→a`）、前缀自包含（`abc` 中
`a→ab`）、重复引入等场景下结论不一致：guard 拒绝的编辑实际可以安全提交，
guard 放行的编辑也可能在实际写入时失配，预检因此失去意义。

本模块把「读取材料 → 校验条目 → 顺序应用 → 结果自校验」收敛为唯一实现，
guard（只读预检）、`edit-if-match --dry-run` 与实际写入全部调用它，保证三者
对同一材料给出同一个结论和同一组诊断（`edit_index` / `occurrences` /
`positions`）。

区间重叠不再是错误：顺序应用下前一个编辑可能改变后一个锚点的位置或出现次数，
这是合法用法。真正会失败的情形（锚点缺失/不唯一/替换无变化）由顺序应用判定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agent_fs.fingerprint import hash_bytes

# 诊断里最多回传多少个锚点出现位置（避免超长锚点产生巨大输出）
_MAX_REPORTED_POSITIONS = 10


@dataclass(frozen=True)
class LoadedEdits:
    """编辑材料的解析结果：解析与哈希来自同一次读取。"""

    edits: list[dict[str, str]] | None
    sha256: str | None
    error_code: str | None = None  # edits_file_unreadable / invalid_edits_file / old_and_new_identical / empty_anchor
    error_index: int | None = None


@dataclass(frozen=True)
class EditApplication:
    """顺序应用的结果与失败诊断（成功时 content 非 None）。"""

    content: str | None
    reason_code: str  # ok / anchor_not_found / anchor_not_unique / post_check_failed
    failed_index: int  # 失败编辑下标；ok 时为 -1
    applied_edits: int
    occurrences: int | None = None
    positions: tuple[int, ...] = ()
    line_numbers: tuple[int, ...] = ()  # 与 positions 一一对应的 1-based 行号

    @property
    def ok(self) -> bool:
        return self.reason_code == "ok"


def load_edits(edits_file: str | Path) -> LoadedEdits:
    """单次读取材料字节，同时解析编辑列表并计算材料哈希。

    解析与哈希必须来自同一份字节：分两次读时材料可能在读取间隙被改写，
    产出「旧材料算出的 Htarget + 新材料的指纹」这类不一致的恢复证据。
    读取失败时哈希为 None；格式非法时仍返回已读取字节的哈希。
    """
    try:
        raw = Path(edits_file).read_bytes()
    except OSError:
        return LoadedEdits(edits=None, sha256=None, error_code="edits_file_unreadable")
    edits_sha = hash_bytes(raw)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return LoadedEdits(edits=None, sha256=edits_sha, error_code="invalid_edits_file")
    if not isinstance(parsed, list):
        return LoadedEdits(edits=None, sha256=edits_sha, error_code="invalid_edits_file")
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            return LoadedEdits(edits=None, sha256=edits_sha, error_code="invalid_edits_file")
        old, new = item.get("old"), item.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            return LoadedEdits(edits=None, sha256=edits_sha, error_code="invalid_edits_file")
        # 空锚点会匹配任意位置（str.count("") 恒 >= 1），顺序应用也无法定位
        if old == "":
            return LoadedEdits(
                edits=None, sha256=edits_sha, error_code="empty_anchor", error_index=index
            )
        if old == new:
            return LoadedEdits(
                edits=None,
                sha256=edits_sha,
                error_code="old_and_new_identical",
                error_index=index,
            )
    return LoadedEdits(edits=parsed, sha256=edits_sha)


def apply_edits(content: str, edits: list[dict[str, str]]) -> EditApplication:
    """按顺序应用编辑；任一锚点失配返回失败诊断。

    锚点唯一性在锁内对最新内容重新校验（guard 预检只能证明「检查时」唯一，
    锁内复查才是安全边界）。顺序应用：第 N 个编辑基于前 N-1 个的结果。
    """
    current = content
    for index, edit in enumerate(edits):
        old, new = edit["old"], edit["new"]
        count = current.count(old)
        positions = _positions(current, old) if count else ()
        if count == 0:
            return EditApplication(
                content=None,
                reason_code="anchor_not_found",
                failed_index=index,
                applied_edits=index,
                occurrences=0,
            )
        if count > 1:
            return EditApplication(
                content=None,
                reason_code="anchor_not_unique",
                failed_index=index,
                applied_edits=index,
                occurrences=count,
                positions=positions,
                line_numbers=_line_numbers(current, positions),
            )
        current = current.replace(old, new, 1)
    # 结果自校验：应用 edit[i] 必须让内容发生**可观测变化**。两种合法形态：
    #   a→b 后 c→a：old "a" 重新出现，但那是 edit[1] 的产物（合法链式）
    #   abc 中 a→ab：替换后 old "a" 仍是 new "ab" 的子串（前缀自包含）
    # 因此校验改为「内容确实变化」：逐编辑重放，每步结果必须 != 上一步。
    replay = content
    for index, edit in enumerate(edits):
        prev = replay
        replay = replay.replace(edit["old"], edit["new"], 1)
        if replay == prev:
            return EditApplication(
                content=None,
                reason_code="post_check_failed",
                failed_index=index,
                applied_edits=index,
            )
    if replay == content:
        return EditApplication(
            content=None,
            reason_code="post_check_failed",
            failed_index=len(edits),
            applied_edits=len(edits),
        )
    return EditApplication(
        content=current,
        reason_code="ok",
        failed_index=-1,
        applied_edits=len(edits),
    )


def _positions(content: str, needle: str) -> tuple[int, ...]:
    """锚点出现位置（最多 _MAX_REPORTED_POSITIONS 个，便于定位歧义）。"""
    found: list[int] = []
    start = 0
    while len(found) < _MAX_REPORTED_POSITIONS:
        index = content.find(needle, start)
        if index < 0:
            break
        found.append(index)
        start = index + max(len(needle), 1)
    return tuple(found)


def _line_numbers(content: str, positions: tuple[int, ...]) -> tuple[int, ...]:
    """把字符偏移换算成 1-based 行号（Agent 通常按行定位，比偏移更直观）。"""
    return tuple(content.count("\n", 0, position) + 1 for position in positions)


def describe_failure(result: EditApplication, edits: list[dict[str, str]]) -> str | None:
    """给锚点/重放失败补充人类可读细节（下标、锚点摘要与出现位置）。"""
    if result.reason_code == "post_check_failed" and result.failed_index >= len(edits):
        return "全部编辑应用后内容无变化"
    if result.failed_index < 0 or result.failed_index >= len(edits):
        return None
    old = edits[result.failed_index]["old"]
    preview = old if len(old) <= 40 else old[:37] + "..."
    if result.reason_code == "anchor_not_found":
        return (
            f"edit[{result.failed_index}] 锚点在最新内容中不存在"
            f"（可能已被协作者修改）: {preview!r}"
        )
    if result.reason_code == "anchor_not_unique":
        where = f"，出现位置 {list(result.positions)}" if result.positions else ""
        lines = f"，所在行 {list(result.line_numbers)}" if result.line_numbers else ""
        return (
            f"edit[{result.failed_index}] 锚点出现 {result.occurrences} 次{where}{lines}: "
            f"{preview!r}"
        )
    if result.reason_code == "post_check_failed":
        return f"edit[{result.failed_index}] 替换未产生变化"
    return None
