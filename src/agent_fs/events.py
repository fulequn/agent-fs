"""agent-fs 事件分类与本地事件流（上报治理内核）。

背景（2026-09-16 重构）：旧协议"所有编辑异常都上报学城、不去重、拿不准也
上报"，在 565 节点/2.9 万字的问题页上累积了 116 条 warning——其中大部分
是门禁正常阻断（expected_block）、调用方参数错误（caller_error）或外部
工具失败（external_failure），并非 agent-fs 本体缺陷。把正常保护行为当
缺陷上报，还让普通任务自动产生学城写入和仓库 CHANGELOG 修改，形成递归
故障与工作树污染。

新协议分五级（SKILL.md「异常上报协议」节的唯一实现）：

- ``expected_block``：门禁正常阻断（verify 不匹配、guard 拒绝、
  write-if-match 的 hash_mismatch/lock_busy 等）。只计指标，不建问题。
- ``caller_error``：调用方参数/路径/转义/cwd 错误。只返回修正提示。
- ``external_failure``：构建、认证、浏览器、网络等外部依赖失败。
  责任边界（2026-09-29 SPEC §9）：网络根因由对应平台/Skill 排障；
  Agent-fs 负责的是故障造成的**文件状态核实与安全恢复**（reconcile），
  两者不互相替代——根因分类为 external_failure 不代表文件状态已确定。
- ``adapter_inconsistency``：编辑工具称成功但哈希未变（或反之）。
  进入待复现队列。
- ``agent_fs_core``：稳定读取、CAS、锁或哈希契约**可复现地**失效。
  只有这一级（及上一级）允许批量同步学城，且同步必须显式触发。

异常根因、文件状态和恢复动作分开记录：AnomalyEvent.file_state 记录
写入结果契约的 commit_state（如 unknown/committed），recovery_action
记录建议的恢复动作（如 reconcile）——三者不互相替代。

事件先写本地 ``~/.agent-fs/events.jsonl``（追加式 JSONL，每行一个事件），
按指纹去重：相同指纹只递增 ``count``/``last_seen``，不新增记录。学城
同步是独立命令 ``agent-fs events sync``（未来实现），不在普通任务中
自动产生外部写入。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# 五级分类（顺序即严重度排序）
CATEGORIES = (
    "expected_block",
    "caller_error",
    "external_failure",
    "adapter_inconsistency",
    "agent_fs_core",
)

# 允许批量同步学城的类别（SKILL.md 上报协议要求）
SYNCABLE_CATEGORIES = frozenset({"adapter_inconsistency", "agent_fs_core"})

# reason_code → 默认类别的映射（覆盖高频场景；未命中由调用方显式指定）
_REASON_CATEGORY_MAP = {
    # 门禁正常阻断 = expected_block
    "hash_mismatch": "expected_block",
    "hash_mismatch_at_replace_time": "expected_block",
    "lock_busy": "expected_block",
    "anchor_not_unique": "expected_block",
    "anchor_not_found": "expected_block",
    # 历史事件码：区间重叠已改为 warnings，仅用于读取旧事件流时保持分类稳定
    "intervals_overlap": "expected_block",
    "trailing_whitespace": "expected_block",
    "match": None,  # verify match:false 属正常结论，不上报（调用方不应传）
    # 调用方错误 = caller_error
    "file_not_found": "caller_error",
    "not_a_regular_file": "caller_error",
    "invalid_sha256_format": "caller_error",
    "invalid_edits_file": "caller_error",
    "invalid_json": "caller_error",
    "empty_anchor": "caller_error",
    "old_and_new_identical": "caller_error",
    "invalid_offset_or_limit": "caller_error",
    "invalid_max_bytes": "caller_error",
    "unknown_command": "caller_error",
    "unknown_argument": "caller_error",
    "snapshot_not_found": "caller_error",
    "binary_or_undecodable": "caller_error",
    # 本体核心失效 = agent_fs_core（可复现时）
    "unstable_after_retries": "agent_fs_core",
    "lock_failed": "agent_fs_core",
    "read_failed": "agent_fs_core",
    "write_temp_file_failed": "agent_fs_core",
    "replace_failed": "agent_fs_core",
    "replaced_but_post_check_failed": "adapter_inconsistency",
    "hash_unchanged_after_claimed_write": "adapter_inconsistency",
}


def classify(reason_code: str) -> str | None:
    """按 reason_code 返回默认类别；未映射的返回 None（调用方显式指定）。

    委托 result.REASON_MAP（写入结果契约的唯一实现）优先，避免两处映射漂移；
    guard 等非写入链路的 reason_code 仍由本模块表兜底。
    """
    from agent_fs.result import reason_meta

    mapped = reason_meta(reason_code)
    # reason_meta 对未登记码兜底返回 agent_fs_core；仅当码确实登记在
    # REASON_MAP 中才采信，否则回退本模块表（guard 系列码不进结果契约）。
    from agent_fs.result import REASON_MAP

    if reason_code in REASON_MAP:
        return mapped[3]
    return _REASON_CATEGORY_MAP.get(reason_code)


def _events_path() -> Path:
    root = Path(os.environ.get("AGENT_FS_HOME", Path.home() / ".agent-fs"))
    return root / "events.jsonl"


def _agent_fs_version() -> str:
    try:
        from agent_fs import __version__

        return __version__
    except Exception:
        return "unknown"


@dataclass
class AnomalyEvent:
    """一条待记录的异常事件（记录前先算指纹，同指纹只累计）。"""

    component: str  # 如 "cli.verify" / "external.apply_patch" / "core.write_if_match"
    operation: str  # 如 "verify" / "multi_edit" / "write"
    reason_code: str  # 稳定枚举（见 _REASON_CATEGORY_MAP 与 result.REASON_MAP）
    normalized_error: str  # 归一化错误摘要（去绝对路径/时间戳/随机数后）
    category: str
    path: str = ""
    detail: str = ""
    file_state: str = ""  # 文件状态（如 "unknown/committed"），与根因分开记录
    recovery_action: str = ""  # 建议恢复动作（如 "reconcile"），与根因分开记录
    platform: str = field(default_factory=lambda: f"{os.name}:{sys_platform()}")
    agent_fs_version: str = field(default_factory=_agent_fs_version)
    count: int = 1
    first_seen: str = ""
    last_seen: str = ""

    def fingerprint(self) -> str:
        """事件指纹：同指纹只递增 count/last_seen，不新增记录。"""
        payload = "|".join(
            (
                self.component,
                self.operation,
                self.reason_code,
                self.normalized_error,
                self.platform,
                self.agent_fs_version,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sys_platform() -> str:
    import sys

    return sys.platform


def normalize_error(raw: str) -> str:
    """归一化错误文本：剥离绝对路径、行号、时间戳、PID 等易变部分。

    指纹去重的关键——同一错误在不同机器/不同时刻的原文不同，归一化后
    才能合并为同一指纹。
    """
    import re

    s = raw
    # 敏感键值对：token/Authorization/password/secret/api_key/cookie 的值
    # 整体替换为 <redacted>。同时覆盖：
    # - `k: v` / `k=v` / `"k": "v"`（JSON 引号键与引号值）；
    # - `Authorization: Bearer <token>`（值本身是两个词，必须连 Bearer 一起消费）；
    # - 未加引号的值截止到空白/逗号/分号/右括号，避免泄露后续字段。
    s = re.sub(
        r"""((?:"|')?(?:token|authorization|password|passwd|secret|api[_-]?key)"""
        r"""(?:"|')?\s*[:=]\s*)"""
        r"""(?:(?P<quote>["'])[^"']*(?P=quote)|(?:bearer\s+)?[^\s,;}\]]+)""",
        r"\1<redacted>",
        s,
        flags=re.IGNORECASE,
    )
    # Cookie 头可能包含多个 `k=v; k=v`：未加引号时消费到行尾/引号/右括号，
    # 不能只脱敏第一个分号前的值。
    s = re.sub(
        r"""((?:"|')?cookie(?:"|')?\s*[:=]\s*)"""
        r"""(?:(?P<quote>["'])[^"']*(?P=quote)|[^\n"'}]+)""",
        r"\1<redacted>",
        s,
        flags=re.IGNORECASE,
    )
    # /abs/path/to/x → <path>/x（保留文件名便于定位）
    s = re.sub(r"(?:/[^\s/]+)+/([^\s/]+)", r"<path>/\1", s)
    # ~/x → <home>/x
    s = re.sub(r"~[^\s]*?/([^\s/]+)", r"<home>/\1", s)
    # Windows 盘符路径 C:\Users\alice\x.py → <path>/x.py
    s = re.sub(r"[A-Za-z]:\\(?:[^\\\s]+\\)*([^\\\s]+)", r"<path>/\1", s)
    # UNC 路径 \\server\share\dir\x.py → <path>/x.py
    s = re.sub(r"\\\\[^\\\s]+\\(?:[^\\\s]+\\)*([^\\\s]+)", r"<path>/\1", s)
    # 行号 line 42: → line N:
    s = re.sub(r"line \d+", "line N", s)
    s = re.sub(r":\d+:", ":N:", s)
    # 十六进制/时间戳/PID 类长数字串
    s = re.sub(r"\b[0-9a-f]{16,}\b", "<hex>", s)
    s = re.sub(r"\b\d{10,13}\b", "<ts>", s)
    # SHA-256 形态的哈希
    s = re.sub(r"\b[0-9a-f]{64}\b", "<sha256>", s)
    return s.strip()[:500]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _events_lock_path(events_file: Path) -> Path:
    """事件文件专属协作锁路径（与 write_atomic 同一套锁目录约定）。"""
    root = Path(os.environ.get("AGENT_FS_HOME", Path.home() / ".agent-fs"))
    key = hashlib.sha256(str(events_file.resolve()).encode("utf-8")).hexdigest()
    return root / "locks" / f"events-{key}.lock"


_EVENT_LINE_LIMIT = 4096


def _encode_event_line(entry: dict[str, Any]) -> str:
    """把事件编码为单行 JSON，并保证字节长度有界。

    追加写依赖"一行一次 write"的原子性；行过长会跨过常见文件系统的
    原子写边界。超限时按固定顺序收缩自由文本字段并标记 ``fields_truncated``，
    不产生半行 JSON。
    """
    line = json.dumps(entry, ensure_ascii=False)
    if len(line.encode("utf-8")) <= _EVENT_LINE_LIMIT:
        return line
    bounded = dict(entry)
    bounded["fields_truncated"] = True
    bounded["detail"] = ""
    bounded["normalized_error"] = str(bounded.get("normalized_error", ""))[:200]
    bounded["path"] = str(bounded.get("path", ""))[:200]
    line = json.dumps(bounded, ensure_ascii=False)
    if len(line.encode("utf-8")) <= _EVENT_LINE_LIMIT:
        return line
    bounded["normalized_error"] = bounded["normalized_error"][:80]
    bounded["path"] = bounded["path"][:80]
    bounded["component"] = str(bounded.get("component", ""))[:64]
    bounded["operation"] = str(bounded.get("operation", ""))[:64]
    return json.dumps(bounded, ensure_ascii=False)


def _write_all(fd: int, payload: bytes) -> None:
    """写满整行；短写时继续写剩余部分（单行长度已限制在 4 KiB 内）。"""
    written = 0
    while written < len(payload):
        chunk = os.write(fd, payload[written:])
        if chunk <= 0:
            raise OSError("short write while appending event")
        written += chunk


def _append_event(path: Path, event: AnomalyEvent) -> dict[str, Any]:
    """唯一写入路径：O_APPEND 追加一行，不读改写、不覆盖任何既有行。

    指纹合并只在读取端 ``load_events`` 做。旧实现"锁内全量读改写 + 锁失败
    追加"两条路径混用时会丢事件（持锁者基于旧快照 replace 覆盖掉刚追加的
    行，却仍返回 recorded=true）。追加式写入在常见本地文件系统上对单行
    （≤4 KiB）是原子的，并发下**最多重复、永不丢失**。
    """
    fp = event.fingerprint()
    entry = {"fingerprint": fp, **event.to_dict()}
    line = (_encode_event_line(entry) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 事件目录含全部事件文件（路径/错误摘要）：收紧 0700
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    fd = -1
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, NotImplementedError, OSError):
            pass
        _write_all(fd, line)
        os.fsync(fd)
    except OSError as error:
        return {
            "recorded": False,
            "fingerprint": fp,
            "merged": False,
            "category": event.category,
            "locked": False,
            "write_mode": "append",
            "degraded": (
                f"append_failed: {error.__class__.__name__}: {error}；事件未落盘"
            ),
        }
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    return {
        "recorded": True,
        "fingerprint": fp,
        "merged": False,  # 去重在读取端；写入端不做合并
        "deduplicated_at_read": True,
        "category": event.category,
        "locked": False,
        "write_mode": "append",
    }


def _with_events_lock(events_file: Path, write_fn) -> Any:
    """对事件文件加排他锁后执行 write_fn（fail-loud）。

    指纹合并是"读改写"三步，必须持锁；无锁下并发读改写会互相覆盖丢数据
    （80 并发实测丢 10 条）。锁不可用（获取失败/锁目录不可写）时**降级为
    追加式写入**——不覆盖既有行，最多重复不丢失，结果带 degraded 标记；
    KeyboardInterrupt/SystemExit 等控制流异常原样上抛，不吞。
    """
    import time

    if os.name == "nt":
        import msvcrt
    else:
        import fcntl

    lock_path = _events_lock_path(events_file)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # 锁目录含事件指纹等派生信息：同样收紧 0700
        try:
            os.chmod(lock_path.parent, 0o700)
        except OSError:
            pass
        lock_file = lock_path.open("a+b")
    except OSError:
        return None  # 调用方走降级路径

    try:
        try:
            lock_path.chmod(0o600)
        except OSError:
            pass
        acquired = False
        # 阻塞获取（带 10s 上限防死等），持锁窗口只覆盖读改写三步
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                if os.name == "nt":
                    lock_file.seek(0)
                    lock_file.write(b"0")
                    lock_file.flush()
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                time.sleep(0.02)
        if not acquired:
            lock_file.close()
            return None  # 超时：调用方走降级路径
        try:
            return write_fn(locked=True)
        finally:
            try:
                if os.name == "nt":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()
    except OSError:
        # 锁实现自身的系统错误（非控制流）：关闭句柄后走降级路径
        try:
            lock_file.close()
        except OSError:
            pass
        return None
    # KeyboardInterrupt / SystemExit / 任何非 OSError 异常原样上抛（不吞）


def record_event(event: AnomalyEvent, events_file: Path | None = None) -> dict[str, Any]:
    """把事件追加写入本地 events.jsonl（append-only，并发安全）。

    写入端不做"读全量 → 合并 → 重写"：那条路径与追加路径混用时会互相
    覆盖并丢事件。同指纹的重复行由 ``load_events`` 在读取端聚合（count
    累加、last_seen 取最大），因此写入结果中的 ``merged`` 恒为 False，
    并以 ``deduplicated_at_read=True`` 表明去重发生在读取端。

    所有自由文本字段（path/detail/normalized_error）在单一入口统一脱敏，
    防止调用方直接构造事件时绕过 ``normalize_error``。
    """
    path = events_file if events_file is not None else _events_path()
    for attr in ("path", "detail", "normalized_error"):
        value = getattr(event, attr)
        if value:
            setattr(event, attr, normalize_error(str(value)))
    now = _now_iso()
    event.first_seen = now
    event.last_seen = now
    return _append_event(path, event)


def compact_events(events_file: Path | None = None) -> dict[str, Any]:
    """显式压缩事件文件：按指纹聚合后原子重写（不在普通任务中自动触发）。

    压缩本身是"读全量 → 重写"，必须与追加写互斥；获取事件文件锁后
    重新读取并按指纹聚合。锁不可用/被占用时明确返回 compacted=false，
    不冒险重写（宁可文件继续增长，也不丢事件）。
    """
    path = events_file if events_file is not None else _events_path()

    def _write(locked: bool) -> dict[str, Any]:
        events = load_events(path)
        tmp = path.with_name(f".{path.name}.compact-{uuid.uuid4().hex[:8]}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return {
            "compacted": True,
            "events": len(events),
            "path": str(path),
            "locked": locked,
            "next_action": "压缩仅合并同指纹行；原始事件内容仍可从聚合视图追溯",
        }

    result = _with_events_lock(path, _write)
    if result is not None:
        return result
    return {
        "compacted": False,
        "events": len(load_events(path)),
        "path": str(path),
        "locked": False,
        "reason": "lock_unavailable_or_busy",
        "next_action": "确认无并发写事件后重试；不要删除事件文件代替压缩",
    }


def sync_eligible(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """筛选允许同步学城的事件（仅 adapter_inconsistency / agent_fs_core）。"""
    return [e for e in events if e.get("category") in SYNCABLE_CATEGORIES]


def load_events(events_file: Path | None = None) -> list[dict[str, Any]]:
    """读取全部事件（跳过损坏行），按指纹聚合。

    锁正常时文件本身已去重；锁降级期间的追加式写入可能产生同指纹
    重复行——读取端按指纹聚合（count 累加、last_seen 取最大），
    保证消费方拿到的是聚合视图。
    """
    path = events_file if events_file is not None else _events_path()
    if not path.exists():
        return []
    by_fp: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        fp = rec.get("fingerprint")
        if fp is None:
            fp = f"<nofp>:{len(order)}"
        if fp in by_fp:
            prev = by_fp[fp]
            prev["count"] = int(prev.get("count", 1)) + int(rec.get("count", 1))
            if rec.get("last_seen", "") > prev.get("last_seen", ""):
                prev["last_seen"] = rec.get("last_seen")
            if rec.get("first_seen", "") < prev.get("first_seen", "￿"):
                prev["first_seen"] = rec.get("first_seen")
        else:
            by_fp[fp] = rec
            order.append(fp)
    return [by_fp[fp] for fp in order]
