"""异常上报协议的行为测试（2026-09-16 重构后替代旧文案检查）。

覆盖 SKILL.md「异常上报协议（五级分类）」的可执行行为：
- 门禁正常阻断（expected_block）不产生待同步事件；
- 参数误用（caller_error）只返回修正建议，不进 syncable；
- 同一指纹只累计一次（count/last_seen 递增，不新增记录）；
- adapter_inconsistency / agent_fs_core 可被 --syncable 筛出；
- 上报（事件记录）失败不递归上报——record_event 自身异常向上抛，由
  CLI 的兜底捕获但不再次 record。
"""

import pytest

from agent_fs.events import (
    SYNCABLE_CATEGORIES,
    AnomalyEvent,
    normalize_error,
    record_event,
    sync_eligible,
)


def _event(reason_code="hash_mismatch", component="cli.verify", normalized="x"):
    return AnomalyEvent(
        component=component,
        operation="verify",
        reason_code=reason_code,
        normalized_error=normalized,
        category="expected_block",
    )


class TestFingerprintDedup:
    def test_same_fingerprint_appends_duplicate_and_read_side_aggregates(self, tmp_path):
        """append-only：写入端不合并，同指纹两行在读取端聚合为 count=2。"""
        import json

        from agent_fs.events import load_events

        f = tmp_path / "events.jsonl"
        e = _event(normalized="anchor missing")
        r1 = record_event(e, events_file=f)
        r2 = record_event(_event(normalized="anchor missing"), events_file=f)
        # 写入端不再做读改写合并；去重语义由读取端承担
        assert r1["merged"] is False and r2["merged"] is False
        assert r1["deduplicated_at_read"] is True
        lines = [ln for ln in f.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2
        rec = json.loads(lines[0])
        assert rec["count"] == 1
        assert rec["last_seen"] >= rec["first_seen"]

        aggregated = load_events(f)
        assert len(aggregated) == 1
        assert aggregated[0]["count"] == 2
        assert aggregated[0]["last_seen"] >= aggregated[0]["first_seen"]

    def test_different_normalized_error_is_new_record(self, tmp_path):
        f = tmp_path / "events.jsonl"
        record_event(_event(normalized="anchor missing"), events_file=f)
        record_event(_event(normalized="lock contention"), events_file=f)
        lines = [ln for ln in f.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2

    def test_normalize_error_strips_volatile_parts(self):
        raw = "FileNotFoundError: /Users/abc/proj/src/x.py at line 42: ts 1789531359538 hash aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899"
        out = normalize_error(raw)
        assert "/Users/abc" not in out
        assert "line 42" not in out
        assert "1789531359538" not in out
        assert "aabbccdd" not in out
        # 文件名保留（便于定位）
        assert "x.py" in out


class TestSyncEligibility:
    def test_only_two_categories_syncable(self, tmp_path):
        f = tmp_path / "events.jsonl"
        record_event(
            AnomalyEvent(
                component="cli.verify",
                operation="verify",
                reason_code="hash_mismatch",
                normalized_error="x",
                category="expected_block",
            ),
            events_file=f,
        )
        record_event(
            AnomalyEvent(
                component="cli.verify",
                operation="verify",
                reason_code="file_not_found",
                normalized_error="y",
                category="caller_error",
            ),
            events_file=f,
        )
        record_event(
            AnomalyEvent(
                component="external.apply_patch",
                operation="write",
                reason_code="hash_unchanged_after_claimed_write",
                normalized_error="z",
                category="adapter_inconsistency",
            ),
            events_file=f,
        )
        record_event(
            AnomalyEvent(
                component="core.read_fresh",
                operation="read",
                reason_code="unstable_after_retries",
                normalized_error="w",
                category="agent_fs_core",
            ),
            events_file=f,
        )

        import json

        events = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        syncable = sync_eligible(events)
        cats = {e["category"] for e in syncable}
        assert cats == {"adapter_inconsistency", "agent_fs_core"}
        assert len(syncable) == 2

    def test_expected_block_not_syncable(self):
        assert "expected_block" not in SYNCABLE_CATEGORIES
        assert "caller_error" not in SYNCABLE_CATEGORIES
        assert "external_failure" not in SYNCABLE_CATEGORIES


class TestCorruptLines:
    def test_corrupted_line_does_not_block_recording(self, tmp_path):
        import json

        from agent_fs.events import load_events

        f = tmp_path / "events.jsonl"
        f.write_text("{not valid json}\n", encoding="utf-8")
        r = record_event(_event(), events_file=f)
        assert r["recorded"] is True
        # 损坏行原样保留（append-only 不重写），新行追加成功且合法
        lines = [ln for ln in f.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2
        json.loads(lines[1])
        # 读取端跳过损坏行
        assert len(load_events(f)) == 1


class TestConcurrentWrites:
    def test_thread_concurrent_no_loss(self, tmp_path):
        """多线程并发记录：全部事件保留（曾 80 并发丢 10 条）。"""
        import threading

        from agent_fs.events import load_events

        f = tmp_path / "events.jsonl"
        N = 40
        barrier = threading.Barrier(N)

        def worker(i):
            barrier.wait()  # 最大化竞争窗口
            record_event(
                AnomalyEvent(
                    component=f"cli.t{i % 4}",
                    operation="op",
                    reason_code="hash_mismatch",
                    normalized_error=f"err{i}",
                    category="expected_block",
                ),
                events_file=f,
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = load_events(f)
        assert len(events) == N  # 每个事件指纹唯一，应全部保留

    def test_event_file_mode_0600(self, tmp_path):
        """事件文件权限仅所有者可读写（含敏感路径信息）。"""
        import os
        import stat

        f = tmp_path / "events.jsonl"
        record_event(_event(), events_file=f)
        assert stat.S_IMODE(os.stat(f).st_mode) & 0o077 == 0  # group/other 无权限

    def test_path_and_detail_normalized(self, tmp_path):
        """path/detail 存储前统一脱敏（绝对路径 → <path>/文件名）。"""
        import json

        f = tmp_path / "events.jsonl"
        record_event(
            AnomalyEvent(
                component="cli.verify",
                operation="verify",
                reason_code="file_not_found",
                normalized_error="x",
                category="caller_error",
                path="/Users/secretuser/project/file.py",
                detail="failed at /Users/secretuser/other",
            ),
            events_file=f,
        )
        rec = json.loads(f.read_text().splitlines()[0])
        assert "/Users/secretuser" not in rec["path"]
        assert "/Users/secretuser" not in rec["detail"]
        assert "file.py" in rec["path"]  # 文件名保留


class TestExplicitCompact:
    def test_compact_merges_duplicates_only_when_requested(self, tmp_path):
        import json

        from agent_fs.events import compact_events, load_events

        f = tmp_path / "events.jsonl"
        record_event(_event(normalized="dup"), events_file=f)
        record_event(_event(normalized="dup"), events_file=f)
        assert len(f.read_text(encoding="utf-8").splitlines()) == 2

        result = compact_events(f)

        assert result["compacted"] is True
        assert result["events"] == 1
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["count"] == 2
        assert load_events(f)[0]["count"] == 2


class TestAppendFailure:
    def test_append_failure_reports_false_not_silent_success(self, tmp_path, monkeypatch):
        """落盘失败必须 recorded=false：不能像旧实现那样谎报成功。"""
        from agent_fs import events as ev

        f = tmp_path / "events.jsonl"
        real_open = ev.os.open

        def fail_events_open(path, *args, **kwargs):
            if str(path) == str(f):
                raise OSError("disk full (simulated)")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(ev.os, "open", fail_events_open)
        r = ev.record_event(_event(), events_file=f)
        assert r["recorded"] is False
        assert "append_failed" in r["degraded"]
        assert not f.exists()

    def test_keybd_interrupt_not_swallowed(self, tmp_path, monkeypatch):
        """KeyboardInterrupt 必须上抛，不能被错误处理吞掉。"""
        from agent_fs import events as ev

        def raising_open(path, *args, **kwargs):
            raise KeyboardInterrupt()

        monkeypatch.setattr(ev.os, "open", raising_open)
        with pytest.raises(KeyboardInterrupt):
            ev.record_event(_event(), events_file=tmp_path / "e.jsonl")


class TestSensitiveRedaction:
    def test_token_password_redacted(self, tmp_path):
        """token/Authorization/password 的值不落盘。"""
        import json

        f = tmp_path / "events.jsonl"
        record_event(
            AnomalyEvent(
                component="cli.verify",
                operation="verify",
                reason_code="auth_failed",
                normalized_error="x",
                category="external_failure",
                detail="Authorization: Bearer abc123def456 failed; token=xyz789; password=hunter2",
            ),
            events_file=f,
        )
        raw = f.read_text(encoding="utf-8")
        assert "abc123def456" not in raw
        assert "xyz789" not in raw
        assert "hunter2" not in raw
        rec = json.loads(raw.splitlines()[0])
        assert "<redacted>" in rec["detail"]

    def test_json_quoted_and_cookie_multivalue_redacted(self, tmp_path):
        f = tmp_path / "events.jsonl"
        record_event(
            AnomalyEvent(
                component="cli.verify",
                operation="verify",
                reason_code="auth_failed",
                normalized_error='{"token": "jsonsecret123", "password": "pwsecret"}',
                category="external_failure",
                detail='Cookie: a=firstsecret; b=secondsecret\n{"api_key": "keysecret"}',
            ),
            events_file=f,
        )
        raw = f.read_text(encoding="utf-8")
        for secret in (
            "jsonsecret123", "pwsecret", "firstsecret", "secondsecret", "keysecret"
        ):
            assert secret not in raw

    def test_windows_and_unc_paths_redacted(self):
        drive = normalize_error(r"failed at C:\Users\alice\proj\src\x.py:42")
        assert "alice" not in drive
        assert "x.py" in drive
        unc = normalize_error(r"failed at \\fileserver\share\team\x.py")
        assert "fileserver" not in unc
        assert "x.py" in unc

    def test_normalized_error_field_is_sanitized(self, tmp_path):
        f = tmp_path / "events.jsonl"
        record_event(
            AnomalyEvent(
                component="cli.verify",
                operation="verify",
                reason_code="auth_failed",
                normalized_error="token=directfieldsecret",
                category="external_failure",
            ),
            events_file=f,
        )
        assert "directfieldsecret" not in f.read_text(encoding="utf-8")

    def test_events_dir_mode_0700(self, tmp_path):
        """事件目录权限仅所有者可进入。"""
        import os
        import stat

        home = tmp_path / "home"
        home.mkdir()
        record_event(_event(), events_file=home / "events.jsonl")
        assert stat.S_IMODE(os.stat(home).st_mode) & 0o077 == 0
