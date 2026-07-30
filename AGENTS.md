# Loom — Agent 工作规范

> 首次进入本项目，先读：
> - `docs/design/004-layered-redesign-purpose.md`：Loom 要成为什么
> - `docs/design/005-layered-redesign-harness.md`：Harness 如何落地
>
> 这两份是当前设计基准。改代码、改 skill、改流程时，同步判断是否需要更新它们。

## 工作方式

- 不中断：对齐目标后自主执行，卡死再请示。
- 理解意图：记住用户真正目的，不机械执行表面指令。
- Skill 变更谨慎：修改已有 Skill 前先理解并保留原设计目的，尤其删除或替换旧机制必须有明确依据，默认做增量式最小修改。
- Skill 变更流程：涉及设计语义的改动按 Design → Spec → 具体实现 / Skill 逐层对齐后落地。
- 产出物汇报用完整绝对路径。
- 高成本任务先小规模验证，再全面执行。
- Python 默认用 `python3.11` / `pip3.11`。
- 超过 10 秒的步骤先报预估；长任务要有可见进度，超时立即排障。
- 独立任务优先并行，有依赖才串行。
- 调用 LLM API 时不设 `max_tokens`。
- 海外资源命令临时走代理：`http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897 <cmd>`。

## 项目结构

```text
bin/loom              工作 agent CLI
bin/loom-admin        特权 CLI（hook / 用户审核后）
src/loom/             Python 实现
  store.py            SQLite + links + FTS5 + sqlite-vec
  checks.py           write-draft 单卡校验 + stop-check 整批校验
  cli.py              命令分发与状态机
skills/               Loom agent skills
docs/design/004...    设计目的
docs/design/005...    Harness 规格
~/.loom/              默认数据目录（data/cards/sources/.env）
```

## CLI 快查

所有卡片操作走 `loom`，输出 JSON。特权操作走 `loom-admin`。

```bash
# 激活 / 关闭 hook
loom on
loom off

# L1 原文
loom import-source <source_id> --title=<title> --path=<md>
loom read-source <L1_id|path>

# 读与探索
loom orient
loom read-l4-index
loom read-cards <id> [<id>...] [--task-id=<task_id>]
loom search <query> [--mode=hybrid|fts|vector] [--top=N] [--ns=X] [--type=X]
loom browse <namespace> [prefix]
loom browse-tree <namespace>
loom children <card_id>
loom siblings <card_id>
loom neighbors <card_id> [--depth=N]
loom skim <card_id>
loom wander <card_id> [--steps=N]
loom suggest-links <card_id> [--top=N]

# 单书 THINK 覆盖账
loom think-init <task_id> --goal=<目标> --materials=<domain>:<book>
loom think-skip <task_id> <主题卡id> --reason=<不深挖的具体理由>
loom think-coverage <task_id>

# 写 draft，不直接入库
loom write-draft <task_id> <card_id> \
  --type=<type> --title=<title> --source=<source> \
  [--layer=L1|L2|L3|L4] [--links=a,b] [--content-file=file] \
  [--from-topics=<主题卡id,...>]

# 提案
loom propose-l4 <task_id> gen:<卢曼ID> \
  --title=<title> --type=模式|判断|反思 --related=a,b --content-file=file \
  [--from-topics=<主题卡id,...>]
loom propose-card-edit <task_id> <card_id> \
  --type=修正|补充|重写|更新 [--related=a,b] --content-file=file \
  [--from-topics=<主题卡id,...>]

# 收尾
loom mark-ready <task_id>
loom commit-ready <task_id> --semantic-passed

# 特权命令
loom-admin stop-check <task_id> [--mode=normal|salvage]
loom-admin stop-check-pending [--all-sessions]
loom-admin salvage-pending [--run-stop-check]
loom-admin proposal-decision <proposal.json> --decision=approved|rejected [--reason=...]
loom-admin commit-l4 <proposal.json>
loom-admin apply-card-edit <proposal.json>
loom-admin update-card <card_id> [--title=X] [--type=X] [--source=X] [--links=a,b] [--content-file=file]
loom-admin delete-card <card_id>
loom-admin rebuild-l4-index
```

## 入库铁律

工作 agent 没有直接入库权限。

正确链路：

```text
write-draft
  → mark-ready
  → hook 或 loom-admin stop-check-pending 跑计算层
  → 读取 .semantic_sample.json 做语义自检
  → commit-ready --semantic-passed
```

`commit-ready` 只在计算层通过且 draft 未被修改后入库。不要绕过这条链路直接写数据库。

## ID 与 Layer

- L1：`<领域>:<书>:src:<单元>`，如 `llm:harness:src:01`
- L2：`<领域>:<书>:<卢曼ID>`，如 `llm:harness:01a`
- L3：`<领域>:<卢曼ID>`，如 `llm:1a`
- L4：`gen:<卢曼ID>`，如 `gen:1a`

领域简写：`llm` / `fin` / `med` / `law` / `sw` / `phil` / `prod` / `fit` / `psy` / `hist` / `soc` / `sci`。

卢曼 ID 用数字字母：`01` / `12a` / `12a1`，不用英文短语或中文短语。

Card layer 只有 `L1 / L2 / L3 / L4`。任务目标可有 `L2_light`，但产出的卡仍是 `layer=L2`。

## Type

合法认知 type：

`概念 / 结构 / 机制 / 案例 / 判断 / 反思 / 模式 / 主题`

另外 `source` 只给 `layer=L1` 原文卡。

关键约束：

- L4 只允许 `模式 / 判断 / 反思`。
- 反思卡必须 link 至少一张 `判断` 或 `模式` 卡。
- L3 必须 link 至少一张 L2 卡；L1 只能作为补充证据。
- L4 必须锚定至少两个不同领域的 L2/L3 卡。
- L4 content 第一段必须以 `[探索期]` 或 `[熟练期]` 开头。

## DIGEST 规则

L2 消化必须走两阶段：

```text
Scout：通读整本材料，只写 type=主题 的主题卡
Deep：每章读 L1 全文 + 已入库主题卡，写其余 L2 卡
```

L2 link 规则：

- 非主题 L2 卡必须 link 对应主题卡。
- 允许 link 同一材料内的其他 L2 / L1 source card，用于表达材料内部结构。
- 禁止 link 跨材料、跨领域、L4；这些属于 L3 THINK 阶段。

DIGEST 完全 L4-blind：不读 L4 索引，不 read-cards L4，不 link L4。发现 L4 候选先记录，等相关 L2/L3 入库后再提案。

## THINK / USE 规则

THINK / USE 启动时先 `loom orient`，把 namespace 全貌和 L4 元层模式读入 context。

L4 是“强制读入，不强制使用”：可以零引用，但 stop-check 会给 WARN，提醒复盘这次是否真的不需要 L4。

## 资源管理

下载的书、PDF、论文、视频、音频等必须整理到 `sources/<领域>/`，不要散落在 `~/Downloads` 或 `/tmp`。

目录：

```text
sources/01-金融/
sources/02-医学/
sources/03-法律/
sources/04-软件/
sources/05-哲学/
sources/06-产品/
sources/07-LLM/
sources/08-健身/
sources/09-心理学/
```

命名建议：`<编号>-<作者或主题>-<书名>.<格式>`；多文件材料建子目录。

卡片 `source` 字段必须指向已注册 L1 source card，或按 005 的 layer 语义填写。

## 反作弊

Loom 是通用大脑构建系统，不是题库优化器。

禁止让卡片内容、密度、存在性受“哪些题答错了”影响。起消化/深化 agent 时，不要在 prompt 里写具体 qid、题干关键词、正确答案方向，也不要让 agent 读 `/tmp/stable_qs/` 或 `data/eval_results/`。

正确做法：主 agent 先做诊断报告，只描述覆盖模式和材料薄弱点；深化 agent 只基于原始材料与覆盖度补卡。
