# agent-fs

多 Agent / 多人 / 多脚本文件协作的**可靠性协议 + 本地 CLI**：稳定读取、写前校验、受控写入、中断恢复。

> 多个 Agent、脚本和人工可能同时操作同一份工作区。一个 Agent 读到旧版本后，若没有重新校验就据此写入或下结论，会静默覆盖别人已写入的内容，或给出已经过时的否定性结论（「文件没变化」「字段不存在」「为空」）。编辑工具调用还可能超时、断线或丢失响应——此时**调用异常不等于没写入**。agent-fs 用 `stat → hash → stat` 三明治校验 + 哈希 CAS 语义解决这类 TOCTOU 问题，纯标准库实现，零第三方依赖。

## 核心能力

| 能力 | 命令 | 说明 |
|---|---|---|
| 稳定快照 | `snapshot` | 三明治校验采集稳定指纹，落盘供跨会话比对；支持 `prune` 清理 |
| 新鲜读取 | `read-fresh` | 读取前后双 stat + 内容哈希，不稳定则重试/返回冲突；`sha256` 始终是完整文件哈希 |
| 诊断校验 | `verify` | 核对独立证据（哈希比对），不是受保护写入步骤 |
| 受控写入 | `write-if-match` / `edit-if-match` | 锁内「基线校验 → 原子替换 → 稳定回读」，支持 Htarget 强校验与 `--dry-run` 预览 |
| 中断核实 | `reconcile` | 中断后的只读三哈希核实，绝不自动重试写入 |
| 变更检测 | `changed` | 相对快照检测文件变化 |
| 纯预检 | `guard` | git-commit / git-object / pathspec / worktree / text-write / multiedit 预检，只检查不执行 |
| 操作台账 | `ops list` | 受控写入的跨会话恢复证据，pending 语义驱动恢复分流 |
| 事件流 | `events list` | 本地五级分类 + 指纹去重的事件记录 |

## 安装

`src` 布局，核心零第三方依赖（仅标准库），Python ≥ 3.9，Mac / Windows / Linux 通用：

```bash
SKILL_DIR=/absolute/path/to/agent-fs
python3 -m venv .venv-agent-fs
. .venv-agent-fs/bin/activate
python3 -m pip install -e "$SKILL_DIR"
agent-fs --help
```

## 快速上手

```bash
# 1. 稳定读取，获取基线哈希 Hbase
agent-fs read-fresh target.md

# 2. 受控写入：基线匹配才替换，并强校验结果哈希
agent-fs write-if-match target.md \
    --expected-sha256 <Hbase> \
    --expected-result-sha256 <Htarget> \
    --content-file /tmp/new-content.txt

# 3. 多点编辑：先 dry-run 预览获得 Htarget，再实际写入
agent-fs edit-if-match target.md --expected-sha256 <Hbase> \
    --edits-file edits.json --dry-run
agent-fs edit-if-match target.md --expected-sha256 <Hbase> \
    --expected-result-sha256 <Htarget> --edits-file edits.json

# 4. 调用超时/断线后：只读核实，按状态分流恢复
agent-fs reconcile target.md --base-sha256 <Hbase> --desired-sha256 <Htarget>
```

## 退出码契约

| 退出码 | 含义 | 允许的后续动作 |
|---|---|---|
| `0` | 成功（稳定 / 匹配 / 未变化 / 已写入 / 预览成功） | 继续内容验收 |
| `1` | 业务性拒绝（不匹配 / 已变化 / 锁忙 / 锚点失配） | 核实现状，重读后重新准备 |
| `2` | 程序或输入错误 | 修正原因；再次写入前重新校验 |
| `3` | **写入结果未知** | **只进入核实流程（reconcile），不自动重试写入** |

所有命令输出结构化 JSON 到 stdout；分支判断用 `reason_code`（稳定枚举），不解析自然语言 `reason`。

## 边界（如实声明）

- 协作锁只约束同样调用 agent-fs 的写入者，**不承诺全局互斥**；编辑器、`sed` 等旁路写入不参与该锁。
- 不提供跨机器锁协调、分布式互斥、实时协作（CRDT/OT）、自动回滚。
- 多 Agent 同时改同一 Git 工作副本的编辑期隔离由 worktree 隔离承担；本协议负责读取新鲜度、受控写入与中断恢复，两者配合使用。
- 操作台账是尽力记录的恢复证据，不构成完整的中断操作日志。

## 文档

- `SKILL.md` —— 完整协议：触发条件、强制流程（写前写后四步）、中断恢复、锁域身份边界、错误五级分类
- `references/result-contract.md` —— 结果契约字段与 reason_code 完整映射
- `references/concurrent-editing.md` —— 并发保护前提与锁域边界
- `references/interruption-recovery.md` —— 中断分流、恢复决策与换工具条件
- `references/error-taxonomy.md` —— 错误五级归类判定细节

## 测试

```bash
python3 -m pytest   # pyproject.toml 已配置 pythonpath = ["src"]
```

## License

暂未声明开源协议（私有技能库用途）。如需复用请先与作者确认。
