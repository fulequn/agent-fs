"""result.py 单元测试：schema-2 结果契约的映射规则是安全语义的核心，
必须全表覆盖，防止新登记的 reason_code 漏配 status/exit/事件分类。"""

from __future__ import annotations

import pytest

from agent_fs import result
from agent_fs.result import (
    COMMIT_COMMITTED,
    COMMIT_NOT_COMMITTED,
    EXIT_ERROR,
    EXIT_NEGATIVE,
    EXIT_OK,
    EXIT_UNKNOWN,
    REASON_MAP,
    SCHEMA_VERSION,
    STATUS_ERROR,
    STATUS_PREPARED,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    argument_error,
    error_outcome,
    reason_meta,
    reason_to_exit,
    rejected,
    unknown_outcome,
    verified,
)


class TestReasonMapCompleteness:
    def test_map_is_not_empty_and_keys_unique(self):
        assert REASON_MAP
        # dict 本身保证 key 唯一；这里锁死规模防止意外清空
        assert len(REASON_MAP) >= 30

    @pytest.mark.parametrize("reason_code", sorted(REASON_MAP.keys()))
    def test_every_reason_code_has_valid_tuple(self, reason_code):
        status, commit_state, exit_code, category = reason_meta(reason_code)
        assert status in {
            STATUS_VERIFIED,
            STATUS_REJECTED,
            STATUS_ERROR,
            STATUS_UNKNOWN,
            STATUS_PREPARED,
        }
        assert commit_state in {COMMIT_COMMITTED, COMMIT_NOT_COMMITTED}
        assert exit_code == reason_to_exit(status)
        assert category in {
            None,
            "expected_block",
            "caller_error",
            "external_failure",
            "adapter_inconsistency",
            "agent_fs_core",
        }

    def test_spec_6_1_core_rows(self):
        """SPEC §6.1 四行映射的锚点断言。"""
        assert reason_meta("replaced") == (STATUS_VERIFIED, COMMIT_COMMITTED, 0, None)
        assert reason_meta("hash_mismatch") == (
            STATUS_REJECTED,
            COMMIT_NOT_COMMITTED,
            1,
            "expected_block",
        )
        assert reason_meta("file_not_found") == (
            STATUS_ERROR,
            COMMIT_NOT_COMMITTED,
            2,
            "caller_error",
        )
        assert reason_meta("replaced_but_post_check_failed") == (
            STATUS_UNKNOWN,
            COMMIT_COMMITTED,
            3,
            "adapter_inconsistency",
        )

    def test_new_reason_codes_registered(self):
        """第一期新增码必须登记，缺一处就会在运行期走 error 兜底而非真实语义。"""
        for code in (
            "prepared_content_changed",
            "observed_mismatch",
            "observed_unstable_after_replace",
            "sync_io_error_before_commit",
            "material_unreadable",
            "unhandled_pre_commit",
            "unhandled_post_commit",
            "edits_applied",
            "tmp_chmod_failed",
            "mode_read_failed",
        ):
            assert code in REASON_MAP, f"缺少新 reason_code 登记: {code}"

    def test_prepared_content_changed_is_rejected_not_error(self):
        status, commit_state, exit_code, category = reason_meta("prepared_content_changed")
        assert (status, commit_state, exit_code, category) == (
            STATUS_REJECTED,
            COMMIT_NOT_COMMITTED,
            1,
            "expected_block",
        )

    def test_observed_mismatch_is_unknown_and_not_adapter_inconsistency(self):
        """外部写入者干扰属正常拦截（expected_block），与回读失败（adapter）区分。"""
        status, _, _, category = reason_meta("observed_mismatch")
        assert status == STATUS_UNKNOWN
        assert category == "expected_block"

    def test_every_code_has_reason_and_next_action_text(self):
        for code in REASON_MAP:
            assert result.reason_text(code), code
            assert result.next_action_text(code), code


class TestToDictCompatMapping:
    BASE = "a" * 64
    TARGET = "b" * 64
    OBSERVED = "c" * 64
    PREVIOUS = "d" * 64

    def test_verified_fills_new_sha256_with_target(self):
        out = verified(
            "write-if-match",
            "/tmp/f",
            desired_sha256=self.TARGET,
            observed_sha256=self.OBSERVED,
            expected_sha256=self.BASE,
        ).to_dict()
        assert out["schema_version"] == SCHEMA_VERSION
        assert out["status"] == STATUS_VERIFIED
        assert out["commit_state"] == COMMIT_COMMITTED
        assert out["written"] is True
        assert out["new_sha256"] == self.TARGET
        assert out["observed_sha256"] == self.OBSERVED
        assert out["desired_sha256"] == self.TARGET
        assert out["expected_sha256"] == self.BASE
        assert out["retryable"] is False

    def test_unknown_keeps_written_true_but_new_sha256_null(self):
        """A07/A08：替换确定执行（written=true）但结果不符——new_sha256 必须
        为 null，实际观察值单独放 observed_sha256，不得冒充 new_sha256。"""
        out = unknown_outcome(
            "write-if-match",
            "/tmp/f",
            "observed_mismatch",
            commit_state=COMMIT_COMMITTED,
            expected_sha256=self.BASE,
            desired_sha256=self.TARGET,
            observed_sha256=self.OBSERVED,
            previous_sha256=self.BASE,
        ).to_dict()
        assert out["status"] == STATUS_UNKNOWN
        assert out["commit_state"] == COMMIT_COMMITTED
        assert out["written"] is True
        assert out["new_sha256"] is None
        assert out["observed_sha256"] == self.OBSERVED
        assert "重试" not in out["next_action"]

    def test_not_committed_writes_written_false(self):
        out = rejected(
            "write-if-match",
            "/tmp/f",
            "hash_mismatch",
            expected_sha256=self.BASE,
            previous_sha256=self.OBSERVED,
        ).to_dict()
        assert out["written"] is False
        assert out["new_sha256"] is None
        # previous_sha256 是本次观察到的写前值，不冒充输入基线
        assert out["previous_sha256"] == self.OBSERVED
        assert out["expected_sha256"] == self.BASE

    def test_edit_command_emits_edited_and_edit_fields(self):
        out = verified(
            "edit-if-match",
            "/tmp/f",
            desired_sha256=self.TARGET,
            observed_sha256=self.TARGET,
            expected_sha256=self.BASE,
        ).to_dict()
        assert out["edited"] is True
        assert out["applied_edits"] == 0
        assert "edits_file" in out
        assert "edits_file_sha256" in out

    def test_write_command_does_not_emit_edited(self):
        out = verified(
            "write-if-match",
            "/tmp/f",
            desired_sha256=self.TARGET,
            observed_sha256=self.TARGET,
        ).to_dict()
        assert "edited" not in out

    def test_dry_run_prepared_not_committed(self):
        """prepared 是 dry-run 的成功预览：未写入、edited=False、new_sha256=null。"""
        from agent_fs.result import WriteOutcome

        outcome = WriteOutcome(
            command="edit-if-match",
            path="/tmp/f",
            status=STATUS_PREPARED,
            commit_state=COMMIT_NOT_COMMITTED,
            reason_code="prepared",
            expected_sha256=self.BASE,
            desired_sha256=self.TARGET,
            applied_edits=2,
        )
        data = outcome.to_dict()
        assert data["status"] == "prepared"
        assert data["commit_state"] == COMMIT_NOT_COMMITTED
        assert data["written"] is False
        assert data["edited"] is False
        assert data["new_sha256"] is None
        assert data["applied_edits"] == 2

    def test_ancillary_failures_surface_without_changing_status(self):
        out = verified(
            "write-if-match",
            "/tmp/f",
            desired_sha256=self.TARGET,
            observed_sha256=self.TARGET,
            ancillary_failures=("tmp_cleanup_failed: OSError",),
        ).to_dict()
        assert out["status"] == STATUS_VERIFIED
        assert out["ancillary_failures"] == ["tmp_cleanup_failed: OSError"]

    def test_durability_default_and_unconfirmed_detail(self):
        ok = verified(
            "write-if-match", "/tmp/f", desired_sha256=self.TARGET, observed_sha256=self.TARGET
        ).to_dict()
        assert ok["durability"] == "synced"

        degraded = verified(
            "write-if-match",
            "/tmp/f",
            desired_sha256=self.TARGET,
            observed_sha256=self.TARGET,
            durability="unconfirmed",
            detail="dir_fsync_unsupported: ENOTSUP",
        ).to_dict()
        assert degraded["durability"] == "unconfirmed"
        assert degraded["detail"] == "dir_fsync_unsupported: ENOTSUP"


class TestExitCodes:
    def test_status_to_exit(self):
        assert reason_to_exit(STATUS_VERIFIED) == EXIT_OK
        assert reason_to_exit(STATUS_PREPARED) == EXIT_OK
        assert reason_to_exit(STATUS_REJECTED) == EXIT_NEGATIVE
        assert reason_to_exit(STATUS_ERROR) == EXIT_ERROR
        assert reason_to_exit(STATUS_UNKNOWN) == EXIT_UNKNOWN

    def test_unknown_status_code_is_3(self):
        assert EXIT_UNKNOWN == 3


class TestConstructors:
    def test_error_outcome_defaults(self):
        out = error_outcome("write-if-match", "/tmp/f", "replace_failed").to_dict()
        assert out["status"] == STATUS_ERROR
        assert out["commit_state"] == COMMIT_NOT_COMMITTED
        assert out["written"] is False

    def test_unknown_outcome_committed(self):
        out = unknown_outcome(
            "write-if-match",
            "/tmp/f",
            "unhandled_post_commit",
            commit_state=COMMIT_COMMITTED,
        ).to_dict()
        assert out["commit_state"] == COMMIT_COMMITTED


class TestArgumentError:
    def test_schema2_shape_with_legacy_keys(self):
        data = argument_error(
            "write-if-match",
            "/tmp/f",
            "invalid_sha256_format",
            "invalid_sha256_format: expected 64 位十六进制字符串，实际长度 8",
            suggested_command="agent-fs write-if-match --help",
        )
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["status"] == STATUS_ERROR
        assert data["commit_state"] == COMMIT_NOT_COMMITTED
        assert data["reason_code"] == "invalid_sha256_format"
        assert data["category"] == "caller_error"
        assert data["retryable"] is False
        assert data["suggested_command"] == "agent-fs write-if-match --help"
        # 旧键兼容
        assert "error" in data

    def test_path_optional(self):
        data = argument_error("agent-fs", None, "invalid_arguments", "missing required arg")
        assert data["path"] is None


class TestArgumentErrorSchema2Completeness:
    def test_error_response_contains_all_required_fields(self):
        """审查 P2-5：参数错误也必须满足 schema-2 必需字段，哈希无法取得时明确 null。"""
        data = argument_error("write-if-match", "/tmp/f", "invalid_sha256_format", "bad hash")
        for key in (
            "schema_version", "command", "path", "status", "commit_state",
            "expected_sha256", "desired_sha256", "observed_sha256", "previous_sha256",
            "reason_code", "reason", "next_action", "retryable", "durability",
        ):
            assert key in data, f"缺少 schema-2 必需字段: {key}"
        assert data["expected_sha256"] is None
        assert data["desired_sha256"] is None
        assert data["observed_sha256"] is None
        assert data["durability"] == "not_applicable"
        assert data["written"] is False
