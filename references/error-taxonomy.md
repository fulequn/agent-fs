# 错误分类对照表（reason_code → 五级类别）

> 本文件是「异常上报协议」的判定细节表，由 SKILL.md 按需引用。
> 写入结果契约的 reason_code 唯一实现在 `src/agent_fs/result.py`（`REASON_MAP`），
> guard 等非写入链路由 `src/agent_fs/events.py`（`_REASON_CATEGORY_MAP`）兜底；
> `classify()` 优先委托 result.REASON_MAP。本表是文档镜像——代码变更时同步更新。
> 写入结果的 status/commit_state/退出码语义见 `result-contract.md`。

## expected_block（协议正常工作，不上报）

| reason_code | 场景 |
|---|---|
| `hash_mismatch` / `hash_mismatch_at_replace_time` | write-if-match/edit-if-match 基线不匹配（协作者已改） |
| `lock_busy` | 其他 agent-fs 调用者持锁 |
| `anchor_not_found` / `anchor_not_unique` / `post_check_failed` | 编辑锚点失配或重放校验未通过 |
| `target_unstable` | 锁内稳定读取失败（内容正在被并发修改） |
| `prepared_content_changed` | 实际准备字节与声明的 Htarget 不一致（材料漂移被拦截） |
| `observed_mismatch` | 替换后观察哈希 ≠ Htarget（外部写入者干扰，结果如实报告 unknown） |
| `git_diff_check_failed` | guard git-commit 正常拦截（空白错误） |

> `intervals_overlap` 仅在历史事件流中出现：编辑列表的原始区间重叠已降级为
> `warnings`，不再作为拒绝理由。

## caller_error（调用方修正，不上报）

| reason_code | 场景 |
|---|---|
| `file_not_found` / `not_a_regular_file` | 路径写错（含 `read-fresh` 传了目录） |
| `invalid_sha256_format` / `invalid_arguments` / `missing_hash` | 参数格式错（含 snapshot ID 误当哈希等） |
| `invalid_edits_file` / `invalid_json` | 编辑列表格式错 |
| `empty_anchor` / `old_and_new_identical` | 编辑材料语义错：空锚点会匹配任意位置；`old == new` 不产生变化 |
| `snapshot_not_found` / `snapshot_path_mismatch` / `snapshot_corrupted` | 快照 ID 错、属于别的文件或记录损坏 |
| `invalid_max_bytes` / `invalid_offset_or_limit` | 读取参数错（含负数） |
| `material_unreadable` | content/edits 材料文件不可读 |
| `binary_or_undecodable` | 对二进制文件做文本编辑 |
| `final_path_is_symlink` | 传入了符号链接（resolve() 后传物理路径） |

## external_failure（外部依赖问题，根因归平台；agent-fs 负责文件状态）

构建失败、SSO/认证失败、浏览器自动化失败、网络失败——**根因**由对应平台/Skill
排障；Agent-fs 负责**文件结果核实与安全恢复**（reconcile），两者不互相替代：
根因分类为 external_failure 不代表文件状态已确定，文件状态按结果契约单独表达
（`status=unknown` 时只进入核实流程）。

## adapter_inconsistency（工具回执与文件证据矛盾，待复现，可同步学城）

| reason_code | 场景 |
|---|---|
| `replaced_but_post_check_failed` | 替换已执行但稳定回读失败（退出码 3，只核实不重试） |
| `observed_unstable_after_replace` | 替换后回读反复不稳定（退出码 3） |
| `hash_unchanged_after_claimed_write` | 外部编辑工具称成功但哈希未变 |

## agent_fs_core（本体缺陷，可同步学城，P0/P1）

| reason_code | 场景 |
|---|---|
| `unstable_after_retries` | 反复读取仍不稳定（可复现时） |
| `lock_failed` / `replace_failed` / `write_temp_file_failed` | 锁、替换或临时文件系统调用失败 |
| `read_failed` / `recheck_failed` / `file_disappeared_before_replace` | 读取或替换前复核失败 |
| `sync_io_error_before_commit` | 提交前真实同步 I/O 错误（阻止替换） |
| `tmp_chmod_failed` | 临时文件无法保留原文件权限（阻止提交，避免替换扩大/丢失权限） |
| `mode_read_failed` | 替换前读取原文件权限位失败（阻止提交，避免以临时文件 0600 权限落地丢失/改变权限） |
| `unhandled_pre_commit` / `unhandled_post_commit` | CLI 未预料异常（按执行阶段分流） |
| `unhandled_exception` | 旧码兼容（历史事件流） |

## 指纹去重

指纹 = `component + operation + reason_code + normalized_error + platform + agent_fs_version`
（`normalize_error` 剥离绝对路径/行号/时间戳/哈希等易变部分）。相同指纹只递增
`count`/`last_seen`，不新增记录。事件流：`~/.agent-fs/events.jsonl`。
事件另含 `file_state`（文件状态）与 `recovery_action`（恢复动作）字段——
根因、文件状态、恢复动作三者分开记录，不互相替代（SPEC §9）。
