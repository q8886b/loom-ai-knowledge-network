# Loom 共同部分（所有模式 skill 通过 @ 引用此文件）

## 运行时

本 skill 可在 Claude Code 和 Codex 中使用。两者都有 hooks，但配置文件不同：

- **Claude Code**：Loom hooks（SubagentStop 计算层 + block-back 语义自检等）默认不触发。使用本 skill 时，先执行：

```bash
loom on
```

  即可在当前项目启用 hooks。关闭用 `loom off`，或设置环境变量 `LOOM_ACTIVE=1` 临时强制开启。
- **Codex**：Codex 不读取 `~/.claude/settings.json`，但读取 Codex hooks 配置（全局 `$CODEX_HOME/hooks.json` 或项目 `.codex/hooks.json`）。`install.sh` 会安装 `SubagentStop` / `Stop` hook，通过 `loom-hook` 触发同一套 stop-check。使用前同样执行 `loom on` 激活当前项目。

## 共同前提

**Loom 是 LLM 思考时的 Harness**（不是被检索的知识库）。设计依据：`docs/design/005-layered-redesign-harness.md`。

**所有卡片操作走 `loom` CLI**，特权维护操作走 `loom-admin` CLI，不直接操作数据库。计算校验内联在 `write-draft`（12 条单卡）和 `loom-admin stop-check-pending`（4 条整批）。Claude Code 与 Codex 都由 `SubagentStop` / `Stop` hook 自动触发 stop-check。无论运行时是什么，agent 都没有"被 hook 自动 commit"的路径——commit 由 agent 在语义自检通过后显式触发。

**立体目录由使用方主动读取**：THINK / USE / PIPELINE 子 agent 启动后，必须先执行 `loom orient` 把 `~/.loom/data/orient.md` 注入 Context（含 namespace 全貌 + L4 全量含核心命题摘要）。DIGEST 阶段不读 L4（完全 L4-blind）。思考中想轻量重读 L4 标题用 `loom read-l4-index`。

## Codex hook 兜底

正常情况下，Codex hook 会在 `mark-ready` 后的 agent 停止点自动运行 `loom-hook`，并把 `decision:block` 注入回来做语义自检。若 hooks 未安装、未信任、或你在非 Codex hook 环境里批量维护，才手动运行：

```bash
loom mark-ready $TASK_ID
loom-admin stop-check-pending
```

`stop-check-pending` 会扫描 `.ready` task，跑整批计算校验，并写出 `.computed_passed.json` 与 `.semantic_sample.json`。如果输出里有 `decision: block`，把它当作必须处理的状态机提示。

计算通过后，读取 `/tmp/loom_task/$TASK_ID/.semantic_sample.json`，按 type_match / single_unit / genuine_digest / self_contained 做语义自检。通过后：

```bash
loom commit-ready $TASK_ID --semantic-passed
```

语义失败则修 draft，重新 `loom mark-ready $TASK_ID`。多任务并行时，`stop-check-pending` 会聚合抽样到 `/tmp/loom_task/.semantic_sample.json`；主 agent 可以统一复检后逐个 `commit-ready`。

## Deep commit 后：L4 候选处理（主 agent 必做）

Deep 子 agent 完成语义自检并 `commit-ready` 后，主 agent **必须**：

1. 查看 Deep 结果里是否记录了 L4 候选；候选只说明核心命题和预期锚点，不等于已提案。
2. 确认候选锚定的 L2/L3 卡已经入库，再决定是否调用 `loom propose-l4 <task_id> gen:<卢曼ID> --related=<已入库卡ID...>` 写 staging。
3. 对每份 staging 提案，把 content **完整内容**讲给用户听——核心命题 + 在哪些领域成立（**核查跨域真实性**：只在 LLM/agent 工程内成立的不该是 L4）+ 边界 + related_cards 是否孤岛。
4. 等用户决策：批准 / 丢弃。
5. 用户决策后调 `loom-admin proposal-decision <proposal.json> --decision=approved|rejected [--reason="..."]`（封装改 JSON status 字段，避免手改 JSON；reason 写入 `decision_reason`）。
6. 批准后再调 `loom-admin commit-l4 <proposal.json>` 入库（CLI 校验 status=approved，否则拒绝）。
7. 丢弃则 status 已变 rejected，proposal 留在 staging/ 不删（审计 trail）。

**L4 是认知架构塔尖，宁缺毋滥**。子 agent 提了什么 ≠ L4 应该有什么——主 agent 是把关门。

## 工具集（loom）

**定位**：`orient`（启动时读立体目录）/ `read-l4-index`（思考中轻量重读 L4）
**读卡**：`read-cards <id>... --task-id=<TASK_ID>`（单/多，bump use_count，记录 read trace 与 L4 refs；对 L1 默认轻量返回，全文用 read-source）/ `skim <id>`（轻量浏览，不 bump）
**读原文**：`import-source <id> --title=<X> --path=<md>`（注册 L1 source card）/ `read-source <L1_id|path>`（读 L1 source card 全文）
**找卡**：`search <q>`（关键词/语义）/ `browse-tree <ns>`（namespace 主题树）
**关联**：`neighbors <id> --depth=N`（link 图遍历）/ `wander <id>`（随机游走）
**建链**：`suggest-links <id>`（找未 link 的语义近邻，构建阶段补缺口）
**Luhmann 结构**：children / siblings
**辅助**：browse / namespaces / stats
**写 draft**：write-draft（内联 14 条单卡计算校验）
**提案**：propose-l4（提案时跑完整 L4 机器校验）/ propose-card-edit（写到 staging，等用户审核）
**任务收尾**：mark-ready → Claude/Codex hook（或手动 `loom-admin stop-check-pending` 兜底）→ 语义自检 → commit-ready --semantic-passed
**特权**（不由工作 agent 直接调用，由 hook 或主 agent 在用户审核后调用）：loom-admin commit-l4 / apply-card-edit / update-card / delete-card / rebuild-l4-index / stop-check-pending

**注意**：旧的 `loom-admin commit` 入口已移除（008 §5）——入库只能走 `loom commit-ready <task_id> --semantic-passed`。

**翻阅姿势**：查询→关联→思考→再查询的螺旋。`orient` 启动定位 → `search/browse-tree` 找入口 → `skim` 快速判断 → `read-cards` 批量深读 → `neighbors/wander` 展开关联 → `suggest-links` 思考后补缺口。交替节奏自己掌握。

THINK/USE 中的 `read-cards` 必须带 `--task-id $TASK_ID`（或确保环境变量 `LOOM_TASK_ID=$TASK_ID` 已导出），否则 stop-check 无法把 read trace / L4 引用归属到当前任务，L4 WARN 可能误报。

**embedding 的两个用途严格区分**：查询时只 `search` 调 embedding（query 是实时输入）；card→card 相似度在构建阶段由 `suggest-links` 算，结果凝结进显性 link 网络。**link 是显性真相，embedding 是辅助建立 link 的工具——不并存两套关联**。

## 铁律

1. **不直接入库**——你只能写 drafts；计算校验由 write-draft 和 hook 中的 loom-admin stop-check 把关，语义自检由你自己做，通过后调 commit-ready 入库。
2. **write-draft 当场校验**——14 条单卡计算校验内联，失败当场拒绝。
3. **L4 提案走 staging**——发现新模式用 propose-l4（提案时跑完整机器校验），不直接写库。L4 是认知架构，决定权在用户。
4. **主题卡由 Scout 建**——Deep agent 启动时主题卡已 commit。Deep 不建主题卡，只读主题卡作为"已读过一遍"的状态输入。Scout 只写主题卡，Deep 不写主题卡（write-draft 强制）。
5. **反思必须锚定**——type=反思 的卡必须 link 一张 type∈{判断,模式} 的卡。
6. **L4 索引格式**——L4 卡 content 第一段以 `[探索期]` 或 `[熟练期]` 开头。
7. **L2 link 规则（008 §1）**——非主题 L2 卡必须 link 主题卡；可选 link 本材料（同 domain:book 前缀）内其他 L2 卡；反思卡必须 link 判断/模式卡；禁止跨材料/跨领域/L4 link，放到 L3 THINK 阶段。
8. **L2.source 指向 L1 source card id（008 §21）**——L2 卡 `--source` 不写 markdown 路径，写对应 L1 source card id（如 `llm:harness:src:08`）。L1 source card 由 `import-source` 预先创建。

## 卢曼树与 ID 选择

卢曼 ID 不是随手拍的唯一标签，而是**探索结果的沉淀**。

**为什么**：同 namespace 内，ID 表达笔记的主生长路径。`children` / `siblings` 按 ID 前缀推导父子/兄弟关系，使树成为结构性回忆工具。如果新卡随意开新顶层分支，树就失去局部性，未来浏览时看不到相关旧卡，也无法沿树继续生长。

**完整规则**：

- 卢曼 ID 必须完整匹配 `数字段 (字母段 数字段)* 字母段?`：数字段=`[0-9]+`，字母段=`[a-z]+`（如 `3` → `3a` → `3a1`，`3ab` 也合法）。下划线、连字符、大写字母和 `_v2` 等版本后缀都非法。
- 父级 ID 必须先存在才能建子节点；兄弟节点共享同一父级。
- 树的作用域：L2 在 `<领域>:<书>` 内，L3 在 `<领域>` 内，L4 在 `gen` 内；L1 不进树。
- 跨层关系由 `links` 表达，不由 ID 嵌套。

**ID 选择是螺旋探索的结果，不是独立步骤**。在 THINK/USE/DIGEST 的多轮 `search / neighbors / children / siblings / read-cards` 中，你已经接触了相关卡并理解了新卡与它们的关系。写卡时，ID 直接表达这个已经确认的位置：

- 新卡是对某张已有卡的**深化或细化** → 挂为其**子节点**；
- 新卡与某张已有卡**并列或连续发展** → 挂为其**兄弟节点**；
- 新卡确实开启一条**全新思想方向** → 才创建新的**顶层数字根卡**。

如果最相关的父级卡还不存在，只有当父级本身构成独立认知单元时才先建父级（L2 章节入口通常是 `type=主题`）；否则把新卡放到合适的已有兄弟位置或新建顶层根。禁止为满足 ID 或门禁创建空目录父卡。

## 起 sub agent 合约

主 agent 起 USE/THINK/DIGEST 子 agent 时，prompt 第一步必须让子 agent **主动加载 skill 文件**（Skill 工具或 Read `skills/loom-*/SKILL.md`），不复制流程。详见 005 §9.4。

## type 易错点（补充 004 type 定义）

1. **判断 vs 案例**：判断 = 定理性命题（跨场景成立）；案例 = 应用判断到具体实例（依赖场景）。「X 系统有 Y 现象」是案例不是判断。
2. **反思不是"不同意见"**：必须 link 判断/模式卡（锚定对象）。

## 反作弊（来自 AGENTS.md）

起消化/深化子 agent 时，prompt 中绝对禁止：提到具体 qid、具体题干关键词、正确答案方向、让 agent 读 eval_results。深化方向只基于"卡覆盖度不足"，不基于"哪题错了"。
