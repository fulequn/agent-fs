"""受控原子写入：write-if-match（schema-2 结果契约）。

背景（TOCTOU 问题）：单独的 `verify` 命令只能证明"检查那一刻"文件哈希匹配预期，
命令退出之后、调用方用另一个工具真正写入之前，仍存在一个时间窗口，其他进程可以在
这个窗口里修改文件。`write_if_match` 把「校验 + 替换」收敛到本进程的单次操作中，尽量压缩这个窗口：
1. 获取绑定目标绝对路径的协作锁，锁覆盖"基线校验→临时文件→复核→替换→回读"全程。
2. 锁内稳定读取并校验 expected_sha256（Hbase），不匹配即拒绝。
3. 计算最终写入字节并与 desired_sha256（Htarget，调用方声明）比较；
   未声明时由本命令自行计算（此时调用方不具备"响应丢失后仍知道预期结果"的完整恢复条件）。
4. 写同目录临时文件（保留权限）、fsync、替换前再次复核 Hbase、os.replace 原子替换。
5. 替换成功后稳定回读，比较 observed_sha256（Hobserved）与 Htarget，再返回结果。

**提交事实与结果确认分离（2026-09-29 SPEC）**：os.replace 成功即视为提交成立，
此后回读失败/回读不一致/附属清理异常都不回退为 not_committed——回读失败返回
unknown/committed（退出码 3），只允许进入核实流程；绝不自动重写或提示直接重试。

**重要限制（如实说明，不夸大保证范围）**：
替换前复核与 os.replace 之间仍有极短的时间窗口，本函数无法提供跨进程的严格 CAS
保证；协作锁只约束同样调用 agent-fs 的写入者。本函数把窗口从"整条外部命令链"压缩到
"两条系统调用之间"，是显著的风险降低，但不是数学意义上的原子性证明。
"""

from __future__ import annotations

import errno
import hashlib
import os
import sys
import unicodedata
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from agent_fs import result as _result
from agent_fs.fingerprint import (
    StableFingerprintResult,
    compute_stable_fingerprint,
    hash_bytes,
)
from agent_fs.result import (
    COMMAND_WRITE_IF_MATCH,
    COMMIT_COMMITTED,
    DURABILITY_SYNCED,
    DURABILITY_UNCONFIRMED,
    WriteOutcome,
    error_outcome,
    rejected,
    unknown_outcome,
    verified,
)

COMMAND = COMMAND_WRITE_IF_MATCH

# 平台/文件系统明确不支持 fsync 的 errno：降级为 durability=unconfirmed 而不是失败。
# Windows 常不支持对目录句柄 fsync；部分网络挂载不支持文件 fsync。
_UNSUPPORTED_FSYNC_ERRNOS = {
    code
    for code in (
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
        errno.EINVAL,
        errno.EBADF,
    )
    if code is not None
}


def _normalize_lock_key(raw: str) -> str:
    """锁键的路径规范化（只用于派生锁文件名，不改变实际写入路径）。

    - Unicode 统一 NFC：同一路径的不同组合形式（如 macOS 的 é）共享锁；
    - Windows：normcase（大小写 + 分隔符）；
    - macOS：casefold。默认 APFS 大小写不敏感，`Foo.txt` 与 `foo.txt`
      是同一 inode 却会得到不同锁键；在大小写敏感卷上这会产生少量
      "误判 lock_busy"，属于安全侧偏差（多等一次，好过双写覆盖）。
    """
    key = unicodedata.normalize("NFC", raw)
    if os.name == "nt":
        return os.path.normcase(key)
    if sys.platform == "darwin":
        return key.casefold()
    return key


def _lock_root() -> Path:
    return Path(os.environ.get("AGENT_FS_HOME", Path.home() / ".agent-fs")) / "locks"


def _legacy_lock_file_path(path: Path) -> Path:
    """旧版锁键（仅 resolve，不做大小写/NFC 规范化），用于滚动升级兼容。"""
    key = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return _lock_root() / f"{key}.lock"


def _lock_file_path(path: Path) -> Path:
    """返回当前用户范围内、与目标绝对路径绑定的协作锁文件（规范化锁键）。"""
    key = hashlib.sha256(
        _normalize_lock_key(str(path.resolve())).encode("utf-8")
    ).hexdigest()
    return _lock_root() / f"{key}.lock"


def _lock_file_paths(path: Path) -> list[Path]:
    """新旧两把锁：新键避免大小写别名各持一把锁，旧键兼容升级中的旧进程。

    旧版本只获取旧键；新版本同时获取两把（排序后获取，避免不同路径的
    获取顺序相反导致死锁）。升级完成后可删除旧键兼容。
    """
    paths = {_legacy_lock_file_path(path), _lock_file_path(path)}
    return sorted(paths, key=str)


@dataclass
class _TargetLock:
    """一次取锁结果：可能包含新旧两把锁文件（通常只有一把）。"""

    files: tuple[BinaryIO, ...]

    def close(self) -> None:
        """兼容旧的 BinaryIO 风格调用：释放全部锁。"""
        _release_target_lock(self)

    def __iter__(self):
        return iter(self.files)


def _open_lock_file(lock_path: Path) -> BinaryIO:
    """以 O_RDWR 打开锁文件并确保长度为 1 字节（Windows 按字节加锁）。

    不使用 `open("a+b")`：O_APPEND 会让 `seek(0); write` 仍然追加到 EOF，
    Windows 上每次取锁都会让锁文件增长。创建即 0600，避免"先 0644 后
    chmod"的权限窗口。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(lock_path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        if os.fstat(fd).st_size == 0:
            os.ftruncate(fd, 1)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"0")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "r+b")


def _acquire_one_lock(lock_path: Path) -> BinaryIO | None:
    lock_file = _open_lock_file(lock_path)
    try:
        if os.name == "nt":
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if getattr(error, "winerror", None) in {33, 36} or error.errno in {11, 13}:
                    lock_file.close()
                    return None
                raise
        else:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                return None
    except BaseException:
        lock_file.close()
        raise
    return lock_file


def _acquire_target_lock(path: Path) -> _TargetLock | None:
    """获取目标文件协作锁；返回 None 表示已有协作者持锁。

    新旧锁键同时获取：任一被占用即释放已获取的锁并返回 None。
    """
    acquired: list[BinaryIO] = []
    try:
        for lock_path in _lock_file_paths(path):
            lock_file = _acquire_one_lock(lock_path)
            if lock_file is None:
                for held in reversed(acquired):
                    try:
                        _unlock_and_close(held)
                    except OSError:
                        pass
                return None
            acquired.append(lock_file)
    except BaseException:
        for held in reversed(acquired):
            try:
                _unlock_and_close(held)
            except OSError:
                pass
        raise
    return _TargetLock(tuple(acquired))


def _unlock_and_close(lock_file: BinaryIO) -> None:
    try:
        if os.name == "nt":
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _release_target_lock(lock: _TargetLock | BinaryIO) -> None:
    """释放并关闭协作锁，不删除锁文件以避免删除-重建竞态。

    释放多把锁时即使其中一把失败也继续释放其余锁，最后抛出首个异常，
    调用方将其作为附属失败记录（不能掩盖主操作结果）。
    """
    files = lock.files if isinstance(lock, _TargetLock) else (lock,)
    first_error: OSError | None = None
    for lock_file in reversed(files):
        try:
            _unlock_and_close(lock_file)
        except OSError as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def _classify_fsync_error(error: OSError) -> tuple[str, str]:
    """fsync 失败分类：("unsupported", msg) 可降级；("io", msg) 为真实 I/O 错误。"""
    message = f"{error.__class__.__name__}: {error}"
    if error.errno in _UNSUPPORTED_FSYNC_ERRNOS:
        return ("unsupported", message)
    return ("io", message)


def _fsync_path(target: Path) -> tuple[str, str] | None:
    """对文件或目录执行 fsync；成功返回 None，失败返回 (kind, message)。

    open/fsync/close 任一环的 OSError 都在这里收敛为分类结果，不向外抛——
    尤其是替换后目录同步的 close 失败：异常逃逸会让 CLI 局部捕获误判为
    "未提交"（实际已替换），必须按 durability 降级表达（审查 P1-1）。
    """
    try:
        fd = os.open(target, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as error:
        return _classify_fsync_error(error)
    return None


def _read_original_mode(path: Path) -> int:
    """读取原文件普通权限位（含可执行位），供临时文件 chmod 保留。

    只取低 9 位（0o777）：setuid/setgid/sticky 不属于"内容副本"该继承的
    语义，沿用它们会把提权/粘滞语义带到替换后的新内容上。

    独立成函数是权限读取故障注入的唯一入口：测试 patch 本函数即可模拟
    stat 失败（审查 P2-2）。读取失败由调用方阻止提交——替换后的文件将
    以临时文件的 0600 权限落地，丢失/改变原权限（如可执行位）。
    """
    return path.stat().st_mode & 0o777


def _stable_readback(path: Path) -> StableFingerprintResult:
    """替换后的稳定回读（stat-hash-stat 三明治）。

    独立成函数是回读故障注入的唯一入口：测试 patch 本函数即可模拟
    回读抛 PermissionError（A07）或回读到外部改写内容（A08）。
    """
    try:
        return compute_stable_fingerprint(path)
    except OSError as error:
        # compute_stable_fingerprint 只捕获 FileNotFoundError；权限不足等
        # OSError 在这里收敛为"无法取得稳定观察"，与上层契约对齐。
        return StableFingerprintResult(
            stable=False,
            fingerprint=None,
            attempts=1,
            reason=f"{error.__class__.__name__}: {error}",
        )


def _attach(outcome: WriteOutcome, ancillary: list[str]) -> WriteOutcome:
    """把附属失败（清理/解锁等）附加到结果上，不改变 status/commit_state。"""
    if not ancillary:
        return outcome
    return replace(outcome, ancillary_failures=tuple(ancillary))


def write_if_match(
    path: Path,
    content: bytes,
    expected_sha256: str,
    desired_sha256: str | None = None,
) -> WriteOutcome:
    """获取协作锁后执行一次受控写入。

    desired_sha256（Htarget）为调用方对本次修改完成后的预期字节哈希声明；
    传入时与实际准备出的字节强校验，不传时由本命令自行计算。
    """
    try:
        lock_file = _acquire_target_lock(path)
    except OSError as error:
        return error_outcome(
            COMMAND,
            str(path),
            "lock_failed",
            expected_sha256=expected_sha256,
            desired_sha256=desired_sha256,
            detail=f"{error.__class__.__name__}: {error}",
        )
    if lock_file is None:
        return rejected(
            COMMAND,
            str(path),
            "lock_busy",
            expected_sha256=expected_sha256,
            desired_sha256=desired_sha256,
        )
    try:
        outcome = _write_if_match_unlocked(path, content, expected_sha256, desired_sha256)
    finally:
        release_error = None
        try:
            _release_target_lock(lock_file)
        except OSError as error:
            # 解锁失败不能掩盖已确定的结果，只作为附属诊断附加（A16）
            release_error = f"lock_release_failed: {error.__class__.__name__}: {error}"
    if release_error is not None:
        outcome = _attach(outcome, [release_error])
    return outcome


def _write_if_match_unlocked(
    path: Path,
    content: bytes,
    expected_sha256: str,
    desired_sha256: str | None = None,
) -> WriteOutcome:
    """无锁内部入口：仅当目标当前哈希等于 expected_sha256 时才替换为 content。

    供 write_if_match（持锁后）与 edit_if_match（锁内复用，flock 同进程可重入）
    调用；临时文件清理失败等附属异常附加在结果的 ancillary_failures 上。
    """
    # 文件指纹为小写十六进制；入参大小写归一化后再比较（审查 P2-6）
    expected_sha256 = expected_sha256.lower()
    if desired_sha256 is not None:
        desired_sha256 = desired_sha256.lower()
    ancillary: list[str] = []
    temp_holder: list[Path] = []
    try:
        outcome = _write_with_temp(
            path, content, expected_sha256, desired_sha256, temp_holder
        )
    finally:
        # 正常路径下临时文件已被 replace 消费；任何提前返回分支都需清理残留。
        # 清理失败不能掩盖已发生的替换或拒绝结论，只作为附属诊断。
        for tmp_path in dict.fromkeys(temp_holder):
            try:
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
            except OSError as error:
                ancillary.append(f"tmp_cleanup_failed: {error.__class__.__name__}: {error}")
    return _attach(outcome, ancillary)


def _write_with_temp(
    path: Path,
    content: bytes,
    expected_sha256: str,
    desired_sha256: str | None,
    temp_holder: list[Path],
) -> WriteOutcome:
    """主流程：存在性检查→基线校验→Htarget 校验→临时文件→复核→替换→稳定回读。"""
    if not path.exists():
        return error_outcome(
            COMMAND, str(path), "file_not_found",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
        )
    if not path.is_file():
        return error_outcome(
            COMMAND, str(path), "not_a_regular_file",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
        )
    # 最终路径是符号链接：默认拒绝。os.replace 会替换链接本身（而非目标），
    # 调用方几乎总是想改"链接指向的真实文件"——静默替换链接会制造断链。
    # 需要操作物理路径时，调用方应先 resolve() 再传入物理路径（显式选择）。
    if path.is_symlink():
        return error_outcome(
            COMMAND, str(path), "final_path_is_symlink",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
        )

    # 基线校验使用稳定指纹（stat-hash-stat 三明治）：哈希采集期间被并发修改
    # 的内容不允许作为"未变化"结论依据（SPEC §5 / 审查 P1-2）。
    try:
        baseline = compute_stable_fingerprint(path)
    except OSError as e:
        # compute_stable_fingerprint 只捕获 FileNotFoundError；权限不足等
        # 系统级异常在这里转化为结构化结果，不让 CLI 以堆栈跟踪形式崩溃。
        return error_outcome(
            COMMAND, str(path), "read_failed",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            detail=f"{e.__class__.__name__}: {e}",
        )
    if not baseline.stable or baseline.fingerprint is None:
        if baseline.reason == "file_not_found":
            return error_outcome(
                COMMAND, str(path), "file_not_found",
                expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            )
        return rejected(
            COMMAND, str(path), "target_unstable",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            detail=f"stable_read_reason={baseline.reason}; stage=baseline",
        )
    current_fp = baseline.fingerprint

    if current_fp.sha256 != expected_sha256:
        return rejected(
            COMMAND, str(path), "hash_mismatch",
            expected_sha256=expected_sha256, desired_sha256=desired_sha256,
            previous_sha256=current_fp.sha256,
        )

    # Htarget：调用方显式声明时校验"实际准备字节 == 声明值"（材料漂移即停止，
    # SPEC §5.1 prepared_content_changed）；未声明时由本命令自行计算。
    computed_target = hash_bytes(content)
    if desired_sha256 is None:
        desired = computed_target
    else:
        desired = desired_sha256
        if computed_target != desired:
            return rejected(
                COMMAND, str(path), "prepared_content_changed",
                expected_sha256=expected_sha256, desired_sha256=desired,
                detail=f"material_sha256={computed_target} != declared desired_sha256={desired}",
            )

    durability = DURABILITY_SYNCED
    durability_details: list[str] = []
    try:
        # 元数据保留：替换前记录原文件权限（含可执行位）。临时文件先以受限
        # 权限 0600 创建再写入——若原文件是 0600 而替换用默认 umask 权限
        # （如 0644），会扩大文件访问范围（审查 P1-3）。
        try:
            original_mode = _read_original_mode(path)
        except OSError as error:
            # 无法读取原权限即阻止提交：继续替换会让文件以临时文件的 0600
            # 权限落地，丢失/改变原权限（如可执行位）却返回 verified（审查
            # P2-2）。目标原文保留，调用方排除环境问题后重试。
            return error_outcome(
                COMMAND, str(path), "mode_read_failed",
                expected_sha256=expected_sha256, desired_sha256=desired,
                previous_sha256=current_fp.sha256,
                detail=(
                    f"cannot read original file mode before replace: "
                    f"{error.__class__.__name__}: {error}"
                ),
            )
        # 临时文件必须独占创建（O_EXCL）：共享目录中预置同名文件/符号链接
        # 时不能截断或跟随链接写入。名字碰撞时换随机名重试，不依赖随机性
        # 作为安全边界；创建、写入、fchmod、fsync 全程使用同一个 fd。
        tmp_path = None
        fd = -1
        for _ in range(8):
            candidate = path.parent / f".{path.name}.agentfs-tmp-{uuid.uuid4().hex[:8]}"
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            tmp_path = candidate
            temp_holder.append(candidate)
            break
        if tmp_path is None:
            return error_outcome(
                COMMAND, str(path), "write_temp_file_failed",
                expected_sha256=expected_sha256, desired_sha256=desired,
                previous_sha256=current_fp.sha256,
                detail="cannot create exclusive temp file after 8 attempts",
            )
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fchmod(handle.fileno(), original_mode)
            except (AttributeError, NotImplementedError):
                # Windows 无 fchmod：退回按路径 chmod（此时临时文件由本进程
                # 独占创建，不存在被替换的风险）。
                try:
                    os.chmod(tmp_path, original_mode)
                except OSError as error:
                    return error_outcome(
                        COMMAND, str(path), "tmp_chmod_failed",
                        expected_sha256=expected_sha256, desired_sha256=desired,
                        previous_sha256=current_fp.sha256,
                        detail=(
                            f"cannot preserve mode {oct(original_mode)} on temp file: "
                            f"{error.__class__.__name__}: {error}"
                        ),
                    )
            except OSError as error:
                # 无法保留原权限即阻止提交：无论目标是收紧还是放宽（如丢失
                # 可执行位），替换出一个权限不符的文件都违背调用方预期。
                # 目标原文保留，调用方排除环境问题后重试。
                return error_outcome(
                    COMMAND, str(path), "tmp_chmod_failed",
                    expected_sha256=expected_sha256, desired_sha256=desired,
                    previous_sha256=current_fp.sha256,
                    detail=(
                        f"cannot preserve mode {oct(original_mode)} on temp file: "
                        f"{error.__class__.__name__}: {error}"
                    ),
                )
            # 数据落盘：fsync 临时文件，保证崩溃后要么旧文件完整、要么新文件完整。
            try:
                os.fsync(handle.fileno())
            except OSError as error:
                kind, message = _classify_fsync_error(error)
                if kind == "unsupported":
                    # 平台不支持：降级并声明，不虚构掉电保证（SPEC §6.3）
                    durability = DURABILITY_UNCONFIRMED
                    durability_details.append(f"tmp_fsync_unsupported: {message}")
                else:
                    # 提交前真实同步 I/O 错误：阻止替换，目标原文保留（A17）
                    return error_outcome(
                        COMMAND, str(path), "sync_io_error_before_commit",
                        expected_sha256=expected_sha256, desired_sha256=desired,
                        previous_sha256=current_fp.sha256, detail=message,
                    )
    except OSError as e:
        # 磁盘满、目录无写权限等系统错误：结构化结果，目标原文保留（A06）
        return error_outcome(
            COMMAND, str(path), "write_temp_file_failed",
            expected_sha256=expected_sha256, desired_sha256=desired,
            previous_sha256=current_fp.sha256,
            detail=f"{e.__class__.__name__}: {e}",
        )

    # 落盘前最后一次复核：同样使用稳定指纹，把"计算完新内容到真正替换"之间
    # 的窗口压缩到最小（审查 P1-2：弱保证单次读会漏掉哈希采集期间的并发修改）。
    try:
        recheck = compute_stable_fingerprint(path)
    except OSError as e:
        return error_outcome(
            COMMAND, str(path), "recheck_failed",
            expected_sha256=expected_sha256, desired_sha256=desired,
            detail=f"{e.__class__.__name__}: {e}",
        )
    if not recheck.stable or recheck.fingerprint is None:
        if recheck.reason == "file_not_found":
            return error_outcome(
                COMMAND, str(path), "file_disappeared_before_replace",
                expected_sha256=expected_sha256, desired_sha256=desired,
            )
        return rejected(
            COMMAND, str(path), "target_unstable",
            expected_sha256=expected_sha256, desired_sha256=desired,
            detail=f"stable_read_reason={recheck.reason}; stage=recheck",
        )
    recheck_fp = recheck.fingerprint

    if recheck_fp.sha256 != expected_sha256:
        return rejected(
            COMMAND, str(path), "hash_mismatch_at_replace_time",
            expected_sha256=expected_sha256, desired_sha256=desired,
            previous_sha256=recheck_fp.sha256,
        )

    try:
        os.replace(tmp_path, path)
    except OSError as e:
        return error_outcome(
            COMMAND, str(path), "replace_failed",
            expected_sha256=expected_sha256, desired_sha256=desired,
            previous_sha256=recheck_fp.sha256,
            detail=f"{e.__class__.__name__}: {e}",
        )

    # 提交事实成立：此后任何异常都不回退为 not_committed（SPEC §6.1），
    # main() 兜底据此把未预料异常判为 unknown/committed。
    _result.set_stage("post_commit")

    # 目录 fsync：让替换这一目录项变更也落盘。发生在提交之后——
    # 失败不回滚、不重写，只降级 durability 并声明原因。
    sync_error = _fsync_path(path.parent)
    if sync_error is not None:
        kind, message = sync_error
        durability = DURABILITY_UNCONFIRMED
        durability_details.append(f"dir_fsync_{kind}: {message}")

    durability_detail = "; ".join(durability_details) or None
    readback = _stable_readback(path)
    if readback.stable and readback.fingerprint is not None:
        observed = readback.fingerprint.sha256
        if observed == desired:
            return verified(
                COMMAND, str(path),
                desired_sha256=desired, observed_sha256=observed,
                expected_sha256=expected_sha256, previous_sha256=expected_sha256,
                durability=durability, detail=durability_detail,
            )
        # 回读哈希 ≠ Htarget：外部写入者已在替换后改写文件（A08）。
        # 如实报告 unknown 与实际观察值，绝不把外部内容包装成本次预期成果。
        return unknown_outcome(
            COMMAND, str(path), "observed_mismatch",
            commit_state=COMMIT_COMMITTED,
            expected_sha256=expected_sha256, desired_sha256=desired,
            observed_sha256=observed, previous_sha256=expected_sha256,
            durability=durability, detail=durability_detail,
        )

    # 无法取得稳定观察（A07 回读失败 / 回读反复不稳定）：
    # 替换已执行，返回 unknown/committed，只进入核实流程，不提示重试。
    if readback.reason == "changed_during_read":
        reason_code = "observed_unstable_after_replace"
    else:
        reason_code = "replaced_but_post_check_failed"
    detail = f"readback_reason={readback.reason}"
    if durability_detail:
        detail = f"{detail}; {durability_detail}"
    return unknown_outcome(
        COMMAND, str(path), reason_code,
        commit_state=COMMIT_COMMITTED,
        expected_sha256=expected_sha256, desired_sha256=desired,
        observed_sha256=None, previous_sha256=expected_sha256,
        durability=durability, detail=detail,
    )
