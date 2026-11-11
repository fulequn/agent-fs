"""锁域身份测试：符号链接/硬链接/大小写别名是否共享同一把协作锁。

对应 2026-10-31 行业调研的核验点 5（锁域身份统一性）与三路评审的补充要求：
- 符号链接别名：path.resolve() 已归一 → 共享锁（含实际互斥验证）；
- 大小写别名：darwin casefold 归一 → 共享锁（含实际互斥验证）；
- 硬链接别名：resolve() 不合并 inode 别名 → 各持一把锁，为**已知残差**。
  内容哈希 CAS 仍会拒绝「基线已变化」的写入，但两次并发写入之间的
  check-then-replace 窗口不受协作锁保护。测试固化当前行为并作为
  SKILL.md「锁域身份边界」一节的证据。
"""

from __future__ import annotations

import os
import sys

import pytest

from agent_fs.write_atomic import (
    _acquire_target_lock,
    _lock_file_path,
    _release_target_lock,
)

symlink_unsupported = not hasattr(os, "symlink")


@pytest.mark.skipif(symlink_unsupported, reason="平台不支持符号链接")
def test_symlink_alias_shares_same_lock(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("content")
    alias = tmp_path / "alias.md"
    alias.symlink_to(real)
    assert _lock_file_path(real) == _lock_file_path(alias)


@pytest.mark.skipif(symlink_unsupported, reason="平台不支持符号链接")
def test_symlink_alias_lock_is_mutually_exclusive(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("content")
    alias = tmp_path / "alias.md"
    alias.symlink_to(real)
    held = _acquire_target_lock(alias)
    try:
        assert held is not None  # 通过别名取锁成功
        assert _acquire_target_lock(real) is None  # 真实路径视角锁被占用
    finally:
        if held is not None:
            _release_target_lock(held)


@pytest.mark.skipif(sys.platform != "darwin", reason="casefold 归一仅 darwin 分支")
def test_case_alias_shares_same_lock(tmp_path):
    # APFS 大小写不敏感：只有一个磁盘文件，两个大小写路径指同一 inode
    upper = tmp_path / "Mixed.md"
    upper.write_text("content")
    lower = tmp_path / "mixed.md"
    assert _lock_file_path(upper) == _lock_file_path(lower)


@pytest.mark.skipif(sys.platform != "darwin", reason="casefold 归一仅 darwin 分支")
def test_case_alias_lock_is_mutually_exclusive(tmp_path):
    upper = tmp_path / "MixedCase.md"
    upper.write_text("content")
    held = _acquire_target_lock(upper)
    try:
        assert held is not None
        assert _acquire_target_lock(tmp_path / "mixedcase.md") is None
    finally:
        if held is not None:
            _release_target_lock(held)


def test_hardlink_aliases_keep_separate_locks__known_gap(tmp_path):
    """固化已知残差：硬链接别名不共享锁（resolve 不合并 inode 别名）。

    若未来引入 (st_dev, st_ino) 锁键，本用例应改为断言相等。
    """
    a = tmp_path / "a.md"
    a.write_text("content")
    b = tmp_path / "b.md"
    os.link(a, b)
    assert a.stat().st_ino == b.stat().st_ino  # 确为同一 inode
    assert _lock_file_path(a) != _lock_file_path(b)  # 当前行为：锁分离
