"""操作台账：受控写入的跨会话恢复证据（write-if-match / edit-if-match）。

设计动机（2026-10-31 行业调研结论，委托 catpaw-ide/gpt-6.1-sol 复核）：
中断后恢复所需的 Hbase/Htarget 与提交阶段此前只存在于调用方上下文中，
会话结束或上下文压缩后即丢失，新会话无从发现「可能已写入但结果未核实」
的目标。本模块把每次受控写入的最终结果追加到本地 ops.jsonl，供任何后续
会话通过 `agent-fs ops list --pending` 查询，再走 reconcile 只读核实——
把恢复上下文从调用方记忆搬进本地库（RTK recall.db 同款"工具自持久化 +
内容寻址 + 任意会话可取回"模式）。

可靠性契约（2026-10-31 六轮评审后修订）：
- 台账是尽力记录：单次 O_APPEND 原子写入，写入不足（短写）或序列化失败
  视为记录失败（recorded=false），**绝不掩盖或改变主命令的结果与退出码**；
  断电时最后一条记录可能缺失，台账不构成完整的中断操作日志。
- 「检查-截断-追加」在台账锁内执行（锁原语跨平台：POSIX flock / Windows
  msvcrt.locking；非阻塞有限重试，竞争超时返回 lock_busy，非竞争类错误
  返回 lock_failed）：协作写入者之间互斥，无换行残尾的截断不会删除其他
  协作者已成功的记录；非 agent-fs 写入者不参与该锁，仍可能在检查与追加
  之间写入（边界如实声明）；锁原语不可用的残余平台降级为无锁，协作写入
  者之间也可能互相删除已成功记录（如实声明）。
- 短写/中断留下的无换行残尾在锁内被截断丢弃（残片已判定为记录失败，
  不复活、不吞后续记录）。
- 读取端逐行隔离一切解析失败（编码、JSON 语法、超长数值等），坏行跳过
  不影响其余记录；无换行末段整体跳过。
- 路径身份：记录侧保存调用路径字面形式（含相对调用串）与 realpath 真实
  路径；查询侧身份集合 = {调用串字面归一, cwd 锚定归一（相对路径）, 自身
  归一（绝对路径）, realpath 归一}——**绝不先折叠 `..` 再解析**（保留
  符号链接与 `..` 的原始解析顺序），与行侧 {调用路径, 真实路径} 集合求
  交。归一 = NFC + 平台大小写策略（darwin casefold / Windows normcase /
  其余平台保持原样）。相对调用串跨 cwd 的同名路径视为同一调用身份，
  建议调用方传绝对路径。
- pending 语义：按记录时真实路径的归一化身份（缺失时回退调用路径字面
  身份）取最新一条受控写入，最新行为 status=unknown 才算待恢复；同目标
  后续任何已确定结论（verified/rejected/error）的写入自然消解旧 unknown；
  realpath 变化（软链改指）视为不同目标，原调用路径仍可检索历史记录。
  reconcile 不写台账，核实结论以当次 reconcile 输出为准。
"""

from __future__ import annotations

import errno
import json
import os
import stat as stat_module
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .result import STATUS_PREPARED, STATUS_UNKNOWN

OPS_SCHEMA = 1
DEFAULT_LIMIT = 50
WRITE_COMMANDS = ("write-if-match", "edit-if-match")
_TRUNC_WINDOW = 65536  # 残尾回扫窗口（字节），不足时逐级前扩
_LOCK_RETRY_TIMES = 10  # 锁非阻塞重试次数
_LOCK_RETRY_INTERVAL = 0.1  # 重试间隔（秒）
_BUSY_ERRNOS = frozenset(
    code
    for code in (
        errno.EACCES,
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    )
    if code is not None
)

try:  # POSIX：flock
    import fcntl
except ImportError:  # pragma: no cover - 平台差异（Windows）
    fcntl = None

try:  # Windows：msvcrt.locking 字节区间锁
    import msvcrt
except ImportError:  # pragma: no cover - 平台差异（POSIX）
    msvcrt = None


class LedgerReadError(OSError):
    """台账存在但不可读取（如权限）——与「台账不存在=空」严格区分，
    避免把恢复证据不可读取误报为「没有待恢复操作」（评审 P2）。"""


def _now_iso() -> str:
    """UTC ISO 时间（秒级精度，+00:00 后缀），与 events.jsonl 同一约定。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ops_path() -> Path:
    root = Path(os.environ.get("AGENT_FS_HOME", Path.home() / ".agent-fs"))
    return root / "ops.jsonl"


def _norm_identity(raw: str) -> str:
    """路径身份归一：NFC + 平台大小写策略（只用于匹配与分组）。"""
    key = unicodedata.normalize("NFC", raw)
    if os.name == "nt":
        return os.path.normcase(key)
    if sys.platform == "darwin":
        return key.casefold()
    return key  # 大小写敏感平台不做大小写归一，避免混合不同文件


def _safe_norm(raw: Any) -> str | None:
    """身份归一的安全包装：非字符串或归一失败返回 None。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return _norm_identity(raw)
    except Exception:  # pragma: no cover - 归一失败按无身份处理
        return None


def _target_identity(path: str) -> str | None:
    """解析目标的真实路径（符号链接/别名归一）；失败时降级为 None。

    注意：realpath 合并符号链接与 darwin 大小写别名，但**不合并硬链接
    别名**（不同路径指向同一 inode 仍是不同 identity）——该残差见
    SKILL.md「锁域身份边界」与 tests/test_lock_identity.py。
    """
    try:
        return os.path.realpath(path)
    except OSError:
        return None


def _row_identity_set(row: dict[str, Any]) -> set[str]:
    """行侧身份集合：{调用路径字面, 记录时真实路径} 的归一形式。"""
    identities: set[str] = set()
    for key in ("path", "path_real"):
        normalized = _safe_norm(row.get(key))
        if normalized:
            identities.add(normalized)
    return identities


def _query_variants(target: str) -> set[str]:
    """查询侧身份集合：与行侧 _row_identity_set 求交集判定命中。

    - 调用串字面归一（覆盖相对调用串的记录）；
    - 相对路径：join(cwd, target) **不折叠 `..`**，取「锚定归一」与
      「realpath 归一」（realpath 按遇到顺序展开软链后再处理 `..`）；
    - 绝对路径：不做 abspath（同上理由），取「自身归一」与「realpath
      归一」——`sym/../f` 与 `f` 是不同文件，不得互为别名。
    """
    variants: set[str] = set()
    literal = _safe_norm(target)
    if literal:
        variants.add(literal)
    if os.path.isabs(target):
        anchored = [target]
    else:
        try:
            anchored = [os.path.join(os.getcwd(), target)]
        except OSError:  # pragma: no cover - cwd 不可用时降级为字面
            anchored = [os.path.normpath(target)]
    for candidate in anchored:
        n = _safe_norm(candidate)
        if n:
            variants.add(n)
        real = _target_identity(candidate)
        nr = _safe_norm(real) if real is not None else None
        if nr:
            variants.add(nr)
    return variants


def _offset_after_last_newline(fd: int, size: int) -> int:
    """从尾部按窗口倒序找最后一个换行，返回其后偏移；整个文件无换行返回 0。"""
    window = _TRUNC_WINDOW
    pos = size
    while pos > 0:
        start = max(0, pos - window)
        os.lseek(fd, start, os.SEEK_SET)
        chunk = os.read(fd, pos - start)
        i = chunk.rfind(b"\n")
        if i != -1:
            return start + i + 1
        pos = start
    return 0


def _open_ledger(ops_path: Path) -> tuple[int, str | None]:
    """打开台账文件，返回 (fd, None) 或 (0, 拒绝原因)。

    - O_NOFOLLOW 可用：O_RDWR|O_CREAT|O_APPEND|O_NOFOLLOW 一次打开，
      打开后 fstat 复核必须为普通文件（预置符号链接直接拒绝）；
    - O_NOFOLLOW 不可用的平台**不静默降级**：先 lstat 预检（已存在则必须
      为普通文件且非软链，否则拒绝），存在文件以无 O_CREAT 方式打开并
      fstat 复核；不存在文件以 O_CREAT|O_EXCL 独占创建。lstat 与 open
      之间的替换窗口在该平台仍存在（如实声明，见 SKILL.md）。
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    # O_BINARY（Windows）：文本模式会把 LF 转 CRLF，使缓冲区索引与物理
    # 偏移错位，截断计算将破坏已成功记录（评审 P1）。
    binary = getattr(os, "O_BINARY", 0)
    base = os.O_RDWR | os.O_APPEND | binary
    if nofollow:
        try:
            fd = os.open(ops_path, base | os.O_CREAT | nofollow, 0o600)
        except OSError as error:  # ELOOP = 命中符号链接（O_NOFOLLOW 拒绝）
            return 0, f"unsafe_target: {error.__class__.__name__}"
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            os.close(fd)
            return 0, "unsafe_target: not_regular_file"
        return fd, None
    try:
        st = os.lstat(ops_path)
    except FileNotFoundError:
        try:
            fd = os.open(ops_path, base | os.O_CREAT | os.O_EXCL, 0o600)
            return fd, None
        except FileExistsError:  # 竞态：他人先创建，按存在分支重查
            st = os.lstat(ops_path)
    if stat_module.S_ISLNK(st.st_mode) or not stat_module.S_ISREG(st.st_mode):
        return 0, "unsafe_target: not_regular_file"
    fd = os.open(ops_path, base)
    st2 = os.fstat(fd)
    if not stat_module.S_ISREG(st2.st_mode):
        os.close(fd)
        return 0, "unsafe_target: not_regular_file"
    return fd, None


def _lock_ledger_fd(fd: int) -> str | None:
    """对台账 fd 加协作锁（非阻塞有限重试）。返回 None 或失败原因。

    - POSIX：fcntl.flock（竞争抛 BlockingIOError/EACCES/EAGAIN → 重试）；
    - Windows：msvcrt.locking 字节区间锁（竞争 errno EACCES/EDEADLOCK →
      重试）；锁区间为文件首字节（可超 EOF，所有写入者锁同一区域）；
    - 非竞争类错误返回 lock_failed（不误报竞争）；两类锁原语都缺失的
      残余平台返回 None（无锁降级，SKILL.md 已声明对应并发窗口）。
    """
    if fcntl is not None:
        for _ in range(_LOCK_RETRY_TIMES):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return None
            except BlockingIOError:
                time.sleep(_LOCK_RETRY_INTERVAL)
            except OSError as error:
                if error.errno in _BUSY_ERRNOS:
                    time.sleep(_LOCK_RETRY_INTERVAL)
                    continue
                return f"lock_failed: {error.__class__.__name__}"
        return "lock_busy: 台账被其他写入者占用"
    if msvcrt is not None:
        for _ in range(_LOCK_RETRY_TIMES):
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return None
            except OSError as error:
                if error.errno in _BUSY_ERRNOS:
                    time.sleep(_LOCK_RETRY_INTERVAL)
                    continue
                return f"lock_failed: {error.__class__.__name__}"
        return "lock_busy: 台账被其他写入者占用"
    return None  # 无锁原语的残余平台：降级为无锁（契约已声明）


def _append_isolated(ops_path: Path, line: bytes) -> dict[str, Any]:
    """台账锁内的「检查-截断-追加」。失败只返回原因，不抛出。

    - 打开（O_NOFOLLOW / 降级预检，见 _open_ledger）；
    - 锁内把无换行残尾截断丢弃（残片已判定为记录失败，不复活、不吞后续
      记录），再单次 os.write 完整一行，返回字节数不足按短写失败处理；
    - fsync 在 close 前且仅完整写入时执行。
    """
    fd, failure = _open_ledger(ops_path)
    if failure is not None:
        return {"recorded": False, "reason": failure}
    try:
        lock_failure = _lock_ledger_fd(fd)
        if lock_failure is not None:
            return {"recorded": False, "reason": lock_failure}
        st = os.fstat(fd)
        if st.st_size > 0:
            keep = _offset_after_last_newline(fd, st.st_size)
            if keep != st.st_size:  # 存在无换行残尾：锁内截断丢弃（尽力记录）
                os.ftruncate(fd, keep)
        os.lseek(fd, 0, os.SEEK_END)
        written = os.write(fd, line)
        if written != len(line):
            return {
                "recorded": False,
                "reason": f"short_write: {written}/{len(line)} 字节",
            }
        try:
            os.fsync(fd)
        except OSError:
            pass  # fsync 失败不改变记录结论；durability 由主结果自带
    finally:
        os.close(fd)  # close 释放锁（POSIX flock / Windows 字节区间锁均适用）
    return {"recorded": True, "ops_path": str(ops_path)}


def record_operation(outcome: Any) -> dict[str, Any]:
    """把一次受控写入的最终结果追加到台账（尽力记录，绝不掩盖主结果）。

    - 只记录 write-if-match / edit-if-match 的真实执行；dry-run 产生的
      ``prepared`` 结果不落台账（未触碰目标，不构成恢复证据）。
    - 整个函数体异常隔离：任何台账失败只降低恢复证据的完整性，不改变
      主命令的结果、输出与退出码。
    """
    try:
        command = getattr(outcome, "command", "")
        if command not in WRITE_COMMANDS:
            return {"recorded": False, "reason": "command_not_a_controlled_write"}
        if getattr(outcome, "status", "") == STATUS_PREPARED:
            return {"recorded": False, "reason": "prepared_dry_run_not_recorded"}

        path = getattr(outcome, "path", "") or ""
        entry: dict[str, Any] = {
            "schema_version": OPS_SCHEMA,
            "ts": _now_iso(),
            "command": command,
            "path": path,
            "path_real": _target_identity(path),
            "status": getattr(outcome, "status", None),
            "commit_state": getattr(outcome, "commit_state", None),
            "reason_code": getattr(outcome, "reason_code", None),
            "expected_sha256": getattr(outcome, "expected_sha256", None),
            "desired_sha256": getattr(outcome, "desired_sha256", None),
            "observed_sha256": getattr(outcome, "observed_sha256", None),
            "previous_sha256": getattr(outcome, "previous_sha256", None),
            "durability": getattr(outcome, "durability", None),
        }
        if command == "edit-if-match":
            entry["applied_edits"] = getattr(outcome, "applied_edits", 0)
            entry["edits_file_sha256"] = getattr(outcome, "edits_file_sha256", None)

        # ensure_ascii=True：非 ASCII 与 lone surrogate 一律转义，编码恒安全
        line = (json.dumps(entry) + "\n").encode("ascii")

        ops_path = _ops_path()
        ops_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(ops_path.parent, 0o700)
        except OSError:
            pass
        result = _append_isolated(ops_path, line)
        if result.get("recorded"):
            return {"recorded": True, "ops_path": str(ops_path)}
        return result
    except Exception as error:  # 台账失败绝不掩盖主结果
        try:
            reason = f"record_failed: {error.__class__.__name__}: {error}"[:200]
        except Exception:  # 连错误字符串化都可能再抛（自定义 __str__）
            reason = f"record_failed: {error.__class__.__name__}"
        return {"recorded": False, "reason": reason}


def load_operations(ops_file: Path | None = None) -> list[dict[str, Any]]:
    """读取全部台账行（**文件行序**，不按时间戳排序）；逐行隔离坏行。

    - 文件末尾无换行符的末段视为中断/短写残迹，**整体跳过**（含「完整
      JSON 但缺换行」的情形——残片已判定为记录失败，不作为可靠记录，
      下次追加前也会被截断丢弃）；
    - 逐行解析的任何异常（编码、JSON 语法、超长数值等）只跳过该行；
    - 最小字段校验：command/status 必须为字符串；path 若存在必须为
      字符串（非法则整行跳过），path_real 非字符串时降级为 None。
    """
    path = ops_file if ops_file is not None else _ops_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []  # 台账尚不存在 = 确实没有任何操作记录
    except OSError as error:
        raise LedgerReadError(
            f"ledger_unreadable: {error.__class__.__name__}: {error}"
        ) from error
    torn_tail = not raw.endswith(b"\n")
    if not torn_tail:
        raw = raw[:-1]
    segments = raw.split(b"\n")
    if torn_tail and segments:
        segments = segments[:-1]  # 无换行末段 = 残迹，跳过
    rows: list[dict[str, Any]] = []
    for segment in segments:
        if not segment:
            continue
        try:
            row = json.loads(segment.decode("utf-8"))
        except Exception:  # 编码/语法/超长数值等一切解析失败：仅跳过该行
            continue
        if not isinstance(row, dict):
            continue
        if not isinstance(row.get("command"), str) or not isinstance(
            row.get("status"), str
        ):
            continue
        row_path = row.get("path")
        if row_path is not None and not isinstance(row_path, str):
            continue  # 身份字段非法的记录不可信，整行跳过
        row_real = row.get("path_real")
        if row_real is not None and not isinstance(row_real, str):
            row["path_real"] = None  # 降级：分组退回 path
        rows.append(row)
    return rows


def list_operations(
    pending: bool = False,
    target: str | None = None,
    limit: int | None = DEFAULT_LIMIT,
    ops_file: Path | None = None,
) -> dict[str, Any]:
    """汇总台账：默认最新在前的历史流水；pending 为「目标最新一条未知」。

    - --target：行侧身份集合 {调用路径字面, 记录时真实路径} 与查询侧身份
      集合（调用串字面归一参与匹配，相对路径以 cwd 锚定且**不折叠 `..`**，
      绝对路径保留 `..` 语义）求交集；先过滤再派生 pending 集合，保证
      pending_count 与 operations 口径一致；
    - pending 按记录时真实路径的归一化身份（缺失回退调用路径字面身份）
      取最新一条，最新行为 status=unknown 才算待恢复；同目标后续任何
      已确定结论（verified/rejected/error）的写入自然消解旧 unknown；
      realpath 变化（软链改指）视为不同目标，原调用路径仍可检索历史；
      无有效身份的行不参与 pending；
    - limit 语义：None → 默认 50；0 → 不返回条目；负数由 CLI 拒绝，
      函数层按 0 处理。
    """
    try:
        rows = load_operations(ops_file)
    except LedgerReadError as error:
        # 恢复证据不可读取 ≠ 没有待恢复操作：以 error 结果显式上报
        return {
            "schema_version": OPS_SCHEMA,
            "command": "ops",
            "status": "error",
            "reason_code": "ledger_unreadable",
            "reason": str(error),
            "total": 0,
            "shown": 0,
            "pending_count": None,
            "operations": [],
            "next_action": "先核实台账文件权限/可用性（恢复证据当前不可读取），不要据此得出「无待恢复操作」的结论",
        }
    entries: list[tuple[int, set[str], dict[str, Any]]] = []
    for index, r in enumerate(rows):
        if r.get("command") not in WRITE_COMMANDS:
            continue
        entries.append((index, _row_identity_set(r), r))

    if target is not None:
        variants = _query_variants(target)
        entries = [e for e in entries if e[1] and e[1] & variants]

    # 默认视图：全量历史（按文件顺序倒序 = 最新在前）
    history = [r for _, _, r in reversed(entries)]

    # pending 视图：按归一化身份（真实路径优先，调用路径字面回退）取最新
    # 一条，仅 status=unknown 入选；排序按目标最后出现的行序（不漏最新）。
    last_by_identity: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, identity, r in entries:
        if not identity:
            continue
        grouping_key = (
            _safe_norm(r.get("path_real"))
            or _safe_norm(r.get("path"))
            or ""
        )
        last_by_identity[grouping_key] = (index, r)
    pending_rows = [
        r
        for _, r in sorted(last_by_identity.values(), key=lambda e: e[0], reverse=True)
        if r.get("status") == STATUS_UNKNOWN
    ]

    selected = pending_rows if pending else history
    total = len(selected)
    if limit is None:
        limit = DEFAULT_LIMIT
    shown = selected[:limit] if limit > 0 else []
    return {
        "schema_version": OPS_SCHEMA,
        "command": "ops",
        "total": total,
        "shown": len(shown),
        "pending_count": len(pending_rows),
        "operations": shown,
        "next_action": (
            "对 pending 目标执行 agent-fs reconcile（只读核实），不要直接重试写入"
            if pending_rows
            else None
        ),
    }
