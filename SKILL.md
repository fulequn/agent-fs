---
name: agent-fs
description: "多 Agent/多人/多脚本文件协作可靠性协议，agent-fs CLI 提供 snapshot/read-fresh/verify/changed/write-if-match/edit-if-match/reconcile/guard/events：稳定读取、写前校验、受控写入、中断恢复。触发场景（满足任一）：1. 覆盖可能已被他人修改的文件；2. 验收子 Agent/并行任务写入成果；3. 操作非 Git 路径或近期人工编辑过的文件；4. 得出受保护文件「没变化」「不存在」「为空」等否定性结论前；5. 编辑调用超时、断线或响应缺失后；6. 工具回执与哈希等独立证据矛盾时。NOT for：单纯阅读与探索性搜索、单 Agent 顺序任务且无并发写入风险的低风险操作。触发词：TOCTOU、MultiEdit、pathspec、并发写入。"
---

# agent-fs（多 Agent 文件协作可靠性协议）

> **背景**：多个 Agent、脚本和人工可能同时操作同一份工作区。一个 Agent 读到旧版本后，
> 若没有重新校验就据此写入或下结论，会静默覆盖别人已经写入的内容，或者给出已经过时的
> 否定性结论（"文件没变化""字段不存在""为空"）。编辑工具调用还可能超时、断线或丢失
> 响应——此时**调用异常不等于没写入**，必须先核实文件状态再决定恢复动作。
> 本 Skill 定义了何时必须重新校验、用什么工具校验、中断后如何恢复，
> `agent-fs` CLI 提供不依赖网络的本地稳定实现。

## 何时必须触发本协议

满足以下任一条件时，**不能**直接相信已有的工具调用结果（哪怕是几秒钟前的），必须走下面的强校验流程：

1. 准备得出**受保护文件版本或编辑结果**的否定性结论：「文件没有变化」「某段实现不存在」「字段没有写入」等（不扩展到无关任务的普通判断）。
2. 准备覆盖一个可能已被别人（人工/其他 Agent/脚本）修改过的文件。
3. 两个读取工具的结果不一致（例如 `read_file` 和 `grep`/`sed` 对同一文件给出不同内容）。
4. 主 Agent 正在验收子 Agent 或并行任务的写入成果。
5. 文件路径不受版本控制（非 Git 仓库路径），或该文件近期确认被人工编辑过。
6. 编辑工具调用超时、连接中断、响应缺失，或结果状态未知（`unknown`）。
7. 工具回执（成功/失败）与独立的文件证据（哈希、内容）互相矛盾。
8. 上次编辑因中断而恢复未完成。

只读浏览、探索性搜索等低风险操作不需要走本协议，避免不必要的开销。

## 安装

`agent-fs` 使用 `src` 布局，未安装时不能在任意目录直接执行 `python3 -m agent_fs.cli`；优先在虚拟环境中安装，避免 macOS PEP 668 或系统 Python 环境导致入口漂移。本 Skill 目录本身就是一个可安装的 Python 包（含 `pyproject.toml`）：在 fulequn-tools 仓库中路径为 `tools/agent-fs`，通过 Skill 市场安装时就是 Skill 根目录。

```bash
SKILL_DIR=/absolute/path/to/agent-fs   # 仓库内替换为 .../tools/agent-fs
python3 -m venv .venv-agent-fs
. .venv-agent-fs/bin/activate
python3 -m pip install -e "$SKILL_DIR"
python3 -m agent_fs.cli --help
agent-fs --help
```

CLI 安装在虚拟环境或 conda 目录时，桌面客户端（Catpaw/Codex 等）从 GUI 启动可能继承不到该 `PATH`；在 `/usr/local/bin` 建一个指向 `agent-fs` 的软链，或显式配置客户端环境变量。

若只在源码目录运行测试，可使用 `python3 -m pytest`（`pyproject.toml` 已配置 `pythonpath = ["src"]`）；不要把 `PYTHONPATH=src` 当作安装替代方案。

## 核心命令

```bash
agent-fs snapshot <file> [--max-retries N]
agent-fs snapshot prune [--older-than DAYS] [--keep-last N] [--dry-run]
agent-fs read-fresh <file> [--offset N] [--limit N] [--max-bytes N] [--no-content] [--output FILE] [--compact]
agent-fs verify <file> (--expected-sha256 <hash> | --sha256 <hash> | --snapshot <snapshot-id>) [--max-retries N]
agent-fs edit-if-match <file> (--expected-sha256 | --sha256 | --snapshot) --edits-file edits.json \
    [--expected-result-sha256 <hash>] [--dry-run]
agent-fs write-if-match <file> --expected-sha256 <hash> [--expected-result-sha256 <hash>] [--content STR | --content-file PATH]
agent-fs reconcile <file> --base-sha256 <hash> --desired-sha256 <hash> [--max-retries N]
agent-fs events list [--syncable] [--since ISO-8601] [--limit N]
agent-fs changed <file> --since <snapshot-id> [--max-retries N]
agent-fs ops list [--pending] [--target PATH] [--limit N]
```

**多点编辑主流程用 `edit-if-match`**（锁内一次完成稳定读取→锚点校验→应用全部替换→稳定回读→原子替换）；先 `--dry-run` 预览可获得预期结果哈希（Htarget）。CLI 参数错误也返回结构化 JSON（含 category/suggested_command/next_action），不再是 argparse usage 文本。`read-fresh` 返回的 `sha256` 始终是**完整文件字节**的 SHA-256，即使使用 `--offset`/`--limit` 返回了截断内容。

### 退出码

| 退出码 | 含义 | 允许的后续动作 |
|---|---|---|
| `0` | 成功（稳定/匹配/未变化/已写入/预览成功） | 继续内容验收 |
| `1` | 业务性拒绝（不匹配/已变化/锁忙/锚点失配） | 核实现状，重读后重新准备 |
| `2` | 程序或输入错误（文件不存在、参数错误等） | 修正原因；再次写入前重新校验 |
| `3` | **写入结果未知**（已替换但结果不符/不可读，或不能判定是否提交） | **只进入核实流程（reconcile），不自动重试写入** |

退出码 3 是安全语义：调用超时、进程被中断或宿主只返回网络错误时，调用方应自行合成"结果未知"，不能假定 CLI 一定有机会输出。**不能只看退出码或 `written=true` 判定成功**，必须消费 `status`/`commit_state` 并比较哈希（见 `references/result-contract.md`）。

## 高频操作预检：`agent-fs guard`

`guard` 只检查并输出 JSON，**绝不**创建 worktree、提交、cherry-pick 或写入目标文件。退出码 `0` 表示可以继续显式执行后续操作，`1` 表示风险被阻断，`2` 表示输入或环境错误。预检通过也不替代后续受控写入协议。

```bash
# 提交前同时检查工作区和暂存区（两个 diff --check）
agent-fs guard git-commit --cwd /absolute/path/to/repo

# cherry-pick 前确认 commit 对象已经存在于本地对象库
agent-fs guard git-object <commit> --cwd /absolute/path/to/repo

# 对包含路径的 Git 命令，要求 pathspec 位于 -- 后；命令必须以 git 开头
agent-fs guard pathspec git log <revision> -- path/to/file

# 写入前确认自己位于预期的隔离 worktree 和分支
agent-fs guard worktree --expected-root /absolute/path/to/worktree --expected-branch <branch> --cwd /absolute/path/to/worktree

# Shell 长文本先落入内容文件，检查 UTF-8 与替换字符后再受控写入
agent-fs guard text-write --content-file /tmp/new-content.txt
agent-fs write-if-match target.md --expected-sha256 <hash> --expected-result-sha256 <target-hash> --content-file /tmp/new-content.txt

# MultiEdit 前检查锚点唯一、old/new 不同；按最新内容顺序应用（原始区间重叠只告警）
agent-fs guard multiedit target.md --edits-file edits.json
```

`multiedit` 的 `edits.json` 是数组，每项为 `{"old": "待替换文本", "new": "新文本"}`。预检与 `edit-if-match --dry-run`/实际写入共用同一套顺序应用语义：第 N 个编辑基于前 N-1 个编辑的结果，锚点唯一性按**应用时的最新内容**判定；原始内容里的区间重叠只作为 `warnings` 返回，不构成拒绝理由。失败结果会带 `edit_index`、`occurrences`、`positions`、`lines`（1-based 行号）和 `next_action`。空锚点（`old == ""`）与 `old == new` 分别返回 `empty_anchor` / `old_and_new_identical`（`caller_error`，退出码 2）。**编辑工具出现网络失败时，首次出现不确定结果就先核实（reconcile），换工具前必须确认原调用已停止**；不得因终端可用就直接重定向覆盖、`sed` 原地修改或重复调用 MultiEdit。禁止通过包含 Markdown 反引号、`$()` 或复杂引号的 `python -c` 拼接长文本。

所有命令输出 JSON 到 stdout；退出码语义见上表。

### `snapshot`：采集稳定快照

内部执行 stat→hash→stat 三明治校验：先采样一次 `size`/`mtime`/`inode`，计算内容 `SHA-256`，再采样一次元信息，两次不一致则判定为不稳定并退避重试（默认 3 次）。稳定后落盘到 `~/.agent-fs/snapshots/`，返回 `snapshot_id`，供后续 `changed` 命令比对。若反复重试仍不稳定，命令返回 `stable: false`，**不会**落盘任何快照，调用方不得把这次结果当作有效快照使用。

`agent-fs snapshot prune` 清理本地快照，默认不删任何文件：必须显式给出 `--older-than DAYS`（按快照文件 mtime）或 `--keep-last N`（**每个目标文件**保留最新 N 份）之一；两个条件同时给出时取交集（更老且不在保留窗口内）。建议先加 `--dry-run` 预览将删除的 `snapshot_id`；损坏或无法解析的快照不参与分组，也不会被删除。

### `read-fresh`：新鲜读取

在读取前后各采集一次文件元信息（`stat`），对实际读取到的字节计算哈希，三者在同一轮完成。若读取期间文件被并发修改，本轮结果标记为 `stable: false`，内部已按退避重试（默认 3 次），仍不稳定则返回冲突，**不得**把这次的 `content` 当作结论依据。

`--offset`/`--limit` 按**行**（从 0 开始）切片，`--max-bytes` 按**字节**截断；参数为负、路径缺失或路径不是普通文件时按调用方错误处理（`category=caller_error`，退出码 2）。`--output FILE` 覆盖已有文件时保留其普通权限位（`0o777`），但不继承 setuid/setgid/sticky 特殊位；输出路径与输入文件是同一文件（含软链/硬链别名）时返回 `output_path_conflict`，不会截断源文件。

### `verify`：诊断性校验（不再是受保护主流程的写入步骤）

> `verify` 只能证明"命令执行那一刻"哈希匹配，不保护之后由其他工具完成的写入。
> 受保护主流程已统一为「read-fresh → 准备 → 受控写入 → 结果核实」（见下节）；
> `verify` 降级为诊断工具，用于核对独立证据或排查矛盾。

### `write-if-match` / `edit-if-match`：受控写入

单命令内完成「基线校验 + 替换 + 稳定回读」：获取绑定目标绝对路径的协作锁 → 校验 Hbase → 写临时文件（保留权限）→ 落盘前复核 → `os.replace()` 原子替换 → 稳定回读比较 Hobserved 与 Htarget。遵守同一协议的并发写入者会在锁占用时返回 `lock_busy`；锁文件位于 `$AGENT_FS_HOME/locks/`（未设置时为 `~/.agent-fs/locks/`）。协作锁只约束同样调用 agent-fs 的写入者；编辑器、`write`、`sed` 等外部写入不会自动获取该锁，**不能宣称全局互斥**。

- `status=verified, commit_state=committed`：替换已执行且稳定回读等于 Htarget，`new_sha256` 即 Htarget。
- `status=rejected, commit_state=not_committed`：未替换（`hash_mismatch`/`lock_busy`/`prepared_content_changed` 等），核实现状后重新准备。
- `status=unknown, commit_state=committed`（退出码 3）：替换已执行但结果不符/不可核实——**绝不提示直接重试**，只进入核实流程。
- 分支判断用 `reason_code`（稳定枚举），`reason` 是给人读的说明。

### `reconcile`：中断后的只读核实

```bash
agent-fs reconcile target.md --base-sha256 <Hbase> --desired-sha256 <Htarget>
```

只读比较三个哈希，不写目标、不回滚、不重放补丁，也不推断原调用是否结束。返回状态：

| status | 判定 | 退出码 | 含义 |
|---|---|---|---|
| `desired_present` | Hobserved = Htarget | 0 | 预期结果当前已存在，**禁止重复应用** |
| `base_present` | Hobserved = Hbase | 1 | 当前仍是原文，**不证明原调用从未执行** |
| `diverged` | 两者均不同 | 1 | 存在其他改动或部分编辑，需分析后重新准备 |
| `unavailable` | 无法取得稳定内容 | 3 | 无法核实，禁止自动创建、覆盖或重试 |

## 强制协议：写前写后

受保护主流程统一为四步（`verify → 外部写入` 不再是受保护流程）：

1. **稳定读取**：`read-fresh` 获取稳定原文与 Hbase（`sha256`）。
2. **准备修改并计算 Htarget**：整文件修改把最终内容落入内容文件并计算其哈希；多点编辑先 `edit-if-match --dry-run` 预览（同一套变换逻辑，返回 Htarget 与 edits 文件哈希）。分段读取只辅助理解，不能作为整文件覆盖内容。
3. **受控写入**：调用前保留恢复上下文（目标绝对路径、Hbase、Htarget、内容/edits 文件位置及其哈希，使用任务独占路径，结束后清理）；执行 `write-if-match --expected-sha256 <Hbase> --expected-result-sha256 <Htarget>`（多点编辑用 `edit-if-match` 同参数）。
4. **结果核实**：检查返回的 `status`/`commit_state`；`verified` 才算预期字节已存在（`new_sha256 == Htarget`），随后继续内容验收——文件哈希一致不能代替语法、业务或任务相关测试。

- 未传 `--expected-result-sha256` 时 CLI 自行计算 Htarget，基本写入功能保留，但调用方**不具备"响应丢失后仍知道预期结果"的完整恢复条件**——新 Skill 的受控主流程必须传。
- `--dry-run` 与实际写入之间可能发生其他修改；实际写入会重新校验 Hbase、编辑列表与结果哈希，不能复用预览通过的结论直接提交。
- **验收方**（主 Agent 核对子 Agent 成果时）：不复用子 Agent 报告里的旧哈希，必须独立执行一次 `read-fresh` 或 `snapshot`，与任务声明的 Htarget 比对一致后再判定成果有效，然后检查正文与测试结果。
- 多文件批量任务逐文件记录与恢复；单文件原子替换不等于多文件原子提交。

## 中断后的核实与恢复（概要）

调用超时、断线或响应缺失后：保留原 Hbase/Htarget → 从宿主确认原调用状态（`finished`/`cancelled_and_stopped` 才算结束证据；超时、已发送取消请求、PID 不可查询都**不算**）→ 原调用未结束时只允许只读核实，**不得启动第二个写入者** → 确认结束后按 reconcile 状态分流：desired_present 不重复应用；base_present 可用原 Hbase/Htarget 受控恢复（同一份材料默认最多一次恢复性重试）；diverged 读取最新内容分析后重新准备（保留他人改动，不能只换 expected_sha256）；unavailable 停止并报告。

换工具到本地 CLI 的条件：原调用已确认停止、目标在授权范围内、当前文件已稳定核实、修改材料完整且与预期哈希一致、受控命令可用。不满足时暂停该文件写入并报告缺失证据。

完整规则见 `references/interruption-recovery.md`；并发边界见 `references/concurrent-editing.md`。

## 操作台账：跨会话恢复证据

`write-if-match` / `edit-if-match` 进入写入流程的每次真实执行（verified/rejected/unknown 及执行期 error）会尽力追加一行到 `$AGENT_FS_HOME/ops.jsonl`：目标调用路径与真实路径（resolve 归一）、Hbase/Htarget/观察哈希、status/commit_state、reason_code、durability 与时间。dry-run 的 `prepared` 与 CLI 参数预检失败（invalid_sha256_format、material_unreadable 等 caller_error）不落台账。写入为单次原子追加，短写或序列化失败视为记录失败（recorded=false），**不影响主命令结果**；断电时最后一条记录可能缺失——台账是尽力记录的恢复证据，不是完整的中断操作日志。追加前会在台账协作锁内把无换行残尾截断丢弃（残片已判定为记录失败，不复活、不吞后续记录）。锁原语跨平台（POSIX flock / Windows msvcrt.locking）：协作写入者之间互斥（非阻塞有限重试，竞争超时返回 lock_busy，非竞争类错误返回 lock_failed），非 agent-fs 写入者不参与该锁，仍可能在检查与追加之间写入。两类锁原语都不可用的残余平台降级为无锁——协作写入者之间也可能互相截断删除已成功记录（pending 漏报），边界如实声明。台账路径优先以 O_NOFOLLOW 打开并校验为普通文件（预置符号链接拒绝记录）；O_NOFOLLOW 不可用的平台以 lstat 预检 + 无 O_CREAT 打开 + fstat 复核降级，仍存在预置软链的残余窗口（如实声明）。

- 查询：`agent-fs ops list [--pending] [--target PATH] [--limit N]`；`--pending` 按目标最新一条受控写入判定——最新行为 `status=unknown`（可能已写入但未核实）才算待恢复，同目标后续 verified 写入自然消解旧 unknown；`--target` 求行侧 {调用路径字面, 记录时真实路径} 身份集合与查询身份集合（调用串字面归一参与匹配，相对调用串跨 cwd 的同名路径视为同一调用身份，建议传绝对路径；绝对路径保留 `..` 语义按遇到顺序展开软链）的交集，先过滤再统计，pending_count 与 operations 同口径。pending 分组按记录时真实路径的归一化身份优先、调用路径字面回退；realpath 变化（软链改指）视为不同目标——原调用路径仍可检索历史记录；无有效身份的行不参与 pending。台账存在但不可读取（如权限）时，`ops list` 返回 error（退码 2，`ledger_unreadable`）并显式上报——不将其误报为「无待恢复操作」。
- 恢复动作：对 pending 目标执行 reconcile（只读核实），分流规则见上一节；不要依据台账行直接重试写入。
- 边界：台账是恢复的输入，不是文件状态的证明；旁路写入与后续覆盖会使行内容漂移，一律以 read-fresh / reconcile 的当次观察为准。

## 锁域身份边界（2026-10-31 实测）

锁键 = resolve 归一路径 + NFC + darwin casefold（Windows normcase）。实测：**符号链接别名共享同一把锁**（互斥成立）；**大小写别名在 darwin 共享**；**硬链接别名不共享锁**（resolve 不合并 inode 别名）——同 inode 不同路径各持一把锁，内容哈希 CAS 仍会拒绝基线已变化的写入，但两次并发写入之间的 check-then-replace 窗口不受协作锁保护。对硬链接路径做并发协作时，统一使用同一路径。

## 否定性结论的双重证据要求

得出受保护文件「没有变化」「某实现不存在」「字段未写入」之类结论前，至少需要两种、且针对**同一版本**的证据，例如：

- 稳定读取（`read-fresh` 返回 `stable: true`）+ 精确搜索（`grep`/`sed` 命中同一份内容）；
- 稳定读取 + `git diff`（Git 仓库内路径）。

当不同工具的结果互相矛盾时，不允许"挑一个相信"，必须重新执行 `read-fresh` 拿到新的稳定快照后再判断。

## 跨平台注意事项

- `inode` 在 Windows 部分文件系统上不具备稳定语义（可能恒为 0），`agent-fs` 已在比较逻辑中把 `inode` 降级为辅助信号，仅在双方均非 0 时参与判定。
- 快照存储路径基于 `pathlib.Path.home()`，不硬编码 `/tmp`，Mac/Linux/Windows 行为一致。
- 相同字节内容在不同操作系统上应计算出相同的 SHA-256。
- Windows 上目录 fsync 通常不支持：`durability` 会如实标注 `unconfirmed`，不虚构掉电保证。

## 错误处理

- `0`：成功且业务判定为稳定、匹配、未变化、已写入或预览成功。
- `1`：业务性拒绝（不匹配、已变化、不稳定、锁忙、锚点失配）；不是参数错误。
- `2`：程序或输入错误（文件不存在、无效 JSON、非 UTF-8、`invalid_sha256_format` 等）；停止当前写入流程，修正输入。
- `3`：写入结果未知；只进入核实流程，不自动重试。

分支判断用 `reason_code`（稳定枚举，完整映射见 `references/result-contract.md`），不要解析 `reason` 自然语言。**「写入失败应保留原文件」仅适用于确定发生于提交前的失败**（`status=rejected/error`、`commit_state=not_committed`）；`status=unknown` 时替换可能已发生，一律先核实。`durability=unconfirmed` 表示内容已验证但掉电持久化未确认。

reason 的五级归类见下方「异常上报协议」：`hash_mismatch`/`lock_busy`/`prepared_content_changed` 等是 `expected_block`（协议正常工作，不是 bug）；`file_not_found`/`invalid_sha256_format` 等是 `caller_error`（修正参数即可）；只有 `adapter_inconsistency`/`agent_fs_core` 才值得上报。 若需要结果契约字段或 reason_code 的完整分支语义：读 `references/result-contract.md`；若写入调用超时、断线或响应丢失：读 `references/interruption-recovery.md`；若遇到锁竞争与并发边界问题：读 `references/concurrent-editing.md`。

## Gotchas

- 调用异常（超时/断线/缺响应）**不证明"没有修改文件"**；结果未知时禁止同时启动第二个写入者。
- `verify` 不是 CAS，也不是受保护主流程的写入步骤；单次校验和写入用受控写命令。
- 协作锁只约束同样调用 agent-fs 的写入者；不能宣称全局互斥，更不承诺跨机器协调。
- 写后 `snapshot` 不能单独作为完成证据；与 Htarget 比较后才能认定预期字节已存在，内容质量另行验收。
- `read-fresh --offset/--limit` 的 `sha256` 是完整文件哈希，不是返回片段哈希；稳定读取失败时不得使用 `content` 作结论依据。
- `guard multiedit` 只预检不执行编辑；真正编辑用 `edit-if-match`。
- `reconcile` 发现 `desired_present` 也可能历史上执行过多次（哈希只能判断当前字节）；发现 `base_present` 不证明原调用从未执行。
- 遇到旧版 CLI：先用 `--help` 或版本证据确认能力，不调用不存在的命令，也不用旧版成功回执冒充新契约。
- 硬链接别名不共享协作锁（同 inode 不同路径各持一把锁）；台账行不是文件当前状态的证明，核实以 read-fresh / reconcile 为准。

## 与 Git 工作区隔离的关系

本 Skill 解决"读取新鲜度"、"受控写入"和"中断恢复"，不解决"多个 Agent 同时改同一个工作副本"的编辑期冲突——那部分职责由 `using-git-worktrees` Skill（每个写任务独立 worktree）承担。两者配合使用：写任务先按 `using-git-worktrees` 隔离工作目录，写入仍按本协议执行受控写入与结果核实；处理 worktree 之外的路径（非 Git 文件、配置文件、外部资源）时，本协议是唯一的保护手段。目录内的其他文件及工作区外部资源不因 worktree 而自动隔离。

## 当前能力与边界（如实声明，避免过度声明）

**当前提供的能力**：

- 平台分发：本 Skill 已注册到 `~/.catpaw/skills/agent-fs`（软链指向本包目录），CatPaw 的全局 Skill 发现机制能找到它；其他平台可在该平台自己约定的入口文件中手动引用本 Skill，属使用方可选接入方式。
- 核心命令：`snapshot`、`read-fresh`、`verify`、`changed`（stat→hash→stat 三明治稳定指纹）。
- 受控写入：`write-if-match`（整文件）与 `edit-if-match`（锁内多点编辑 + `--dry-run` 预览），均支持 `--expected-result-sha256`（Htarget 强校验）、稳定回读、提交事实与结果确认分离、durability 分级。
- `reconcile`：中断后的只读三哈希核实。
- 本地短时协作锁：`$AGENT_FS_HOME/locks/`（同一锁域 = 同一 AGENT_FS_HOME + 同一规范化目标路径 + 兼容锁实现）。**未提供**长时租约与跨机器锁。
- `events`：本地事件流（五级分类 + 指纹去重），配合异常上报协议。
- 操作台账：`ops list`（受控写入的跨会话恢复证据，pending 语义见「操作台账」一节）
- 纯预检 `agent-fs guard`：只检查不执行，不替代受控写入协议。

**当前未提供的能力**：

- 多 Agent 同时改同一工作副本的编辑期隔离（由 `using-git-worktrees` Skill 承担）。
- 自动续做、断电恢复、自动回滚、恰好一次执行保证（操作台账已提供跨会话恢复证据，但不构成完整的中断操作日志）。
- 跨机器锁协调、分布式互斥、实时字符级协作（CRDT/OT）、影子历史。
- Windows 替换语义与权限保真的单独验证；MCP 薄适配（2026-10-31 调研结论：一致性来自统一写入路径而非载体，待多客户端结构化调用需求出现时评估）。

## 异常上报协议（五级分类）

> 协议核心：**分类 → 本地记录 → 指纹去重 → 显式同步**。普通任务不自动
> 产生任何外部写入（学城/仓库 CHANGELOG 均不动）。

### 五级分类（先分类，再决定动作）

| 级别 | 含义 | 动作 |
|---|---|---|
| `expected_block` | 门禁正常阻断（verify 不匹配、锚点失配、lock_busy、prepared_content_changed、observed_mismatch） | **不上报**。这是协议在正确工作，向用户说明即可 |
| `caller_error` | 调用方参数/路径/转义/cwd 错误 | **不上报**。按 CLI 返回的 `next_action`/`suggested_command` 修正后重试 |
| `external_failure` | 构建、认证、浏览器、网络等外部依赖失败 | **根因归平台/Skill 排障；agent-fs 负责文件状态核实与安全恢复（reconcile）**。两者不互相替代 |
| `adapter_inconsistency` | 工具回执与独立文件证据矛盾（称成功但哈希未变，或反之） | 先核实；具备复现证据后记入本地事件流，**可**同步学城 |
| `agent_fs_core` | 稳定读取、受控写入、锁或哈希契约**可复现地**失效 | 记入本地事件流，**可**同步学城（P0/P1） |

异常根因、文件状态和恢复动作分开记录：网络错误可以是 `external_failure`，同时文件状态为 `unknown`，恢复动作是 `reconcile`。文件丢失、持续不稳定的分类需要阶段和环境证据；不确定的原因保留未定性，文件状态仍按结果契约明确表达。

### 本地事件流（唯一默认记录通道）

- 命令内部异常已自动记录到 `~/.agent-fs/events.jsonl`（含五级分类、指纹去重、file_state/recovery_action）。
- 查询：`agent-fs events list [--syncable] [--since ISO-8601] [--limit N]`。`--since` 只保留该时间点及之后的事件（按秒精度比较，非 UTC 或带时区偏移的时间会被归一化），`--limit` 在排序后截断；输出中的 `total` 是过滤后的总数，`count` 是本次返回条数。
- Agent 主动记录：`agent-fs events list --syncable` 查看待同步事件；需要新增事件时在对话中告知用户（不自动写文件）。
- **禁止**在普通任务中自动修改 `tools/agent-fs/CHANGELOG.md`；该文件仅人工维护。

### 学城同步（显式触发，用户确认）

只有 `adapter_inconsistency` 与 `agent_fs_core` 级别的事件，在**用户明确要求同步**时才写入学城 [《agent-fs 已知问题汇总》](https://km.sankuai.com/collabpage/2779500222)：

1. `agent-fs events list --syncable` 获取待同步清单
2. 向用户展示聚合表（指纹/类别/次数/首末次时间）
3. 用户确认后按 doc-toolkit 上报格式写入（`doc-toolkit km doc patch`）
4. 学城页只保留「开放问题聚合表」结构，原始事件留在本地 jsonl 归档，**不把 KM 当日志文件**

## 参考

- 完整背景与四阶段落地设计：仓库开发态维护文档 `knowledge/superpowers/specs/2026-08-04-agent-file-collaboration-unified-design.md`（不随本 Skill 安装分发，安装态无需读取）
- 可靠编辑 SPEC（本协议的契约来源）：仓库开发态维护文档 `knowledge/superpowers/specs/2026-09-29-agent-fs-concurrency-interruption-recovery-design.md`（不随本 Skill 安装分发，安装态无需读取）
- 结果契约字段与 reason_code 映射：`references/result-contract.md`
- 并发保护前提与锁域边界：`references/concurrent-editing.md`
- 中断分流、恢复决策与换工具条件：`references/interruption-recovery.md`
- 错误五级归类判定细节：`references/error-taxonomy.md`
