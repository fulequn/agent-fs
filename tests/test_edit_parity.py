"""P1-7 契约测试：guard / --dry-run / 实际写入对同一份 edits 结论一致。

统一实现位于 `agent_fs.edits`；本文件只验证三处调用方在链式、自包含、
引入重复、区间重叠、空锚点等历史分裂场景下给出同一个判定。
"""

from __future__ import annotations

import json

from agent_fs.edit_if_match import edit_if_match, prepare_edits
from agent_fs.fingerprint import compute_fingerprint
from agent_fs.guard import guard_multiedit
from agent_fs.result import STATUS_PREPARED, STATUS_VERIFIED


def _setup(tmp_path, content: str, edits: list[dict[str, str]]):
    target = tmp_path / "doc.md"
    target.write_text(content, encoding="utf-8")
    edits_file = tmp_path / "edits.json"
    edits_file.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
    return target, str(edits_file)


def _sha(target) -> str:
    return compute_fingerprint(target).sha256


def _assert_agrees(tmp_path, content, edits, *, ok, expected_content=None):
    """三处结论一致；失败时目标字节不变且 reason_code 相同。"""
    target, edits_file = _setup(tmp_path, content, edits)
    base_sha = _sha(target)

    guard = guard_multiedit(str(target), edits_file)
    preview = prepare_edits(target, edits_file, base_sha)
    actual = edit_if_match(target, edits_file, base_sha)

    assert guard["ok"] is ok
    assert (preview.status == STATUS_PREPARED) is ok
    assert (actual.status == STATUS_VERIFIED) is ok

    if ok:
        assert guard["sha256"] == base_sha
        assert actual.observed_sha256 == preview.desired_sha256
        assert target.read_text(encoding="utf-8") == expected_content
    else:
        # 预检与实际写入必须给出同一个失败原因和同一个失败下标
        assert guard["reason"] == preview.reason_code == actual.reason_code
        assert preview.applied_edits == actual.applied_edits
        assert target.read_text(encoding="utf-8") == content
    return guard, preview, actual


CHAIN_FAILING = [{"old": "a", "new": "b"}, {"old": "b", "new": "c"}]
CHAIN_LEGAL = CHAIN_FAILING  # 同一份材料：在 "ab" 上失败，在 "a" 上合法


def test_chain_on_generated_text_is_legal_everywhere(tmp_path):
    """guard 旧实现看原始内容里没有 "b" 会误拒；三处现在都放行。"""
    guard, _, _ = _assert_agrees(
        tmp_path, "a", CHAIN_LEGAL, ok=True, expected_content="c"
    )
    assert "warnings" not in guard


def test_chain_reintroducing_duplicate_is_rejected_everywhere(tmp_path):
    """a→b 之后 b 出现两次：三处都在 edit[1] 以 anchor_not_unique 拒绝。"""
    guard, preview, actual = _assert_agrees(
        tmp_path, "ab", CHAIN_FAILING, ok=False
    )
    assert guard["edit_index"] == preview.applied_edits == actual.applied_edits == 1
    assert guard["occurrences"] == 2
    assert guard["positions"] == [0, 1]
    assert guard["lines"] == [1, 1]


def test_anchor_diagnostics_report_line_numbers(tmp_path):
    """重复锚点的诊断带出行号：调用方可以按行修正，而不必自己换算偏移。"""
    guard, _, _ = _assert_agrees(
        tmp_path,
        "head\nanchor\nmid\nanchor\n",
        [{"old": "anchor", "new": "x"}],
        ok=False,
    )
    assert guard["reason"] == "anchor_not_unique"
    assert guard["positions"] == [5, 16]
    assert guard["lines"] == [2, 4]


def test_prefix_self_contained_edit_is_legal_everywhere(tmp_path):
    """abc 中 a→ab：替换后 old 仍是 new 的子串，不得误判 post_check_failed。"""
    _assert_agrees(
        tmp_path,
        "abc",
        [{"old": "a", "new": "ab"}],
        ok=True,
        expected_content="abbc",
    )


def test_overlapping_anchors_are_warning_not_failure(tmp_path):
    """原始区间重叠但顺序应用成功：guard 放行并给出 warning。"""
    guard, _, _ = _assert_agrees(
        tmp_path,
        "abc",
        [{"old": "ab", "new": "abc"}, {"old": "bc", "new": "z"}],
        ok=True,
        expected_content="azc",
    )
    assert guard["warnings"] and "区间重叠" in guard["warnings"][0]


def test_duplicate_introduced_by_earlier_edit_is_handled(tmp_path):
    """前一个编辑引入重复 "bb"，但不影响后续锚点。"""
    _assert_agrees(
        tmp_path,
        "aX",
        [{"old": "a", "new": "bb"}, {"old": "X", "new": "Y"}],
        ok=True,
        expected_content="bbY",
    )


def test_overlap_that_removes_later_anchor_fails_on_apply(tmp_path):
    """区间重叠且前一步吃掉了后一步锚点：三处都以前一步之后的锚点缺失拒绝。"""
    guard, _, _ = _assert_agrees(
        tmp_path,
        "abcdef",
        [{"old": "abcd", "new": "x"}, {"old": "cdef", "new": "y"}],
        ok=False,
    )
    assert guard["edit_index"] == 1
    assert guard["occurrences"] == 0


def test_missing_anchor_rejected_everywhere(tmp_path):
    guard, preview, _ = _assert_agrees(
        tmp_path, "abc", [{"old": "zzz", "new": "x"}], ok=False
    )
    assert guard["edit_index"] == 0
    assert "锚点在最新内容中不存在" in preview.detail


class TestMaterialValidationParity:
    def test_empty_anchor_rejected_everywhere(self, tmp_path):
        """空锚点会匹配任意位置：三处都拒绝，且不修改目标。"""
        target, edits_file = _setup(tmp_path, "abc", [{"old": "", "new": "x"}])
        base_sha = _sha(target)

        guard = guard_multiedit(str(target), edits_file)
        preview = prepare_edits(target, edits_file, base_sha)
        actual = edit_if_match(target, edits_file, base_sha)

        assert guard["ok"] is False and guard["reason"] == "empty_anchor"
        assert guard["edit_index"] == 0
        # 材料语义错误保留细分码：三处都指向同一条 edit
        assert preview.reason_code == actual.reason_code == "empty_anchor"
        assert target.read_text(encoding="utf-8") == "abc"

    def test_old_equals_new_rejected_everywhere(self, tmp_path):
        target, edits_file = _setup(tmp_path, "abc", [{"old": "a", "new": "a"}])
        base_sha = _sha(target)

        guard = guard_multiedit(str(target), edits_file)
        preview = prepare_edits(target, edits_file, base_sha)

        assert guard["ok"] is False and guard["reason"] == "old_and_new_identical"
        assert guard["edit_index"] == 0
        assert preview.reason_code == "old_and_new_identical"
        assert target.read_text(encoding="utf-8") == "abc"

    def test_unreadable_material_rejected_everywhere(self, tmp_path):
        target = tmp_path / "doc.md"
        target.write_text("abc", encoding="utf-8")
        missing = str(tmp_path / "missing.json")
        base_sha = _sha(target)

        guard = guard_multiedit(str(target), missing)
        preview = prepare_edits(target, missing, base_sha)

        assert guard["ok"] is False and guard.get("error") is True
        assert guard["reason"] == "invalid_edits_file"
        assert preview.reason_code == "invalid_edits_file"

    def test_malformed_json_rejected_everywhere(self, tmp_path):
        target = tmp_path / "doc.md"
        target.write_text("abc", encoding="utf-8")
        edits_file = tmp_path / "edits.json"
        edits_file.write_text("{not json", encoding="utf-8")
        base_sha = _sha(target)

        guard = guard_multiedit(str(target), str(edits_file))
        actual = edit_if_match(target, str(edits_file), base_sha)

        assert guard["ok"] is False and guard["reason"] == "invalid_edits_file"
        assert actual.reason_code == "invalid_edits_file"
        assert target.read_text(encoding="utf-8") == "abc"
