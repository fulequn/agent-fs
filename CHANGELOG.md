# Changelog

本文件记录 `agent-fs` 的版本变更，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.2.0] - 2026-10-22 并发边界与契约收敛：输出覆盖、锁域、事件写入与可运维性

执行《agent-fs 优化方案：并发边界、结果契约与可运维性》（学城 `2793059160`）的
P0、P1 全量与 P2 主要项。本批不含 `read-fresh` 全量读文件的内存优化和
actor/op-id 传递（保留在后续迭代）。

### 修复（P0/P1）

- **[P0] `read-fresh --output` 不再覆盖源文件**：写输出前做同路径判定
  （`resolve()` 相等或 `os.path.samefile`，覆盖符号链接与硬链接别名），命中返回
  `output_path_conflict`（caller_error / exit 2）并提示改用 `write-if-match`；
  输出改为同目录临时文件 `O_EXCL` 创建 + `os.replace` 原子落盘，目标已有普通权限位保留，
  不再可能留下半截文件。源文件在 `--offset/--limit/--max-bytes` 组合下哈希不变。
- **[P1-1] 事件写入统一为 append-only**：`record_event` 只追加单行，不再执行
  "读全量 → 按指纹合并 → 重写"；同指纹聚合移到读取端（`load_events` 累加 `count`、
  取最大 `last_seen`），写入结果以 `deduplicated_at_read` 标记。按指纹压缩收敛到
  显式 `compact_events`（持锁、`O_EXCL` 临时文件、原子替换），锁不可用时返回
  `compacted=false` 而不是冒险重写。追加路径与压缩路径不再混用，避免互相覆盖丢事件。
- **[P1-2] 锁域适配大小写不敏感文件系统**：macOS（APFS 默认）按 `casefold` 归一化
  锁键，同时保留旧键兼容；`Foo.txt` 与 `foo.txt` 指向同一把锁，不再出现两份并发写。
- **[P1-3] Windows 锁文件不再每次取锁增长**：取锁/解锁前 `seek(0)` 写回同一个字节再锁，
  锁文件稳定为 1 字节，长期使用不再持续膨胀。
- **[P1-4] 临时文件独占创建**：`write-if-match`/`edit-if-match`、快照、压缩与
  `read-fresh --output` 的临时文件统一 `O_CREAT | O_EXCL` + 随机名，共享目录中预置的
  同名文件或符号链接无法被复用/劫持。
- **[P1-5] 权限与脱敏闭环**：快照目录与事件锁目录收紧到 `0700`、文件 `0600`；
  `record_event` 在单一入口对 `path`/`detail`/`normalized_error` 统一脱敏，调用方直接
  构造事件也无法绕过；补充 JSON 引号键、Cookie 多值、Windows 路径等泄漏形态。
- **[P1-6] 快照身份校验**：`verify`/`changed` 使用 `snapshot_id` 时校验记录中的
  `path` 与目标一致，不一致返回 `snapshot_path_mismatch`（caller_error / exit 2），
  快照记录缺字段或损坏返回 `snapshot_corrupted`，不再跨文件误用快照。
- **[P1-7] guard / dry-run / 实际写入共用一套编辑语义**：新增 `agent_fs.edits`
  （`load_edits` + `apply_edits`）作为唯一实现，三处都按"第 N 个编辑基于前 N-1 个的结果"
  顺序应用、按最新内容重判锚点唯一性、`old == new` 与空锚点显式拒绝、区间重叠只告警；
  失败时统一给出 `edit_index` 与失败原因，不再出现预检放行而写入拒绝。
- **[P1-8] 解锁异常不再掩盖成功结果**：释放多把锁时逐把释放，单把失败继续释放其余，
  最终异常记入 `ancillary_failures`，`verified/committed` 结论保持不变。
- **[P1-9] 补 CI 矩阵**：新增 `.github/workflows/agent-fs-ci.yml`（3 OS × Python
  3.9/3.13 跑 ruff + pytest + 构建）。

### 改进（P2）

- 锚点重复诊断补齐 `occurrences`、`positions` 与 1-based `lines`，`detail` 同步给出
  行号；guard 失败统一带 `next_action`。
- 只读命令契约统一：`snapshot`/`verify`/`changed`/`guard` 输出 `next_action`；
  `read-fresh` 的负 `offset/limit`、路径缺失、非普通文件按 `caller_error` exit 2 返回，
  不再落到 exit 1；`--max-bytes` 帮助明确单位为字节。
- 空锚点与 `old == new` 在 result 契约中显式建模为 `empty_anchor` /
  `old_and_new_identical`（error / not_committed / exit 2，caller_error）。
- 新增 `agent-fs snapshot prune`：必须显式给 `--older-than` 或 `--keep-last`，
  `--keep-last` 按目标文件分组保留最新 N 份，两条件取交集，支持 `--dry-run`；
  损坏快照不参与分组也不会被删除。
- `agent-fs events list` 新增 `--since`（ISO-8601，归一化到 UTC 秒）与 `--limit`
  （排序后截断，`total` 为过滤后总数、`count` 为返回条数）。
- 元数据保留边界：写入与 `--output` 只继承普通权限位（`0o777`），不再继承
  setuid/setgid/sticky。

### 文档

- `SKILL.md`：补充 `snapshot prune`、`events list --since/--limit`、`--max-bytes` 单位、
  `--output` 权限语义与顺序编辑规则。
- `references/error-taxonomy.md` / `references/result-contract.md`：同步
  `empty_anchor`、`old_and_new_identical`、`invalid_offset_or_limit`、`output_path_conflict`
  等新码，并把区间重叠标注为历史事件码（现仅告警）。

### 验证

- `python3 -m ruff check .` 通过（Python 3.9 兼容）。
- `python3 -m pytest -q`：297 passed / 2 skipped（跳过项为平台不支持的 setuid 用例；
  4 条告警来自既有 macOS 临时目录清理）。
- CI 工作流文件未在本地执行；仓库远端为 GitLab，GitHub Actions 需镜像仓库后才能跑。

## [未发布] - 2026-10-08 复审补齐：兜底统一结构、权限读取阻断与空 Htarget 拒绝

复审（2026-10-08，384b65f 之后故障注入）发现 3 个 P2 与 1 个 P3，本批全部修复。

### 修复

- **[P2] 兜底异常结果改用统一结构并保留调用上下文**：新增调用上下文登记
  （`result.record_call_context`：main 解析后记命令名/路径，命令函数校验后补记
  Hbase/Htarget）；`main()` 兜底对 write-if-match / edit-if-match 复用
  `WriteOutcome`——`command` 不再误报 `unknown`，`path`/三个哈希/`reason`/
  `durability` 全量输出（`unhandled_post_commit` 时 `durability=unconfirmed`），
  调用方可凭响应直接进入 reconcile。读取类命令兜底同步补记 command/path/reason/durability。
- **[P2] 读取原权限失败不再提交**（新码 `mode_read_failed`，error/not_committed/2）：
  旧实现 stat 失败时跳过 chmod，文件以临时文件 0600 权限落地（0755 丢失可执行位）
  却返回 verified；现在读取失败即阻止替换，原文与权限保留。权限读取独立为
  `_read_original_mode`（故障注入唯一入口，与 `_stable_readback` 同模式）。
- **[P2] 显式传入空 Htarget 不再静默跳过校验**：`--expected-result-sha256 ''`
  从真值判断（空串=未声明）改为 `is not None` 判断——空字符串按
  `invalid_sha256_format`/exit 2 拒绝并保留原文（write-if-match 与 edit-if-match
  同口径），错误消息提示"不校验请省略该参数"。
- **[P3] 参数错误帮助命令前缀重复修正**：`command`/`suggested_command` 改由
  `_JsonArgumentParser.error` 从子解析器 prog 剥离子命令名——不再输出
  `agent-fs agent-fs --help`（旧实现在 main 里用根 parser.prog 二次拼接）。

### 验证

- 全量测试 236 passed（含本轮 6 个回归用例：兜底统一结构提交前/后、空 Htarget
  write/edit 拒绝、mode 读取阻断、帮助命令前缀）；Ruff 通过（Python 3.9 环境）。

## [未发布] - 2026-10-02 审查修复：提交事实判定、稳定指纹、权限保持与契约补全

外部审查（2026-10-02，fea96d0 版本）发现 6 个可复现问题与 1 项 3.9 兼容缺陷，本批全部修复。

### 修复

- **[P1] 提交后异常不再误报"未提交"**：`_fsync_path` 的 open/fsync/close 任一环 OSError
  收敛为分类结果（不再向外抛）；删除 `cmd_write_if_match` 不辨阶段的局部异常捕获，
  统一由 main() 按执行阶段分流——目录同步 close 失败（替换后）现在返回
  verified/unconfirmed，而非 error/not_committed。
- **[P1] 写路径基线与替换前复核改用稳定指纹**（stat-hash-stat 三明治）：
  哈希采集期间被并发修改不再漏判；基线/复核不稳定返回 `target_unstable`（rejected/1，目标不动）。
- **[P1] 临时文件以受限权限 0600 创建，chmod 失败阻止提交**（新码 `tmp_chmod_failed`，
  error/not_committed/2）：0600 原文件不再可能被替换成 0644 扩大访问范围。
- **[P2] edits 材料单次读取**：解析与材料哈希来自同一份字节，不再产出
  "旧材料算出的 Htarget + 新材料的指纹"这类不一致恢复证据。
- **[P2] 参数错误补齐 schema-2 必需字段**：`argument_error` 输出
  expected/desired/observed/previous_sha256（无法取得为 null）、`durability`、`written`。
- **[P2] 哈希统一小写归一化**：CLI 全入口校验后 `.lower()`，write/edit/reconcile 核心
  入参防御性归一化——大写 Htarget 不再使 reconcile 误报 diverged。
- **[C] Python 3.9 兼容**：`result.py` 模块级类型别名 `_R` 从 `str | None`（PEP 604，需 3.10）
  改为 `Optional[str]`；已用 Python 3.9 实际运行全量测试验证。

### 验证

- 全量测试 229 passed（含本轮 8 个回归用例：close 错误收敛、chmod 阻断、0600 权限保持、
  不稳定基线/复核拒绝、材料单次读取、大写哈希、错误响应字段全量）；Ruff 通过。
- Python 3.9 运行验证：227 passed / 2 skipped（跳过项为 3.9 临时环境下 subprocess
  找不到 `build` 模块的 wheel 端到端用例）。
- 平台覆盖：macOS（arm64）实测；Windows 未实测。

## [未发布] - 2026-09-30 可靠编辑第一期：schema-2 结果契约、Htarget 校验与中断核实

> **异常自动上报本地留痕区**（自 2026-08-04 起）：Agent 在使用 `agent-fs` 协议或编辑文件过程中自动发现并上报到学城文档的问题，会在本文件顶部新增一个专门的 `## 异常自动上报记录` 区块追加一行记录（日期/场景摘要），与正式版本发布记录区分开，方便集中回顾所有自动上报历史，不需要每次都去学城翻。

## 异常自动上报记录

> 本区块仅作本地留痕，实际问题详情以学城文档 [《agent-fs 已知问题汇总》](https://km.sankuai.com/collabpage/2779500222) 为准，本处只保留时间线索引，不重复维护内容。

> 以下记录以远端学城文档为事实源，于 2026-08-20 回填。远端共 14 条，
> 其中 2 条因内容损坏无法可靠还原，已标记为「内容损坏」。
>
> 远端分析方案同时指出以下已解决问题，此处记录闭环状态：
> - **「CHANGELOG.md 不存在」**（2026-08-14 上报）：现已解决。文件存在且可写，
>   见本文件本身及 `test_changelog_exists` 测试用例。
> - **命令漂移 `doc-toolkit doc patch` → `doc-toolkit km doc patch`**：现已修复。
>   SKILL.md 已统一为 `doc-toolkit km doc patch`，见 `test_no_legacy_command_drift` 测试用例。

| 日期 | 场景摘要 | 状态 |
|---|---|---|
| 2026-09-15 | 最终产物汇总脚本误用 zsh 特殊变量 path 覆盖 PATH，导致 find/wc/tr 不可用并退出 127；改名 artifact_dir 后重验 | 已上报 |
| 2026-09-15 | 解析 master 的 2.2 MB pnpm-lock 时 Node execFileSync 默认缓冲区不足并报 ENOBUFS；提高 maxBuffer 后重验 | 已上报 |
| 2026-09-15 | 锁文件 importer 最终校验中根目录缺 yaml 模块，且组合脚本未 set -e 导致中间失败被掩盖为退出 0；改用真实模块并 fail-fast 重验 | 已上报 |
| 2026-09-15 | MRN 首次构建被 cang-lan 生成 dist 的同名 package.json 干扰，日志报 DuplicateError 却退出 0；隔离产物后 Android/iOS 重构建成功 | 已上报 |
| 2026-09-15 | Node 16 独立构建 me-pc-vue 成功并生成 18 个文件，但 i18n:sync 再次改写 24 个共享语言包/类型文件；仅恢复生成变更 | 已上报 |
| 2026-09-15 | 检查 H5 产物时误用仓库根 build/me-h5-react，find/du 路径不存在；改用 packages/me-h5-react/build/me-h5-react | 已上报 |
| 2026-09-15 | Node 16 独立构建 me-h5-react 成功，但 i18n:sync 再次改写 24 个共享语言包/类型文件；仅恢复生成变更 | 已上报 |
| 2026-09-15 | 移除 jibai-landing 后排除 PC 包并以 Node 16 复验，MAX 微信侧完成但 Web 侧仍因既有 class properties 转换错误非零退出 | 已上报 |
| 2026-09-15 | 全仓构建的 me-pc-vue i18n:sync 改写 24 个共享语言包/类型文件；确认均由本次验证产生，仅恢复这些路径 | 已上报 |
| 2026-09-15 | 移除 jibai-landing 后跑全仓 build:all，在 me-pc-vue 的既有 useCounter Hook ESLint 处失败；继续与 master 对比归因 | 已上报 |
| 2026-09-15 | CDP 已见 Dev 目标 cookie，但 mpr 仍主动拒绝并报告缺 cookie；只读失败，转同源官方 API 内存认证验证 | 已上报 |
| 2026-09-15 | 默认浏览器打开 Dev/OIDC 后用 mpr list 校验仍缺 f32a546874 cookie；只读失败，继续定位具体浏览器/Profile | 已上报 |
| 2026-09-15 | 按官方 mtsso Skill 探测 PR 换票能力时本机 MOA WSS 连接超时；未获得 token，转系统默认浏览器完成 Dev/OIDC 登录 | 已上报 |
| 2026-09-15 | 读取 mpr 认证实现时，组合命令后半段误读不存在的 meituan/sso/requests.py；关键 CookieManager 已读到，后续改由 import 定位 | 已上报 |
| 2026-09-15 | 定位 mpr 模块时按常见 venv 布局检索的 site-packages/meituan 不存在；未改文件，改由工具自带 Python 输出真实模块路径 | 已上报 |
| 2026-09-15 | 诊断 hotel-me-ai-assistant PR 工具认证时，zsh 将未命中的 Python 路径通配符提前拒绝；未改文件，改用 rg --files 定位安装路径 | 已上报 |
| 2026-09-15 | 创建 hotel-me-ai-assistant PR 时 catdesk-browser 因 Dev 站点证书链无效拒绝导航；远端分支无损，转内部 PR 接口或证书信任路径 | 已上报 |
| 2026-09-15 | 用 mpr 创建 hotel-me-ai-assistant PR 时缺少账号 cookie；远端分支无损，转 OIDC/SSO 登录后重试 | 已上报 |
| 2026-09-15 | 创建 hotel-me-ai-assistant PR 时 mtdev pr 子命令与 Skill 声明不一致；分支已推送，改用同包 mpr 别名继续 | 已上报 |
| 2026-09-15 | 验证退役 landing 物理目录时发现基线安装/构建遗留 38 MB 忽略产物；精确预览后仅清理该目录 | 已上报 |
| 2026-09-15 | 移除 landing 后验证 workspace 时，pnpm list --depth -1 返回成功却未列出 workspace；改用递归列表并结合锁文件与精确搜索验收 | 已上报 |
| 2026-09-15 | 批量修改根 package.json 与两篇历史发布文档时，apply_patch 因 Markdown 上下文缺少补丁前缀而完整拒绝；重新读取并拆分补丁 | 已上报 |
| 2026-09-15 | 在隔离 worktree 移除退役 landing 时，git rm 被基线构建改写的 3 个生成文件拦截；确认删除范围后改用精确路径强制删除 | 已上报 |
| 2026-09-14 | 验收 NoCode SEO 控制台时，批量 apply_patch 因 Settings.tsx 重复 catch 锚点与实际内容不一致而整体拒绝；已重读并拆分补丁 | 已上报 |
| 2026-09-14 | 既白官网部署后使用产品内置网页读取器验证两个公开域名时均被 URL 安全策略拒绝；改用只读 curl 完成线上验收 | 已上报 |
| 2026-09-14 | 第五次复审 skill-toolkit 时发现无效 YAML 的测试说明声称保留物理身份，实际却从 entries 移除且 `real_path` 为空，clean inventory 无法覆盖 | 已上报 |
| 2026-09-14 | 第五次复审 skill-toolkit 时发现 JSON v1 虽锁定历史六字段，却重新输出 `clients=[]` 的无效 Skill，字段兼容但集合语义相对上一版回退 | 已上报 |
| 2026-09-14 | 第五次复审 skill-toolkit 时发现目录软链安装被记录为 `is_symlink=false`、`discovered_kind=symlink-directory`，导致 clean preflight 拒绝所有常见软链删除 | 已上报 |
| 2026-09-14 | 第五次复审 skill-toolkit 时发现 clean 虽记录跨客户端别名但删除计划与 preflight 不检查，真实共享目录仍获准 `rmtree`；同一客户端多软链也会被提前去重漏报 | 已上报 |
| 2026-09-14 | 第四次复审 skill-toolkit 时发现 Claude profile 仍与官方校验器有 4 个边界判定差异，且无效客户端入口在登记 `dup_map` 前被跳过导致清理别名漏报 | 已上报 |
| 2026-09-14 | 复审 skill-toolkit 的 PyYAML 依赖声明时，未加引号的 `requirements*.txt` 被 zsh 作为空 glob 拒绝；改用精确检索完成核查 | 已上报 |
| 2026-09-14 | 第三次复审 skill-toolkit 时发现严格校验仅在 `track_invalid=True` 时生效、审计指南 JSON v1 隐藏无效项，且 Codex 校验白名单被跨客户端复用 | 已上报 |
| 2026-09-14 | 再审 skill-toolkit 时发现 JSON 输出无版本改形、自带打包校验器与官方契约漂移、`invalid_entries` 仍漏报部分宿主无效入口 | 已上报 |
| 2026-09-14 | Skill 优化复审的结构分类辅助校验沿用已迁移的旧脚本路径并报文件不存在；通过 `rg` 定位到 `audit/scripts/classify_skill.py` 后重验 | 已上报 |
| 2026-09-14 | 复审 Skill 优化提交时，官方校验仍检出嵌套真实入口的顶层 `version`，新增仓库级测试因漏扫 `modules/*/SKILL.md` 产生假绿 | 已上报 |
| 2026-09-14 | 审核 Skill 优化批次 2 时，官方 `quick_validate.py` 检出 4 个新增无效 YAML frontmatter，并发现 doc-toolkit 的既有 `version` 键不合契约 | 已上报 |
| 2026-09-14 | 按真实路径分类冲突 Skill 时，批量 `realpath` 混入 5 个推测但不存在的安装路径；改用盘点输出的 `discovered_path` | 已上报 |
| 2026-09-14 | 审计重复 Skill 文件时 `shasum -a 256` 输出 Perl locale 警告；哈希仍有效，后续改用不依赖该 locale 的比较方式 | 已上报 |
| 2026-09-14 | skill-toolkit description 边界测试首次 GREEN 因“各客户端”与“多客户端”术语不一致仍有 1 个失败；统一措辞后重验 | 已上报 |
| 2026-09-14 | 核对 skill-toolkit 包配置时尝试读取不存在的 `skills/skill-toolkit/pyproject.toml`；改用现有测试配置继续 | 已上报 |
| 2026-09-14 | skill-toolkit 修复留痕补丁误重放两条既有操作，学城页出现重复 note；已基于最新 XML 仅删除重复节点并回读确认 | 已上报并修正 |
| 2026-09-14 | skill-toolkit 验证中两次只读检索误用推测文件路径；改由 `rg --files` 定位真实文档与测试文件 | 已上报 |
| 2026-09-14 | 读取本文件时误用 `agent-fs read-fresh --max-bytes`；查阅帮助后改用 `--limit` | 已上报 |
| 2026-09-14 | 追加异常记录时误用 `doc-toolkit km doc patch --patch-file`；改用位置参数后成功 | 已上报 |
| 2026-09-14 | 写前校验误用 `agent-fs verify --sha256`；改用 `--expected-sha256` 后通过 | 已上报 |
| 2026-09-14 | skill-toolkit tests 目录过滤修复的完整 pytest 验证暴露 16 个既有 fixture 缺失失败；专项测试不受影响 | 已上报 |
| 2026-09-14 | skill-toolkit 测试失败诊断命令误用 zsh 只读变量 `status`，组合命令提前中止；更换变量后重试 | 已上报 |
| 2026-09-13 | OpenWork 样式优化中编排脚本与首次异常补丁格式错误；未写入项目文件，学城自动上报因远端 XML 响应为空未推送 | 本地留痕，待补推 |
| 2026-09-11 | OpenWork 问既白推荐区设计文档提交前检查发现文件末尾多余空行；修正并重新暂存后通过 | 已上报 |
| 2026-09-11 | OpenWork 流失酒店名修复提交前检查发现设计文档末尾多余空行；修正并重新暂存后通过 | 已上报 |
| 2026-09-11 | OpenWork 流失酒店名修复中首次自动上报时远端 XML 响应为空；只读确认后重试成功 | 已上报 |
| 2026-09-11 | OpenWork 流失酒店名修复中误将预期哈希作为 agent-fs verify 位置参数；改用 --expected-sha256 后校验成功 | 已上报 |
| 2026-09-11 | OpenWork 流失酒店名修复中误传 agent-fs read-fresh 的 --format 参数；查阅帮助后改用 snapshot | 已上报 |
| 2026-09-11 | OpenWork 流失酒店名修复中在包目录执行类型检查触发 ENOWORKSPACES/nvm prefix 冲突；改从仓库根按包过滤后通过 | 已上报 |
| 2026-09-11 | 同行房价修复分支推送前验证时，全局 Maven 缺失且 git-commit-id-plugin 无法解析 linked worktree HEAD；改用 Maven Wrapper 并跳过该插件后测试通过 | 已上报 |
| 2026-09-11 | 安全 rebase 后远端新增 Supabase client 但漏声明依赖，Vite 无法解析；补齐相邻项目同版本依赖后构建恢复 | 已上报 |
| 2026-09-11 | 既白官网构建期 Dyna 同步完成后推送 NoCode 固定分支时，远端出现并发提交导致 non-fast-forward；停止强推并转为 fetch 后安全整合 | 已上报 |
| 2026-09-09 | OpenWork 同行价格迷你趋势计划自检时，rg 将以 `-24h` 开头的搜索模式识别为参数；添加 `--` 后重新检查 | 已上报 |
| 2026-09-09 | 更新搜狗验证码时校验参数格式错误、NoCode 不接受短 commit ID，异常记录补丁又因上下文不准确被拒绝；重读后使用完整 ID 完成部署和记录 | 已上报 |
| 2026-09-09 | 既白官网站长验证标签修改前，人工复制 SHA-256 时传入截断值导致 verify 拒绝；重新 read-fresh 后使用完整哈希完成校验 | 已上报 |
| 2026-09-09 | OpenWork“昨日订单流失”实施期间共享页面测试被并发改写；稳定快照确认最新内容并改用精确范围验收，未覆盖协作者改动 | 已上报 |
| 2026-09-09 | OpenWork 页面修复提交前误用 git diff revision 与不存在的 workspace vitest script；改用明确 pathspec 和 packages/app 下 pnpm exec vitest 完成验收 | 已上报 |
| 2026-09-09 | MDP 项目 Mafka Consumer 回退验证中，全局 Maven 缺失且 git-commit-id-plugin 无法解析 linked worktree HEAD；改用 Maven Wrapper 直接 compiler:compile 完成验证 | 已上报 |
| 2026-09-09 | OpenWork 通过 Electron CDP 重启 sidecar 时首次携带 Origin 被 Chromium 403 拒绝；改用无 Origin 本地连接后成功 | 已上报 |
| 2026-09-09 | OpenWork server 的 Bun 测试误用 pnpm exec vitest，因未安装 Vitest 而未执行；改用 bun test 并修订计划 | 已上报 |
| 2026-09-09 | OpenWork 门店保存修复计划误用不存在的 pnpm workspace 包名，测试未执行但 pnpm 返回 0；改为包目录直接运行并核验用例收集 | 已上报 |
| 2026-09-09 | OpenWork 门店竞态修复中，apply_patch 因手写上下文一字不符被完整拒绝；read-fresh 后继续 | 已上报 |
| 2026-09-09 | OpenWork 验收时在 packages/app 子目录误用仓库根相对路径，snapshot 返回 file_not_found；回到仓库根重验成功 | 已上报 |
| 2026-09-09 | OpenWork 新同行页面修复中，verify 后并发修改导致组合 apply_patch 上下文失效；补丁完整拒绝，重读并拆分后继续 | 已上报 |
| 2026-09-08 | OpenWork 新同行走势组件 TDD 中，MultiEdit 因包含 old/new 相同的无效替换整体拒绝；学城自动上报拉取 XML 响应为空未推送 | 本地留痕，待补推 |
| 2026-09-07 | OpenWork 预算保存 ACK 修复写前校验时，agent-fs CLI 入口漂移到缺少 agent_fs 模块的 miniforge 安装；使用显式 PYTHONPATH 完成校验 | 已上报 |
| 2026-09-07 | OpenWork queryPeerRoom 路由与流失订单展示 TDD 中，string_replace/MultiEdit 对已验证唯一锚点间歇返回 apply_error 或未找到；已改用逐项重读与原子编辑完成并上报 | 已上报 |
| 2026-09-03 | 同行分析实时房型快照 SPEC 验收编排中，JavaScript 模板字符串误包含 Markdown 反引号导致脚本解析失败；未触碰目标文件，改用安全参数重新验收 | 已上报 |
| 2026-09-02 | 学城接口文档验收时，CatDesk 对已加载页面执行截图返回 `Page.captureScreenshot` CDP 超时；已用 XML 两次回读和 DOM `rowspan` 校验完成验收，并已上报 | 已上报 |
| 2026-09-01 | 核查酒店订单流失接口归属时，mtdev Skill 声明的 `code` 子命令缺失，且 repo CDP 自动启动缺少浏览器；已上报 | 已上报 |
| 2026-09-01 | 酒店 ROCS Thrift 接口文档 1.3 响应表合并前的结构校验脚本错误假设固定 5 列，实际为保留完整字段信息采用 6 列；修正校验器后通过，已上报 | 已上报 |
| 2026-09-01 | MultiEdit 返回已应用但 agent-fs changed/read-fresh 对同一物理路径显示本地 CHANGELOG 未变化；异常已上报，随后改用物理路径受控写入补齐 | 已上报 |
| 2026-08-31 | OpenWork 酒店情报采集时间契约 TDD 中，OCC 问既白测试的临时 guard 配置错误转义选择器双引号，预检未命中；已重新读取并用准确锚点完成修改，已上报 | 已上报 |
| 2026-08-31 | OpenWork 酒店情报采集时间契约续作中，对推测存在的 OCC 日历独立测试文件执行 snapshot 返回 `file_not_found`；确认仅有 Shell 集成测试覆盖，已上报 | 已上报 |
| 2026-08-31 | OpenWork 数据就绪验证中，计划指定 `bunx vitest run` 执行 `bun:test` 用例而报模块未找到；改用 `bun test` 后通过，已上报 | 已上报 |
| 2026-08-31 | 酒店 ROCS Thrift 接口文档改版时，`agent-fs verify` 不接受 snapshot ID，仅接受 `--expected-sha256`；已按正确参数复核并上报 | 已上报 |
| 2026-08-31 | OpenWork 市场与竞争数据就绪 TDD 实施中，本机缺少 `rg`，历史侧栏空态检索改用标准命令；已上报 | 已上报 |
| 2026-08-31 | OpenWork 酒店情报 UI 重构回归修复中，MultiEdit 使用已过期的 Shell 测试文案锚点未命中；已重新读取确认并完成学城上报 | 已上报 |
| 2026-08-28 | jibai-landing testimonial 轮播验证时，TypeScript 4.5.5 无法解析 type-fest@4.41.0 类型语法，且内置精确搜索缺少 ripgrep 二进制；学城自动上报因远端 XML 响应为空未推送 | 本地留痕，待补推 |
| 2026-08-27 | OpenWork macOS 测试包验收时首次使用了缺少“测试版”后缀的产物路径，`file` 校验失败后已用实际路径复核 | 已上报 |
| 2026-08-27 | MultiEdit 修改 Shell 转义文本时锚点未命中，需重新读取后精确替换 | 已上报 |
| 2026-08-27 | jibai-landing 合并提交钩子将 TS/TSX 当 JS 解析且拒绝默认 `merge:` 提交信息 | 已上报 |
| 2026-08-27 | jibai-landing 提交钩子 Rome 将 TS/TSX 当 JS 解析，lint-staged 失败但 Git commit 仍成功并推送 | 已上报 |
| 2026-08-27 | CatDesk GUI 未运行或未登录，浏览器自动化无法建立本地验收会话 | 已上报 |
| 2026-08-27 | 浏览器技能文档指定 ~/.catpaw/bin/catdesk，但实际安装在 ~/.catdesk/bin/catdesk | 已上报 |
| 2026-08-27 | 提交钩子 Rome 将 TS/TSX 当 JS 解析而报错；学城异常上报因 CIBA 确认超时未完成 | 已记录，待补推 |
| 2026-08-26 | 隔离 worktree 中 pnpm install 超时，未生成 Landing 所需 tsc 二进制 | 已上报 |
| 2026-08-26 | OpenWork 数据就绪 SPEC 更新时 MultiEdit 因包含 old/new 相同的无效替换而整体拒绝执行 | 已上报 |
| 2026-08-25 | OpenWork Server typecheck 被既有 computeruse-tool 声明导出和 owpenwork/tool-source 模块解析问题阻断 | 已上报 |
| 2026-08-25 | OpenWork Server 响应适配测试首次运行时暴露数值时间戳断言采用 UTC、运行环境采用 UTC+8 的时区差异 | 已上报 |
| 2026-08-24 | 既白 Landing 重构中 MultiEdit 因相邻编辑锚点重叠拒绝操作；学城上报因 token 交换超时未推送 | 待补推 |

| 2026-08-12 | skill-toolkit clean 编排 Task 1，独立 Agent 在主工作区而非隔离 worktree 创建提交 | 已上报 |
| 2026-08-13 | 修复 AGENTS.md 时使用 python3 -c 拼接含 Markdown 反引号的长文本 | **内容损坏** — 远端记录乱码，关键场景和报错不可可靠还原 |
| 2026-08-13 | 修复 OpenWork PMS 路径校验时 MultiEdit 连续两次返回网络错误 | **内容损坏** — 远端记录乱码，关键场景和报错不可可靠还原 |
| 2026-08-13 | mtdev M0-M2 提交前 git diff --cached --check 发现大量尾随空格 | 已上报 |
| 2026-08-13 | 云部署预授权路由 MultiEdit 锚点重叠失败 | 已上报 |
| 2026-08-13 | 云部署预授权路由测试预期 409 但实际得到 403（守卫顺序） | 已上报 |
| 2026-08-13 | 云部署预授权路由测试夹具 TS2352 类型断言错误 | 已上报 |
| 2026-08-13 | MultiEdit 因目标文本多次出现且未设 replace_all 而拒绝执行 | 已上报 |
| 2026-08-14 | agent-fs snapshot/read-fresh 对 CHANGELOG.md 返回 file_not_found | **已解决** — 文件现已存在 |
| 2026-08-17 | mtdev rds write-ddl 命令不存在 | 已上报（外部依赖） |
| 2026-08-17 | git diff --cached --check 因行尾空格失败 | 已上报 |
| 2026-08-19 | git cherry-pick 浅克隆中不存在的本地提交返回 bad revision | 已上报 |
| 2026-08-19 | git worktree add 命令返回非零且未输出具体错误 | 已上报 |
| 2026-08-19 | git log pathspec 参数位置错误返回 unrecognized argument | 已上报 |
| 2026-08-20 | 云端部署路由修复时 MultiEdit 对唯一精确删除操作返回 reject_all；学城自动上报因 SSO token exchange 超时未推送 | 本地留痕 |
| 2026-08-21 | jibai-landing 首页 MP4 背景改动时 snapshot 返回 63 位 SHA-256，学城自动上报因 SSO token exchange 超时未推送；浏览器 Skill 声明的 `~/.catpaw/bin/catdesk` 路径不存在，实际 CLI 在 `~/.catdesk/bin/catdesk` | 本地留痕 |
| 2026-08-21 | jibai-landing 通用视频组件验证时，全局和包级 TypeScript CLI 路径均不可用，无法额外执行 tsc；已由 Vitest 与真实浏览器验证替代 | 本地留痕 |
| 2026-08-23 | jibai-landing help section 开发时 MultiEdit 因缺失 `new_string` 参数拒绝编辑；自动上报因 SSO token exchange 超时未推送 | 本地留痕 |
| 2026-08-24 | jibai-landing 视频缓存验证时，catdesk-browser Skill 指定的 `~/.catpaw/bin/catdesk` 路径不存在；学城自动上报因 SSO token exchange 超时未推送 | 本地留痕 |
| 2026-08-24 | jibai-landing 首页按钮流光/粒子 CSS 原型验证时，catdesk-browser Skill 指定的 `~/.catpaw/bin/catdesk` 路径不存在；学城自动上报因 SSO token exchange 超时未推送 | 本地留痕 |
| 2026-08-24 | 分析用户提供的 MP4 参考样式时，`ffprobe` 不存在且指定视频路径无法找到，无法提取参考帧；学城自动上报因 SSO token exchange 超时未推送 | 本地留痕 |

## [未发布] - 2026-09-30 可靠编辑第一期：schema-2 结果契约、Htarget 校验与中断核实

依据《[Agent-fs 可靠编辑 SPEC](../../knowledge/superpowers/specs/2026-09-29-agent-fs-concurrency-interruption-recovery-design.md)》第一期交付。
**本版本包含有意的安全兼容变更（退出码 3、`new_sha256` 收紧、`reason` 语义变化），不声称完全兼容，迁移要求见 `references/result-contract.md`。**

### 新增

- 统一结果契约 `src/agent_fs/result.py`（schema_version=2）：`status`（verified/rejected/error/unknown/prepared）+
  `commit_state`（committed/not_committed）+ Hbase/Htarget/Hobserved 三哈希分列 + `reason_code` 稳定枚举 +
  `durability` 分级 + `ancillary_failures` 附属诊断；`REASON_MAP` 为 reason_code → 状态/退出码/事件分类的唯一实现。
- `agent-fs reconcile <file> --base-sha256 <Hbase> --desired-sha256 <Htarget>`：中断后的只读三哈希核实，
  返回 desired_present / base_present / diverged / unavailable / error；不取锁、不写目标、不回滚、不重放。
- `write-if-match` / `edit-if-match` 新增 `--expected-result-sha256`（Htarget）：替换前将实际准备字节与声明值
  强校验，不一致返回 `prepared_content_changed`（目标不动）。
- `edit-if-match --dry-run`：只读预览（status=prepared），复用与实际写入同一套变换逻辑，
  返回 Hbase/Htarget/编辑数/edits 文件哈希；实际写入仍重新校验全部前提。
- Skill 细则拆分：`references/result-contract.md`（结果契约）、`references/concurrent-editing.md`（锁域边界）、
  `references/interruption-recovery.md`（中断分流与恢复决策）；references 已随 wheel 打包。

### 变更（安全语义）

- **新增退出码 3（结果未知）**：已替换但回读失败/回读不一致/不能判定是否提交时返回
  `status=unknown, commit_state=committed, exit 3`——只允许进入核实流程，不自动重试写入；
  `written=true` 不再等价于成功。
- **`new_sha256` 收紧**：仅 `verified` 时填 Htarget；实际观察值一律在 `observed_sha256`。
- **`reason` 语义变化**：从"code[: 详情]"变为纯人类说明，分支判断改用 `reason_code`，异常细节迁至 `detail`。
- 提交事实与结果确认分离：`os.replace` 成功即 committed，此后任何异常不回退为 `not_committed`；
  `main()` 兜底按执行阶段分流（pre_commit → error/2，post_commit → unknown/3），删除统一"重试一次"提示。
- durability 分级：平台不支持 fsync（ENOTSUP 等）降级 `unconfirmed` 并声明；提交前真实同步 I/O 错误
  阻止替换（`sync_io_error_before_commit`）；提交后目录 fsync 失败不回滚、不重写。
- 参数错误统一 schema-2 字段结构（保留 `error`/`category` 旧键）；`events` 事件新增
  `file_state`/`recovery_action` 字段（根因/文件状态/恢复动作分开记录，SPEC §9）。
- SKILL.md：强制协议改为「read-fresh → 准备(Htarget) → 受控写入 → 结果核实」，verify 降级为诊断工具；
  触发场景补编辑超时/连接中断/响应缺失/回执矛盾/恢复未完成；退出码表与命令表同步。

### 验收

- 全量测试 217 passed（含 A01–A20 中可自动化项：并发拒绝、双进程同锁域竞争、不同锁域边界、故障注入回读失败/外部改写、reconcile 全状态、
  附属异常、durability、材料漂移、dry-run 一致性；A02/A19 为双进程/双锁域用例），Ruff 检查通过。
- wheel 端到端构建验证：SKILL.md 与全部 references 文件打进 wheel 且内容一致。
- 平台覆盖：macOS（arm64）实测；Windows 仅代码级兼容（锁/fsync 降级路径已覆盖 errno 判定），未实测——
  按 SPEC §11 要求，单平台完成不宣称跨平台验收。

### 已知未解决

- 跨会话持久操作记录与自动续做（SPEC §10.2，第二阶段）。
- 事件锁降级路径可能静默丢事件（P1，沿用上一版本台账）。

## [未发布] - 2026-09-16 事件留痕、锁内多点编辑与 CLI 契约收敛

### 新增

- `agent-fs edit-if-match`：在同一把 per-path 协作锁内完成多点受控编辑（稳定读取 → 锚点校验 → 应用 → 复核 → 原子替换），
  支持 `--expected-sha256` / `--sha256` / `--snapshot` 三种基线指定方式与 `--edits-file` 编辑列表。
- `agent-fs events list`：查询本地异常事件流（`~/.agent-fs/events.jsonl`），`--syncable` 筛选可同步事件。
  事件按五级分类记录，同指纹事件去重计数，落盘前对路径、哈希、时间戳做归一化，避免绝对路径原样留存。
- `references/error-taxonomy.md`：异常五级分类，以及 reason → 类别 → 退出码映射的说明。

### 变更

- 报告体系统一为五级分类，`cli.py` 全部子命令接入；失败路径不再让异常穿透，转为结构化 `reason` 字段。
- `write_atomic.py` 补齐协作锁相关失败分支（`lock_failed` / `lock_busy`）的结构化返回。

### 修复

- 事件文件并发追加安全：正常锁路径补齐跨进程排他锁与唯一临时文件名。
- 链式编辑（a→b、c→a）改为逐步重放处理，不再一次性比对。
- `read-fresh --max-bytes` 按 UTF-8 字节安全截断，并返回 `content_total_bytes`。

### 验收

- 全量测试 107 passed / 1 skipped（跳过项为未安装 `python -m build` 时的 wheel 解包端到端用例），Ruff 检查通过。

### 已知未解决

- 事件锁降级路径仍可能静默丢事件（P1）、`new` 包含 `old` 时编辑被误拒（P2）、CLI 退出码与字段契约未完全统一（P2）、
  事件目录权限与凭据脱敏不足（P2）。四项均为复审后保留的可复现问题，详见
  《[agent-fs 已知问题汇总](https://km.sankuai.com/collabpage/2779500222)》。

## [未发布] - 2026-09-07 协作写加固与预检补强

### 新增

- `write_atomic.py` 增加按规范化绝对路径加的 per-path 协作锁（POSIX `fcntl.flock` / Windows `msvcrt.locking`），
  非阻塞获取，拿不到直接返回 `lock_busy`；写入流程补齐替换前二次复核与父目录 `fsync`。

### 变更

- `guard.py` 预检覆盖范围扩大；`SKILL.md` 同步补充协作写的使用边界——协作锁只对遵守 `agent-fs` 协议的进程生效，
  不构成对任意进程的严格互斥，严格互斥仍需写租约。

## [未发布] - 2026-08-20 新增 guard 预检命令族

### 新增

- `agent-fs guard` 六个无副作用预检子命令：`git-commit`（`git diff --check`）、`git-object`（commit 对象存在性）、
  `pathspec`（路径分隔符与平台一致性）、`worktree`（工作副本 root/branch 身份）、`text-write`（合法 UTF-8 与替换字符）、
  `multiedit`（锚点唯一性与区间重叠）。
- `SKILL.md` 补充预检命令的适用场景与调用时机。

### 说明

- 这批检查此前以人工纪律形式存在于工作协议中（从磁盘原文提取锚点、写入前用独立命令交叉核对），本次固化为可调用命令，
  对应《跨平台多 Agent 文件协同编辑技术方案》中"写前预检"层。

## [未发布] - 2026-08-04 首个版本：多 Agent 文件协作可靠性工具

### 背景

针对学城文档《[doc-toolkit 文档Skill 已知问题汇总](https://km.sankuai.com/collabpage/2775090406)》中记录的
"write-after-read 可靠性"问题（多个 Agent/脚本/人工同时操作同一文件时，缺乏新鲜度校验与并发控制手段，
容易读到并发写入的中间态、或覆盖他人刚写入的内容）新建的独立零依赖 CLI 工具。

### 新增

- `agent-fs snapshot`：采集文件稳定快照（size/mtime/inode/sha256），内部使用
  `compute_stable_fingerprint()` 做 stat-hash-stat 三明治校验，避免采集到并发写入的中间态。
- `agent-fs read-fresh`：新鲜读取，同样使用 stat-read-stat 三明治校验并支持重试，防止读到被并发修改穿插的中间内容；支持
  `--offset`/`--limit` 按行截断，支持二进制文件（返回哈希但不返回可解码文本）。
- `agent-fs verify`：写前乐观并发校验，比较当前文件哈希与调用方持有的基线哈希是否一致。
- `agent-fs changed`：对比某次历史快照与当前文件状态，识别"内容未变化 / 内容已变化 / 文件已消失 / 读取不稳定"。
- `agent-fs write-if-match`：受控原子写入（write-if-match 语义），仅当当前文件哈希等于 `--expected-sha256`
  时才通过 `os.replace()` 原子替换内容，否则拒绝写入并保留原文件；支持 `--content`、`--content-file`、stdin
  三种输入方式。如实说明：本命令把校验窗口压缩到本进程内的两次系统调用之间，不是跨进程严格 CAS。
- 所有命令统一以 JSON 输出到 stdout，退出码语义为 0（成功/匹配/未变化）/1（业务性不稳定或不匹配，非程序错误）
  /2（程序错误，如文件不存在、参数格式错误）。
- 零第三方依赖，仅使用标准库，跨平台（macOS/Linux/Windows）：Windows 上 inode 恒为 0 时自动降级为仅依赖
  size+mtime 判定；快照存储路径使用 `Path.home()` 而非硬编码 `/tmp`，支持 `AGENT_FS_HOME` 环境变量覆盖便于隔离测试。

### 验收

- 独立子 Agent 黑盒评审（不知悉实现细节，自行阅读代码、独立设计边界场景验证）：首轮发现 1 个 Critical、
  1 个 High、2 个 Medium、2 个 Low 级问题，结论 FAIL。问题详情：
  - 【Critical】`compute_fingerprint()` 返回的 `FileFingerprint.path` 字段未经 `path.resolve()` 规范化，
    同一文件的不同路径写法（如含 `..` 的路径）会得到不一致的 path 字段。
  - 【High】`pyproject.toml` 声明的 `package-data` 未生效，`SKILL.md` 实体在包根目录而非 `src/agent_fs/` 内，
    导致构建出的 wheel 不包含它。
  - 【Medium】快照文件以默认权限（0o644）写入，多用户系统下其他用户可读到文件绝对路径与内容哈希。
  - 【Medium】`tests/test_fingerprint.py`、`tests/test_read_fresh.py` 中 2 处 Ruff `UP012` 规则报错。
  - 【Low】`verify` 命令不校验传入哈希格式，格式错误时只返回 `match=False` 而非明确参数错误。
  - 【Low】`read-fresh` 允许负数 `offset`，静默返回空内容而非明确拒绝。
- 修复：`compute_fingerprint()` 内部对路径做 `resolve()` 规范化后再返回；采用与 `doc-toolkit` 一致的符号链接方案
  （`src/agent_fs/SKILL.md` 指向 `../../SKILL.md`）使 wheel 正确打包 `SKILL.md`；`save_snapshot()` 写入后
  `chmod(0o600)` 收紧权限；修正 2 处 Ruff 报错；`verify`/`write-if-match` 新增哈希格式正则校验；`read_fresh()`
  新增负数 offset/limit 拒绝分支。同时补齐此前完全缺失测试覆盖的 `write-if-match`（`write_atomic.py`）命令
  的单元测试与 CLI 端到端测试（成功替换、哈希不匹配拒绝、文件不存在、目录路径拒绝、临时文件清理、空内容写入等）。
- 第二轮独立子 Agent 复核：逐项复验上述 6 个问题的修复效果，并自行补充了打包完整性回归测试
  （`tests/test_packaging.py`，含真实执行 `python -m build` 解压校验 wheel 内容的端到端用例），额外验证
  `write-if-match` 通过 stdin 输入、`changed` 命令在读取持续冲突导致重试耗尽时的行为等场景。结论：**PASS**，
  全部 58 个测试通过（1 个因未安装 `python -m build` 而优雅跳过），Ruff lint 无错误，wheel 内容校验通过。
