"""共享测试设施：AGENT_FS_HOME 隔离。

此前 test_cli.py 与 test_store.py 各自维护一份相同的 autouse fixture，
test_edit_if_match.py 里还有一处手工 os.environ save/restore 样板；统一收敛
到 conftest，避免新增测试文件时忘记隔离而污染真实用户主目录。个别用例
需要在隔离基础上再覆盖 AGENT_FS_HOME（如锁竞争用例），直接在用例内
monkeypatch.setenv 即可——后设置者生效。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_agent_fs_home(tmp_path, monkeypatch):
    """每个测试用例使用独立的 AGENT_FS_HOME，避免污染真实用户主目录，也避免用例间互相影响。"""
    monkeypatch.setenv("AGENT_FS_HOME", str(tmp_path / ".agent-fs"))
    yield
