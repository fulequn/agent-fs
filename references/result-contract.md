# 写入结果契约（schema_version=2）

> write-if-match / edit-if-match（实际写入与 dry-run）共用本契约；
> reconcile 使用自己的状态集合（见下）。字段与映射的唯一实现在
> `src/agent_fs/result.py`（`WriteOutcome` / `REASON_MAP`），本文件是文档镜像。
> 消费方必须以 `status` + `commit_state` + 哈希比较为准，不能只看退出码或 `written=true`。

## 三个哈希

| 名称 | 字段 | 含义 | 采集时机 |
|---|---|---|---|
| Hbase | `expected_sha256` | 构造本次修改所依据的完整原文件字节哈希 | 稳定读取原文件时 |
| Htarget | `desired_sha256` | 本次修改完成后预期完整文件字节的哈希 | 发出实际写入调用前（dry-run 预览或内容文件计算） |
| Hobserved | `observed_sha256` | 核实时稳定读取到的当前完整文件字节哈希 | 写后验证或恢复检查时 |

哈希为 SHA-256、按实际字节计算。换行符/编码/BOM 的变化必须属于已准备的修改。
实际观察值**从不**填入 `new_sha256`；两者分别保存、分别比较。

## 状态与退出码

| status | commit_state | 判定 | 退出码 | 允许的后续动作 |
|---|---|---|---|---|
| `verified` | `committed` | 替换已执行，稳定回读等于 Htarget | 0 | 继续内容验收 |
| `rejected` | `not_committed` | 基线冲突、持锁冲突或锚点拒绝，未替换 | 1 | 核实现状，重读后重新准备 |
| `error` | `not_committed` | 输入、准备或可确定发生在提交前的 I/O 错误 | 2 | 修正原因；再次写入前重新校验 |
| `unknown` | `committed` | 已替换但结果不符/不可读，或不能判定是否提交 | 3 | 只进入核实流程（reconcile），不自动重试 |
| `prepared` | `not_committed` | dry-run 预览成功（未修改目标） | 0 | 确认后用同一材料实际写入 |

- `committed` 仅表示本进程确认替换执行过，不表示文件以后没有被改动。
- 替换后的回读异常**不能回退成** `not_committed`。
- 退出码 3 是安全语义：调用方未收到完整 JSON、进程被中断或宿主只返回网络错误时，
  自行合成"结果未知"，不能假定 CLI 一定有机会输出该码。

## 必需字段

| 字段 | 含义 |
|---|---|
| `schema_version` | 固定为 2 |
| `command` / `path` | 命令与目标身份 |
| `status` / `commit_state` | 总体结果与本次替换事实 |
| `expected_sha256` / `desired_sha256` / `observed_sha256` | Hbase / Htarget / Hobserved |
| `previous_sha256` | 本次操作观察到的写前值；冲突时不冒充输入基线 |
| `reason_code` / `reason` | 稳定枚举 / 人类可读说明（分支判断只用前者） |
| `next_action` | 核实、修正输入、重新准备或继续验收 |
| `retryable` | 能否直接重放本次写入；首期一律 `false` |
| `durability` | `synced`（fsync 成功）/ `unconfirmed`（平台不支持或提交后同步失败）/ `not_applicable` |
| `written` / `edited` | 兼容字段：仅本次替换确定执行时为 true；**不能单独作为成功依据** |
| `new_sha256` | 兼容字段：仅 `verified` 时填 Htarget，其余为 null |
| `applied_edits` / `edits_file` / `edits_file_sha256` | edit-if-match 专用：编辑数与材料指纹 |
| `ancillary_failures` | 附属失败（临时清理/解锁等），不改变 commit_state |
| `detail` | 附加诊断（fsync 原因、材料哈希对比等） |

## reason_code 映射表（与 `result.REASON_MAP` 同步）

| reason_code | status | exit | 事件分类 |
|---|---|---|---|
| `replaced` / `edits_applied` | verified | 0 | 不上报 |
| `prepared` | prepared | 0 | 不上报 |
| `hash_mismatch` / `hash_mismatch_at_replace_time` | rejected | 1 | expected_block |
| `lock_busy` | rejected | 1 | expected_block |
| `anchor_not_found` / `anchor_not_unique` / `post_check_failed` | rejected | 1 | expected_block |
| `target_unstable` | rejected | 1 | expected_block |
| `prepared_content_changed` | rejected | 1 | expected_block |
| `file_not_found` / `not_a_regular_file` / `final_path_is_symlink` | error | 2 | caller_error |
| `invalid_edits_file` / `invalid_sha256_format` / `missing_hash` / `invalid_arguments` | error | 2 | caller_error |
| `empty_anchor` / `old_and_new_identical` | error | 2 | caller_error |
| `snapshot_not_found` / `snapshot_path_mismatch` / `snapshot_corrupted` | error | 2 | caller_error |
| `invalid_max_bytes` / `invalid_offset_or_limit` / `material_unreadable` | error | 2 | caller_error |
| `write_temp_file_failed` / `read_failed` / `recheck_failed` | error | 2 | agent_fs_core |
| `file_disappeared_before_replace` / `replace_failed` / `lock_failed` | error | 2 | agent_fs_core |
| `sync_io_error_before_commit` | error | 2 | agent_fs_core |
| `tmp_chmod_failed` | error | 2 | agent_fs_core |
| `mode_read_failed` | error | 2 | agent_fs_core |
| `unhandled_pre_commit` | error | 2 | agent_fs_core |
| `replaced_but_post_check_failed` | unknown | 3 | adapter_inconsistency |
| `observed_unstable_after_replace` | unknown | 3 | adapter_inconsistency |
| `observed_mismatch` | unknown | 3 | expected_block |
| `unhandled_post_commit` | unknown | 3 | agent_fs_core |

## durability 语义

- 成功所需文件与目录 fsync 全部成功 → `synced`。
- 平台不支持（ENOTSUP/EINVAL 等，含 Windows 目录 fsync）→ `unconfirmed` + `detail` 声明原因。
- 提交前真实同步 I/O 错误 → 阻止替换（`sync_io_error_before_commit`，目标原文保留）。
- 提交后目录 fsync 失败 → 不回滚、不重写；内容验证结论照常，`durability=unconfirmed`。
- `verified` 只证明当前内容匹配，不能推导掉电恢复保证。

## 兼容迁移要求（不声称完全兼容）

1. 只判断 `written=true` 或退出码 0 的调用方必须迁移到 `status`/`commit_state`——
   `unknown` 场景下 `written=true` 且退出码 3。
2. `new_sha256` 收紧为 verified-only；实际观察值看 `observed_sha256`。
3. `reason` 从"code[: 详情]"变为纯人类说明；分支判断改用 `reason_code`。
4. 退出码 3 新增：`set -e` 脚本行为不变（仍非零），但"任意非零即失败并重试"的
   消费方会把结果未知当错误重放——这正是本契约阻断的行为。
5. 参数错误输出 schema-2 全字段（保留 `error`/`category` 旧键）；无法取得的哈希值明确填 `null`。
6. 哈希参数校验通过后统一归一化为小写再参与比较；`observed_sha256`/`previous_sha256`/输出哈希始终为小写。
7. CLI 兜底（未预料异常）对写入类命令同样输出本契约全字段：`command`/`path` 取自
   调用上下文登记（main 解析后记命令名与路径，命令函数校验后补记 Hbase/Htarget），
   `unhandled_post_commit` 时 `durability=unconfirmed`、`pre_commit` 时
   `durability=not_applicable`；`--expected-result-sha256 ''` 属显式传入的非法
   Htarget（`invalid_sha256_format`/exit 2），不当作"未声明"跳过校验。

## reconcile 状态（独立集合）

| status | 判定 | 退出码 |
|---|---|---|
| `desired_present` | Hobserved = Htarget（Hbase == Htarget 时优先） | 0 |
| `base_present` | Hobserved = Hbase 且 ≠ Htarget | 1 |
| `diverged` | 与两者均不同 | 1 |
| `unavailable` | 目标缺失/不可读/持续不稳定 | 3 |
| `error` | 哈希参数非法 | 2 |

每次输出含 `observed_at`（观察时间），结论只对观察时刻有效。
