"""打包完整性验收测试：确认 SKILL.md 能被正确打进 wheel。

背景：此前 SKILL.md 实体位于包根 tools/agent-fs/SKILL.md，pyproject.toml 的
package-data 声明为 agent_fs/SKILL.md（相对 src/ 的包内路径），但包目录内并没有这个
文件，导致 setuptools 找不到、构建出的 wheel 里缺失 SKILL.md。修复方式是在
src/agent_fs/ 下建立指向 ../../SKILL.md 的符号链接（与 doc-toolkit 的做法一致）。

本测试直接检查符号链接是否存在、指向是否正确、内容读取后是否与包根文件一致，
避免今后重构时又意外破坏这个链接而没有测试兜底。
"""

from __future__ import annotations

import os
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_SKILL_MD_ROOT = _PACKAGE_ROOT / "SKILL.md"
_SKILL_MD_IN_PACKAGE = _PACKAGE_ROOT / "src" / "agent_fs" / "SKILL.md"


def test_skill_md_exists_at_package_root():
    assert _SKILL_MD_ROOT.is_file()


def test_skill_md_symlink_exists_inside_package_dir():
    """setuptools 打包时只会收集包目录（src/agent_fs/）内的文件，SKILL.md 必须
    在包目录内可见（无论是真实文件还是符号链接），否则 package-data 声明不生效。"""
    assert _SKILL_MD_IN_PACKAGE.exists(), (
        "src/agent_fs/SKILL.md 不存在：wheel 构建时不会包含 Skill 说明文件，"
        "会重现历史缺陷（package-data 声明了 agent_fs/SKILL.md 但包目录内实际没有该文件）"
    )


def test_skill_md_in_package_is_symlink_to_root_not_a_duplicate_copy():
    """必须是符号链接而不是复制的独立文件，避免出现两份需要手动同步的内容。"""
    assert _SKILL_MD_IN_PACKAGE.is_symlink(), (
        "src/agent_fs/SKILL.md 应该是指向包根 SKILL.md 的符号链接，"
        "而不是独立复制的文件（否则未来修改包根 SKILL.md 时容易忘记同步这一份）"
    )


def test_skill_md_symlink_content_matches_root_file():
    """通过符号链接读取到的内容应与包根实体文件完全一致。"""
    root_content = _SKILL_MD_ROOT.read_text(encoding="utf-8")
    linked_content = _SKILL_MD_IN_PACKAGE.read_text(encoding="utf-8")
    assert linked_content == root_content


def test_skill_md_symlink_resolves_to_root_file():
    """符号链接最终应精确解析到包根的 SKILL.md，而不是其他位置的同名文件。"""
    resolved = _SKILL_MD_IN_PACKAGE.resolve()
    assert resolved == _SKILL_MD_ROOT.resolve()


def test_pyproject_declares_skill_md_as_package_data():
    """pyproject.toml 必须声明 SKILL.md 为 package-data，否则即便符号链接存在，
    setuptools 默认也不会把非 .py 文件打进 wheel。"""
    pyproject_path = _PACKAGE_ROOT / "pyproject.toml"
    content = pyproject_path.read_text(encoding="utf-8")
    assert "[tool.setuptools.package-data]" in content
    assert "SKILL.md" in content


def test_built_wheel_actually_contains_skill_md(tmp_path):
    """端到端验证：真实构建一次 wheel，解压检查其中是否包含 SKILL.md。

    这是本测试文件中最关键的一条——前几条只验证了"源码层面配置正确"，
    但历史问题恰恰是"源码配置看起来合理，实际构建产物却缺失文件"，
    因此必须真正跑一次构建来兜底。
    """
    import subprocess
    import sys
    import zipfile

    dist_dir = tmp_path / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=str(_PACKAGE_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )

    if result.returncode != 0 and "No module named build" in (result.stderr or ""):
        import pytest

        pytest.skip("未安装 python -m build（pip install build），跳过端到端 wheel 构建验证")

    assert result.returncode == 0, (
        f"wheel 构建失败:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    wheel_files = list(dist_dir.glob("*.whl"))
    assert len(wheel_files) == 1, f"预期恰好构建出 1 个 wheel，实际: {wheel_files}"

    with zipfile.ZipFile(wheel_files[0]) as zf:
        names = zf.namelist()
        skill_md_entries = [n for n in names if n.endswith("SKILL.md")]
        assert skill_md_entries, f"构建出的 wheel 中未找到 SKILL.md，实际内容: {names}"
        # 校验 wheel 内 SKILL.md 内容与源文件一致（符号链接应被解析为真实内容，而非链接本身）
        wheel_skill_md_content = zf.read(skill_md_entries[0]).decode("utf-8")
        assert wheel_skill_md_content == _SKILL_MD_ROOT.read_text(encoding="utf-8")


def test_symlink_is_relative_for_portability():
    """符号链接应使用相对路径（如 ../../SKILL.md），而不是绝对路径，
    否则仓库被克隆到其他机器的不同路径下时链接会失效。"""
    target = os.readlink(_SKILL_MD_IN_PACKAGE)
    assert not os.path.isabs(target), (
        f"src/agent_fs/SKILL.md 的符号链接目标应为相对路径，实际为绝对路径: {target}"
    )


# ─── references/ 按需文档：与 SKILL.md 同一套符号链接方案打进 wheel ───

_REFERENCE_NAMES = (
    "concurrent-editing.md",
    "interruption-recovery.md",
    "result-contract.md",
    "error-taxonomy.md",
)


def test_reference_files_exist_at_package_root():
    for name in _REFERENCE_NAMES:
        assert (_PACKAGE_ROOT / "references" / name).is_file(), f"缺少 references/{name}"


def test_reference_symlinks_exist_inside_package_dir():
    """references 符号链接必须存在于包目录内，否则 package-data 声明不生效。"""
    for name in _REFERENCE_NAMES:
        link = _PACKAGE_ROOT / "src" / "agent_fs" / "references" / name
        assert link.exists(), f"src/agent_fs/references/{name} 不存在，wheel 会缺失该文件"
        assert link.is_symlink(), (
            f"src/agent_fs/references/{name} 应为指向包根 references 的符号链接，"
            "而不是独立复制的文件（两份实体会失同步）"
        )


def test_reference_symlinks_are_relative_and_resolve_to_root():
    for name in _REFERENCE_NAMES:
        link = _PACKAGE_ROOT / "src" / "agent_fs" / "references" / name
        target = os.readlink(link)
        assert not os.path.isabs(target), f"{link} 的符号链接目标应为相对路径: {target}"
        assert link.resolve() == (_PACKAGE_ROOT / "references" / name).resolve()


def test_reference_symlink_content_matches_root_file():
    for name in _REFERENCE_NAMES:
        root = (_PACKAGE_ROOT / "references" / name).read_text(encoding="utf-8")
        linked = (
            (_PACKAGE_ROOT / "src" / "agent_fs" / "references" / name)
            .read_text(encoding="utf-8")
        )
        assert linked == root, f"references/{name} 链接内容与包根不一致"


def test_built_wheel_contains_references(tmp_path):
    """端到端：wheel 中必须包含全部 references 文件且内容与包根一致。"""
    import subprocess
    import sys
    import zipfile

    dist_dir = tmp_path / "dist"
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=str(_PACKAGE_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 and "No module named build" in (result.stderr or ""):
        import pytest

        pytest.skip("未安装 python -m build（pip install build），跳过端到端 wheel 构建验证")
    assert result.returncode == 0, f"wheel 构建失败:\nstdout={result.stdout}\nstderr={result.stderr}"

    wheel_files = list(dist_dir.glob("*.whl"))
    assert len(wheel_files) == 1
    with zipfile.ZipFile(wheel_files[0]) as zf:
        names = zf.namelist()
        for name in _REFERENCE_NAMES:
            entries = [n for n in names if n.endswith(f"references/{name}")]
            assert entries, f"wheel 中缺少 references/{name}，实际: {names}"
            assert zf.read(entries[0]).decode("utf-8") == (
                _PACKAGE_ROOT / "references" / name
            ).read_text(encoding="utf-8")
