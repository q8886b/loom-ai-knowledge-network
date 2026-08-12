# 005 — 分层重设计的 Harness 落地

> 004 定义了 Loom 应该是什么样（L1-L4 分层、type 系统、激活机制、密度门禁）。
> 005 回答 harness 怎么把 004 的设计变成可执行的现实。
>
> 命题：**Agent = Model + Harness**（Harness Engineering）。
> Loom 是 LLM 思考时的 Harness，不是被检索的知识库——它的作用是把 LLM 已内化的压缩智能转化为可靠、有目的的思考行动。
>
> 本文同时承载设计逻辑与具体形态：前半讲全局架构与构件分工，后半展开每个构件的实现规范（路径、工具签名、hook 配置、判据）。
>
> 本文取代 001（旧三层 harness 设计）。001 的核心洞察——"规则下沉到工具层，不靠 prompt 自觉"——在 005 保留并精确化。

## 一、Harness 视角下的 Loom

### 1.1 五构件映射

按 Harness Engineering 的五构件框架，Loom 的所有设计决策落在五个构件 + 三个支撑层上：

| 构件 | 在 Loom 里的实现 | 004 对应 |
|---|---|---|
| **Prompt** | AGENTS.md + SKILL.md/DIGEST.md/THINK.md/USE.md（制度化合约） | type 系统、反作弊、工作原则 |
| **Context** | 立体目录主动读取（`orient`）+ L1 可逆压缩 + 周期性重读 | L4 激活机制（思考-激活） |
| **Plan** | plan.json 约束性合约（每任务一份） | 三个使用场景的流程 |
| **Tool** | bin/loom CLI 工具集（agent 通过 Bash 调用） | 卡片 CRUD、search |
| **Hook / 收尾触发** | Claude Code / Codex hook（SubagentStop/Stop，SessionStart 不注入 L4） | 密度门禁、type 约束、入库 |
| **可观测性** | use_count/search_count + 任务 trace | 卡片活跃度自动维护 |
| **Skill** | Loom 自身（记忆化搜索 + 人类审核演化） | L4 演化、Skill 闭环 |

### 1.2 核心设计原则（贯穿所有构件）

这些原则来自 Harness Engineering，是后续所有具体设计的判据：

- **硬约束前置，软约束兜底**（ch02 §2.5）：能形式化的约束从"事后软约束"迁移到"事前硬约束"。
- **合约两个条件**（ch07 §7.2）：每条规则必须同时满足**可观察性**（谁在验证？）+ **激励相容性**（完成任务压力下 agent 会主动遵守？）。任一不满足，规则价值为零。
- **约束 = 验证机制**（ch07 §7.2）：没有对应验证机制的规则必须删掉，或补一个验证机制。
- **最小权限**（ch10）：操作集设计决定 agent 能到哪些状态；"Agent 用得上"不是保留工具的充分理由，"没有这个工具任务必然无法完成"才是。
- **Hook 只诊断不执行**（ch11）：Hook 输出止步于诊断与状态机协议提示（"计算层已通过，请做语义自检；通过后走 commit-ready 协议"）；允许说明下一步状态机命令名，但**禁止**输出具体修复方案、替 agent 改内容、或自动触发工具调用（008 §4）。

## 二、物理形态

### 2.1 L1-L4 的物理表示

| 层 | 物理形态 | 位置 |
|---|---|---|
| L1 | 库内卡片（`layer=L1, type=source`）+ markdown 镜像 | `data/brain.db` + `sources/<领域>/<编号>-<材料名>/<单元标识>.md` |
| L2 | 库内卡片 | `data/brain.db` + `cards/<领域>/<书>/<id>.md` |
| L3 | 库内卡片 | `data/brain.db` + `cards/<领域>/<id>.md` |
| L4 | 库内卡片 | `data/brain.db` + `cards/gen/<id>.md` |
| L4 索引 | 派生文件（读时按需重建）| `data/l4_index.md` |
| 立体目录 | 派生文件（读时按需重建）| `data/orient.md` |

**L1 是统一卡片体系中的一张卡**（008 §10）：
- 字段：`layer=L1`、`type=source`、`id=<领域>:<书>:src:<单元ID>`（008 §15）
  - 例：`llm:harness:src:08`、`fin:tianwei:src:03`
- `content` 保存 markdown 全文（方案 C，008 §16）——可被 search 命中
- `source` 字段保留原始 markdown 路径（008 §22）
- 自动维护 use_count / search_count，**走 cards 表统一活跃度**——无旁路表（早期 `l1_files` 旁路表已合并到 cards，启动时 init_db 检测到旧表自动迁移后 drop）
- 文件系统上的 markdown 是 L1 卡的全文来源/镜像，不是交互身份本身
- 与 L2/L3/L4 共用同一交互模型：search、read-cards、neighbors、graph、suggest-links 都覆盖 L1

**相邻层级 ID 对照**（008 §15）：
- L1 source card：`fin:tianwei:src:03`
- L2 主题卡：`fin:tianwei:03`
- L2 深卡：`fin:tianwei:03a`
- L3：`fin:3a`
- L4：`gen:1a`

**namespace 按 layer 区分**：
- **L1**（原文卡）：`<领域>:<书>:src:<单元ID>`——与 L2 同域同书，便于追溯原文
  - `单元ID` 不是卢曼 ID，而是原材料内部的天然单元标识：章节号、节号、`full` 等（如 `src:03` / `src:12a` / `src:full`）。它只负责定位 L1 原文单元，不表达卡片网络中的思想位置。
- **L2**（一书一体系）：`<领域>:<书>:<卢曼ID>`——必须带书（消化某本书的产物，可追溯到 source）
  - 例：`llm:harness:01`、`llm:harness:12a`、`fin:tianwei:03`
- **L3**（跨材料生成）：`<领域>:<卢曼ID>`——**永远挂领域下**（基于 L2 生成，L2 必有领域归属）
  - 跨领域通过 link 联结（如 `fin:3a` link `phil:2b`），**不用 gen**
  - 例：`fin:3a`（金融领域综合判断，可能 link 哲学/医学等其他领域卡）
- **L4**（元层模式）：`gen:<卢曼ID>` 全跨域——L4 是思考方式本身，天然脱离具体领域
  - gen namespace **只给 L4**，L3 不用
  - 例：`gen:1a`、`gen:2b1`
- 领域（按 `sources/XX-领域/`）：`llm`/`fin`/`med`/`law`/`sw`/`phil`/`prod`/`fit`/`psy`/`hist`/`soc`/`sci`
- 书（L1/L2）：英文取关键词（去停用词），中文取拼音；格式 `[a-z0-9]+(?:[-_][a-z0-9]+)*`
- L1 单元：材料内部天然编号，允许用单个连字符或下划线分段；格式同书 ID（如 `11_2`）
- **卢曼 ID 格式**：完整匹配 `[0-9]+(?:[a-z]+[0-9]+)*(?:[a-z]+)?`（如 `01`/`12a`/`12a1`/`12ab`）
- 跨书/跨领域关联通过 link 实现（不依赖 namespace 同名）

**卢曼树规则**：

- 同 namespace 内，卢曼 ID 表达笔记的**主生长路径**：`children` / `siblings` 按 ID 前缀推导父子/兄弟关系。
- 父级 ID 必须先存在，子节点才能派生；解析不得跳过非法字符。
- 父卡必须有独立认知内容，禁止为满足 ID 或门禁创建空目录父卡。
- 兄弟节点共享同一父级 ID。
- 普通删除在目标卡存在子节点时必须拒绝；子树迁移或递归删除应使用独立的显式操作。
- **L1 不进卢曼树**：L1 是原文材料，位置由材料结构决定，与 L2/L3/L4 的关系通过 `source` 和 `links` 表达。
- 跨层关系（如 L3 生长自 L2）由 `links` 表达，不由 ID 嵌套表达。

### 2.2 层间依赖关系

- **L1 → L2 → L3 垂直依赖链**：
  - L2 依赖 L1：消化需要原文。L2 卡 `source` 字段**固定指向 L1 source card id**（008 §21）。
  - L3 依赖 L1L2：生成需要素材 + 理解。§五计算校验强制 L3 必须 link 至少一张 L2（008 §20，L1 可补充但不满足门槛）。
- **L4 跨层独立**：L4 不依赖具体材料，是元层思考方式，走独立提案通道（§九 9.2）。
- **L4 的血肉通过 link 横向连接**：L4 模式卡主动 link 它的血肉——实例（L3 案例）、边界反例（L3 反思）、抽象来源（L2 判断/案例）、关联（其他 L4）、必要时原文（L1 source card）。L4 卡的 links 字段挂着它的整个思考体系。L4 跨域锚定门槛只统计 L2/L3（008 §19）——L1 可 link 但不计门槛。

### 2.3 L1 切分原则

**一个 L1 markdown = 一个可独立消化的材料单元 = 一个 L2 主题卡。**

"可独立消化的材料单元"按材料类型自然切分（不咬死"章节"）：

| 材料类型 | 一个 L1 单元 |
|---|---|
| 书 | 一章或一节（视章节粒度） |
| 论文 | 一篇 |
| 文章/博客 | 一篇（单元 ID 可用 `full`） |
| 视频转写 | 一段主题连贯的内容 |
| 对话记录 | 一段主题连贯的对话 |

判定标准（材料无关）：**单元内部主题连贯、可独立消化，不需要其他单元的上下文也能读懂**。

切分决策是 Plan 层的事——主 agent 拿到材料时先判断切成几个 L1 单元，每个单元对应一个消化子任务。单元标识用材料内部的天然编号（章节号、段落号、"全文"等）。

### 2.4 路径约定

```
sources/
  01-金融/02-Bernstein-与天为敌/03-伯努利与期望值.md   # L1 markdown（L1 卡的 content 来源/镜像）
  07-LLM/24-HarnessEngineering/ch08.md

cards/
  fin/tianwei/03.md                                    # L2 卡镜像
  fin/3a.md                                            # L3 卡镜像
  gen/1a.md                                            # L4 卡镜像
  fin/tianwei/src/03.md                                # L1 source 卡镜像（type=source）

data/
  brain.db                                             # SQLite 主库
  l4_index.md                                          # L4 索引（派生物）

/tmp/loom_task/<task_id>/                                # 任务工作区（不进 git）
  plan.json
  drafts/                                              # agent 写在这里，不直接入库
  staging/                                             # L4 提案（§3.4，绕开 ready drafts 入库链路）
  result.json                                          # 子 agent 输出
```

### 2.5 L2 入口主题卡

每份 L1 材料对应的 L2 卡集合中，**主题卡（type=主题）是入口，必须最先建立**。

主题卡是第一遍读 L1 时建立的全局视野——整体论点、结构骨架。它不是 L2 消化的产出之一（和其他 L2 卡并列），而是 L2 消化的**起点**。004 的定位：L2 agent 启动时读 L1 全文 + 主题卡，就有"已经读过一遍"的效果——模拟人脑第二遍读书的状态（记得结构和论点，按需精读细节）。

落地在 DIGEST 流程（§九 9.1）：先写主题卡，再写其余 L2 卡，其余 L2 卡 link 主题卡。**plan.json 用 `phase` 字段硬化两阶段**——`phase=scout` 时只允许写主题卡，`phase=deep` 时只允许写其余 L2 卡，由 write-draft 强制（防单阶段绕过）。

### 2.6 type 系统的两条说明

- **L4 没有"元概念"**（004 第 51 行）：L4 的 type 受限为 {模式, 判断, 反思}，不含"概念"——这是为了阻止在元层造抽象词（"元认知""系统性思维"这类）。元层内容必须通过具体的可迁移结构（模式）或基于材料的哲学级结论（判断）表达。这条约束由 layer×type 矩阵校验物理保证（§五 5.3），无需额外校验。
- **比较不作为独立 type**（004 第 53 行）：8 种 type 中没有"比较"。比较关系通过两张卡之间的 link + 各自卡内的说明表达。type=比较 会被 type_valid 校验直接拒绝（不在合法集）。

### 2.7 L4 卡的血肉是网络

L4 卡（尤其是模式卡）不是一句抽象原则，是有血肉填充的思考体系。005 对 004 的工程化解释是：血肉**不全塞进 L4 卡本身**，而是由 L4 本体和 link 网络共同承载：

- L4 卡本身：自足表达核心结构命题、成熟度、基本适用边界（第一段承担索引功能，但整卡不能空心化）
- link 血肉网络：跨域实例、适用边界、反例、关联应用——分布在 **L4 link 到的 L2/L3/L4 卡**里

"L4 卡的血肉" = **L4 本体 + 以这张 L4 为中心的 link 网络**。检索 L4 时先读本体命题，再通过结构遍历（neighbors/children）看到连接的实例卡、反思卡、关联卡——这些共同构成血肉。

落地：
- SKILL.md 教 agent "建 link 表达血肉"，不教"塞实例进卡"
- 新 L4 提案必须通过 `l4_links_lower` 机器校验：至少锚定两个不同领域的 L2/L3 卡；零 link 的新 L4 直接拒绝，不走 WARN
- commit-l4 时人类审核看 link 网络，不只看单卡

### 2.8 卡字段结构

**L2/L3/L4 卡**（004 已定，此处重申）：

```
id            namespace + 卢曼 ID（如 fin:3a、gen:1a）
title         短标题
type          8 种认知 type 之一（L4 受限为 模式/判断/反思）
content       消化内容。L4 卡第一段是索引（核心命题 + [探索期/熟练期]）
links         双向图邻接边
source        按 layer 区分（见下）
origin        认知主导来源；默认 ai，明确人工沉淀时为 human
tags          卡片本体上的 tag 值列表；默认空，由人显式维护
use_count     累计使用次数（自动维护）
search_count  被 search 命中次数（自动维护）
```

**`origin` 与 `tags` 的 Harness 边界**：

- `origin=ai` 是内部默认值，不要求 agent 或人每次显式传；只有用户明确表示这是人的想法、手写卡、人工沉淀时，才写 `origin=human`。
- `tags` 是卡片本体上的 tag 值列表，`cards.tags` 是 SSOT；`card_tags(card_id, tag)` 只是由 Harness 自动维护的筛选索引，可通过 `loom-admin rebuild-tag-index` 从 `cards.tags` 重建。
- AI 不主动根据内容给卡打 tag。tag 只来自人的明确要求，后置通过 `loom-admin tag-card --add/--remove` 维护。
- tag 只做展示、筛选和统计；不参与 embedding / FTS 排序，不改变计算校验强度。

**type 体系（008 §13 方案 B）**：
- 认知 type（8 种）：`概念 / 结构 / 机制 / 案例 / 判断 / 反思 / 模式 / 主题`
- card type：8 种认知 type + `source`
- `source` 只能用于 `layer=L1`，不是认知 type，是 L1 原文卡的 card type

**`source` 字段按 layer 的语义**（008 §17/§21/§22）：

| layer | source 语义 |
|---|---|
| L1 | 保留原始 markdown 文件路径（相对项目根） |
| L2 | **固定指向唯一的 L1 source card id**（如 `llm:harness:src:08`） |
| L3 | 不强制；依据靠 links 表达 |
| L4 | 不强制；依据靠 links / proposal |

**card layer 与 task target layer**（008 §12/§25）：
- card layer：`L1 / L2 / L3 / L4`（数据库 `cards.layer` 只能取这 4 个）
- task target layer：`L1 / L2_light / L2 / L3 / L4`（plan.json 用）
- `L2_light` 是轻量消化任务目标；产出的卡仍是 `layer=L2`
- `L1_only` 改名为 `L1`（008 §12）

## 三、Context 工程与 L4 激活

### 3.1 三层信息架构

把 004 的 L1-L4 分层映射到 Context 的信息密度层级（Harness Engineering ch08）：

| Context 层 | Loom 对应 | 信息密度 | 淘汰优先级 |
|---|---|---|---|
| 层 0：System Prompt | AGENTS.md + skill 内容 | 极高 | 绝不淘汰（写保护） |
| 层 1：任务规范 | plan.json | 高 | 任务内不淘汰 |
| 层 2：工具返回 | read-cards / search 结果 | 中 | 按相关性管理 |
| 层 3：状态快照 | drafts 进度、已读卡清单 | 视任务 | 按需刷新 |
| 层 4：历史摘要 | 早期查询、已完成步骤 | 低 | token 上限前优先淘汰 |

**立体目录**（`data/orient.md`）在使用方主动调 `orient` 时进入层 2——不强制注入，由 skill 流程在使用时拉取。

### 3.2 L4 激活的物理实现

**本质定位**（004 第 160, 197 行）：Loom 不是被检索的知识库，是**激活 LLM 思考能力的脚手架**。LLM 预训练已内化大量模式（远比 Loom 卡片库丰富），Loom 的价值不是"提供模式"，而是 ① 扩展思考方向 ② 提供具体血肉（跨域实例/边界/反例）③ 约束 LLM 不跑偏 ④ 沉淀用户特异性模式 ⑤ 可追溯记录。

004 的"思考-激活"机制在 005 落地为三层操作：

```
[启动]
  使用方（THINK/USE 子 agent）启动后主动调 bin/loom orient
  → 返回立体目录（namespace 全貌 + L4 全量含核心命题摘要）
  → 目录格式：
     ## loom 全貌
     N 张卡，M 个 namespace：llm (n) / fin (n) / ...
     ## L4 元层模式
     ### `gen:1a` <标题>
     <核心命题摘要>

[思考中]
  LLM 按需 read-cards(l4_id) 读完整 content（渐近式加载）
  一条或多条，不预设数量——思考需要几条就读几条
  周期性 read-l4-index 重读轻量版（仅 L4 标题列表）

[可观测 + WARN]
  cmd_read_cards 记录本次深读到 /tmp/loom_task/<tid>/.read_trace.jsonl
  cmd_read_cards 记录 L4 引用到 /tmp/loom_task/<tid>/.l4_refs
  cmd_stop_check 末尾输出 L4 引用统计
  - THINK/USE 任务零引用 → WARN（不拒，提醒优化 skill）
  - DIGEST 任务预期零引用（DIGEST 完全 L4-blind），不 WARN
```

**为什么不做 SessionStart 自动注入**：

- 早期版本曾用 SessionStart hook 自动注入 L4 索引，实践中发现两个问题：
  1. 被动接收：AI 把它当背景信息，不触发"思考激活"——启动时 L4 索引只有 1 张卡，AI 看到后误判 loom 几乎空，跳过检索
  2. 时机错位：session 开始时不一定进入 loom 心智，注入被无效消耗
- 改为使用方主动 `orient`：进入 loom 心智时主动拉取立体目录，是"思考激活"动作而非被动接收
- 副作用是 AI 必须在 skill 流程中显式调 `orient`——这是 prompt 引导的（SKILL.md/USE.md/THINK.md 第一步）

**为什么不做"强制周期性重读 L4"**：

- §3.3 自己的边界：「思考时是否真用上 L4」**不强制使用**——强制使用会扭曲思考（agent 合规式调用，不真用）
- 但 THINK/USE 启动时 `orient` 是强制的（强制读入 context ≠ 强制使用）
- draft 数不是 context rot 的好指标：DIGEST 的 draft 离散且场景不读 L4；THINK/USE 的 draft 少（1-5 张），用 draft 数触发频率不合理
- AI 思考遇到相关问题时自然 read-cards(L4)，靠 prompt 引导 + 长期 WARN 反馈优化

harness 的真正力量在**可观测 + 反馈闭环**——记录、统计、WARN、用户决策——不是合规式强制。

**派生物的两个文件（统一 pull 模式）**：
- `data/orient.md`：立体目录（namespace 全貌 + L4 全量含命题摘要），`orient` 命令带缓存按需重建
- `data/l4_index.md`：L4 简短索引（单行列表），`read-l4-index` 命令带缓存按需重建，用于思考中轻量重读

**pull 模式的设计**：派生物是缓存，缓存应该 lazy（pull），不该 eager（push）。commit/commit-l4/apply-card-edit/update-card/delete-card 等写入路径**不**触发派生物重建；读时按派生物内容选择失效判据：
- `orient` 包含 namespace 全貌 + L4 摘要，因此检查全库 `MAX(cards.updated_at)` vs `data/orient.md` mtime
- `read-l4-index` 只包含 L4 简短索引，因此检查 L4 卡 `MAX(updated_at)` vs `data/l4_index.md` mtime

这样写入路径更干净，派生物永远按需生成。

手动重建入口：`loom-admin rebuild-l4-index` 特权命令（少用，只在调试或批量重建时调）。

### 3.3 L4 激活的合约边界（不强制）

L4 激活是"思考时主动用上"，**不能事前强制**（强制就扭曲思考行为，违反激励相容性）。harness 能做的只有：

| 能强制的 | 不能强制的 |
|---|---|
| 立体目录可被 orient 主动读取 | 思考时是否真用上 L4 |
| 周期性重读（prompt 引导动作） | 选择了哪张 L4 |
| trace 记录所有 read-cards 调用 | 选错了怎么办 |

长期统计某次思考全程零 L4 引用 → WARN（不是失败），提醒优化 skill。**不阻断**，因为选错是正常探索，靠反馈纠正。

**THINK/USE 的探索铁律（Skill 强制）**：

Skill 必须把“让 Loom 已有认知尽可能参与对问题的重构”定义为任务完成条件，并以独立、醒目的强制性章节呈现，不能埋在工具列表或可选建议中。认知任务执行时：

- 必须同时打开“地图 → 问题”与“问题 → 网络”两条路线，它们可以交替、回返和相互产生新方向，不规定固定顺序。地图路线不能用“已经运行 `orient`”代替，必须把其中的模式当作候选假设向问题投射，并按需深读本体与关联血肉。
- 问题路线不能止于一次泛化或同义改写；必须提出彼此真正不同的多种紧凑结构假设，为各自生成检索改写，再做多路召回。结构假设没有固定句式、维度表或数量配额。
- 首次命中只是起点。只要还能提出可能实质改变当前理解的未验证方向，就必须继续查询、深读、关联和反证；不得因为已找到文本相关信息而提前结束。
- 向量或混合检索只负责召回候选；采用模式前必须深读双方 content，并由 LLM 判断结构等同性、边界和反例。某一种抽象或改写返回重复结果，只代表该局部路线耗尽。
- 探索以语义饱和为停止条件，不以调用次数、卡片数或固定轮数为条件。强制的是主动探索和继续深入的责任，不强制采用某张卡或得出某种结论。

模式仍是少数 L4/L3/L2 网络节点及其 links，不为每张卡生成结构字段或独立结构索引。这项合约的落点是 Prompt 驱动的执行行为，不是事后根据工具调用数做形式检查；Harness 的 trace / WARN 只作为长期诊断信号，不替代 Skill 对思考过程的驱动。

### 3.4 L4 的双向机制

004 明确 L4 有两个方向，005 必须同时承载：

**应用方向（L4 → 思考）**：已沉淀的 L4 模式在思考时被激活——THINK/USE 主动 `orient` 拉取立体目录，按需深读 L4 本体与 link 血肉，思考中按需 `read-l4-index` 轻量重读。这是 §3.2 已讲的机制。

**形成与演进方向（思考 → L4）**：新的思考洞察可能产生新的 L4 模式，或修正/丰富已有 L4 卡。这个方向的落地：

- **触发**：工作 agent 思考时通过显式工具 `bin/loom propose-l4` 表达提案（不是 hook 强制，是 prompt 引导——发现真模式时 agent 自然想表达）。propose-l4 不阻断任务，提案是副产物。若提案锚定当前任务刚生成的 L2/L3 卡，必须等这些卡通过 `commit-ready` 入库后再正式提案；Deep 过程中只能记录候选，避免 L4 proposal 引用未通过语义层的 draft。
- **载体**：提案写到 `/tmp/loom_task/<task_id>/staging/<proposal_id>.json`（与 drafts/ 物理隔离，绕开 ready drafts 入库链路，保留人类审核关口）。**JSON 是唯一 SSOT**——无额外表（早期 `l4_proposals` 表已废弃，启动时 init_db 检测到旧表自动 drop）。
- **回流**：主 agent 在任务收尾后检查 staging/，把提案摘要汇报给用户。流程：
  1. 主 agent 向用户汇报 proposal 内容（核心命题、跨域真实性、边界、related_cards）
  2. 用户决策后，主 agent 调 `loom-admin proposal-decision <proposal.json> --decision=approved|rejected [--reason="..."]`（封装改 JSON 的 status 字段，避免主 agent 手改 JSON；reason 写入 JSON `decision_reason` 字段）
  3. approved 状态：主 agent 调 `loom-admin commit-l4 <proposal.json>` 入库（CLI 校验 status=approved，否则拒绝）
  4. rejected 状态：proposal JSON 留在 staging/ 不删（事后审计 trail）；committed 后 status 改为 `consumed` 防重复入库
  5. 若 rejected 原因只是 L4 `target_id` 冲突而内容仍有价值，必须先选择未占用 `gen:<卢曼ID>` 重新 `propose-l4`，并在汇报中保留“旧提案 → 新提案”的映射；只有内容本身不成立时才允许单纯 rejected 结束
  6. L4 索引采用 pull 模式，下次 `orient` / `read-l4-index` 发现缓存过期时按需重建
- **已有卡的修正**（应用反哺演进）：发现需要修正已有卡时，用 `bin/loom propose-card-edit <task_id> <card_id> --type=修正|补充|重写|更新 --content-file=<完整新版> [--related=<新增link1,新增link2>]` 提修正提案（子 agent 先 read-cards 获取原内容，编辑成完整新版后提案；新增 related links 与完整新版一起跑机器校验），走同样的回流（staging → 用户审核 → `loom-admin apply-card-edit <proposal.json>` 入库全量替换并追加 related links）。L2/L3/L4 通用——L4 补边界/反例、L3 修正判断前提、L2 修正理解偏差。

**应用反哺演进**（004 第 16 行）：L4 不是静态库，每次被调用都可能产生修正信号（这个例子不适用、这个边界要收紧、这个反例要记录）。这些信号通过上述闭环回流到 L4 卡。

两个方向构成 L4 的完整生命周期：应用时被激活，应用后可能演进。005 的 harness 必须两端都支撑——只支撑应用方向，L4 退化成静态模式库；只支撑演进方向，L4 脱离实际使用变成空中楼阁。

## 四、Plan 层：约束性合约

### 4.1 plan.json 极简结构

每次任务（消化/思考/问答）主 agent 写一份 plan.json，**是任务描述，不是预测**：

```json
{
  "task_id": "abc123",
  "task": "消化《与天为敌》第 3 章",
  "source": "fin:tianwei:src:03",
  "layer": "L2",
  "phase": "scout",
  "skill": "DIGEST"
}
```

通用字段：
- `task_id` — 任务标识，隔离 drafts 目录
- `task` — 任务描述
- `source` — L1 source card id（或路径）；L1 任务的 source 是 markdown 路径
- `layer` — task target layer，合法集 `{L1, L2_light, L2, L3, L4}`（008 §25；`L2_light` 不产出独立 card layer，draft 仍写 `layer=L2`）
- `skill` — 子 agent 用哪个 skill 流程（DIGEST/THINK/USE）

**DIGEST-L2 两阶段强制字段**（008 §6）：

| phase | 强制字段 | 说明 |
|---|---|---|
| `scout` | `phase=scout` | Scout 子 agent 通读整本书，建每章主题卡 |
| `deep` | `phase=deep` + `topic_card=<已 commit 的主题卡 id>` | Deep 子 agent 精读每章，写其余 L2 卡 |

由 write-draft 前置拦截（Scout 只能写主题卡；Deep 不能写主题卡且 plan 必须有已 commit 的 topic_card）。

**plan.json 不包含校验逻辑**——校验是 hook 代码的职责，不是任务描述的职责。约束声明和执行代码物理上在一起，无法漂移。

**THINK 任务另有 `think_state.json`（单书覆盖清单）**。它不是第二份任务合约，而是一本账：记录"这本书有哪些主题、每个主题被哪张产出承接、或为什么明确不深挖"。

- `think-init <task_id> --goal=X --materials=<domain>:<book>`：从该书的全部已入库主题卡（type=主题）生成覆盖清单，写入 `/tmp/loom_task/<task_id>/think_state.json`。主题是**验收单位**，不是执行单元——不规定顺序、不强制逐个处理。
- 探索过程完全自由（004 双路线螺旋不变），think_state 不干预；agent 通读主题清单后自己从 L2 血肉里挑深挖对象。
- `think-skip <task_id> <主题卡id> --reason=X`：对明确不深挖的主题记一句具体理由（必须建立在 skim/深读基础上，理由是否成立由语义层判）。
- `write-draft / propose-l4 / propose-card-edit` 带 `--from-topics=<主题卡id,...>`：声明该产出承接了哪些主题；CLI 校验主题 id 必须在本任务 think_state 清单里。
- 收尾时 stop-check 整批校验覆盖账（§5.4）：每个主题要么被至少一张产出 `--from-topics` 承接，要么有 think-skip 理由；不允许静默跳过。
- 单书 THINK 是单 agent 场景，没有 worker 分配、session 绑定、退出拦截这些并发机制；多材料并发是后续独立设计，不在此规格内。

### 4.2 为什么不预测卡数

004 密度门禁第 1 条：**密度由材料的认知结构决定，不由字符数决定。**

消化一本书的章节之前，无法预知出几张卡、什么 type。预测卡数 = 强行套用机械密度，违反 004 的核心原则。plan 只描述"任务是什么 + 该满足什么约束"，不描述"产出必须是几张什么卡"。

### 4.3 layer 能否提前定

**主目标 layer 能提前定，L4 涌现不能。**

| 场景 | layer | 说明 |
|---|---|---|
| 消化新材料（DIGEST） | L2 | 004：消化时 L3L4 不建（除非材料本身触发明显生成/模式） |
| 深入思考（THINK） | 开放 | THINK 不预设产出 layer，可能是 L3/L4/更新已有卡/无产出 |
| 回答具体问题（USE） | L3（仅沉淀时） | 默认只回答；确认沉淀后写 L3 实践卡/反思卡 |
| L4 演化 | 不能 | L4 是从多个 L2L3 抽象出来的，不是一次任务定的 |

L4 在过程中涌现走 ch13 的 Skill 闭环（提案→人类审核→提交），本来就不该自动入库。

### 4.4 价值判断（Bacon 分级）

DIGEST 流程的第 0 步是价值判断——主 agent 读 L1 后，判断这份材料值得跑到哪层。结果写入 plan.json 的 layer 字段：

| layer | 含义 | 产出 |
|---|---|---|
| L1 | 只注册 L1 source card，不消化 | L1 卡（type=source） |
| L2_light | 轻量消化（核心几张卡，不穷尽） | 主题卡 + 少量 L2 卡（card layer 仍为 L2） |
| L2 | 完整消化到 L2 | 主题卡 + 其余 L2 卡 |
| L3 | 跨材料生成（THINK/USE 沉淀场景） | L3 卡，必须 link L2 |
| L4 | 元层演化（罕见，通常走提案） | L4 卡 |

判断依据是材料的信息密度、与现有知识网络的相关性。**不强制套用固定档位**（如 Bacon 的"浅尝/吞下/嚼/消化"四级），但**必须有这个判断动作**——判断结果决定 layer 字段。

**低价值材料也保留在 sources/**——sources/ 是资源库（按 AGENTS.md 资源管理规范，所有下载资源必须整理到 sources/），保存与消化是两件事。L1 的材料入了 sources 但暂不消化，未来按需可升级到 L2。

校验：plan.layer 必须在合法集 `{L1, L2_light, L2, L3, L4}` 里，否则拒绝；card layer 必须在 `{L1, L2, L3, L4}` 里（008 §25）。

## 五、Tool 层：操作算子与最小权限

### 5.1 工具集（CLI 实现）

Loom 工具暴露给 Claude 的方式是 **CLI 脚本**（延续老 tools.py 的形态，但重构）。agent 通过 Bash 调用。

**基础工具**（工作 agent 用）：

```
bin/loom import-source <source_id> --title=X --path=<md>  # 注册 L1 source card（layer=L1,type=source），008 §24
bin/loom read-source <id|path>                 # 读 L1 source card 全文（id 优先；path 兜底未导入文件），008 §14
bin/loom write-draft <task_id> <card_id>       # 写 draft（内联 14 条计算校验 + Scout/Deep phase 前置拦截）
            --type=<type> --source=<L1 source card id|原始 path> --links=<a,b>
            --content-file=<file>
            [--layer=<L1|L2|L3|L4>]           # 可选；省略时从 plan.json.layer 推断（L2_light 自动归一为 L2）
            [--origin=human]                  # 仅在人明确要求人工沉淀时传；默认 ai，不支持 tags
            [--from-topics=<主题卡id,...>]     # THINK 任务：声明承接哪些主题（须在 think_state 清单内）
bin/loom read-cards <id> [<id>...]             # 读一张或多张卡（bump use_count + 记录 read trace / L4 引用归属 task）
            [--task-id=<task_id>]              #   L1 卡默认返回 snippet/content_size，不返回全文（008 §23）
bin/loom search <query> [--tag=<a,b>]          # tag 多选按 AND 过滤；tag 不参与排序
bin/loom orient                                # 启动时定位——读立体目录（namespace 全貌 + L4 含命题摘要）
bin/loom read-l4-index                         # 思考中周期性重读 L4（轻量，仅标题列表）
bin/loom think-init <task_id> --goal=X         # 单书 THINK：从该书主题卡生成覆盖清单（§4.1）
            --materials=<domain>:<book>
bin/loom think-skip <task_id> <主题卡id>        # 对明确不深挖的主题记具体理由（须先 skim/深读）
            --reason=X
bin/loom think-coverage <task_id>              # 查看当前覆盖账：各主题 → 承接产出 / skip 理由 / 未结
bin/loom propose-l4 <task_id> <card_id> --title=X  # L4 新模式提案（agent 显式指定 gen:<卢曼ID>；提案时跑完整机器校验，写 staging 不阻断任务）
            --content-file=X --related=<a,b> --type=<模式|判断|反思>
            [--from-topics=<主题卡id,...>]
bin/loom propose-card-edit <task_id> <card_id>  # 已有卡修正提案（L2/L3/L4 通用，写入 staging 完整新版）
            --content-file=X --type=<修正|补充|重写|更新> [--related=<新增link1,新增link2>]
            [--from-topics=<主题卡id,...>]
bin/loom mark-ready <task_id>                  # 标记 drafts 完成（stop-check 扫描入口）
bin/loom commit-ready <task_id> --semantic-passed  # 语义质检通过后整批入库
```

**诊断工具**（008 §9，工作 agent 按需调用，只列不改）：

```
bin/loom silent-cards [--min-age-days=N]       # 沉默卡（use_count=0 AND search_count=0），可能过时/孤立/从未被需要
bin/loom l4-upgrade-candidates [--use-count=N] # L4 升级候选（基于 use_count + 反思数 + 跨域），只列不升
            [--reflections=N] [--domains=N]
```

诊断工具不自动调用（SessionStart 不注入），agent 在 THINK/USE 过程中按需调用。升级决策仍由人类审核。

**特权工具**（hook / 主 agent 用户审核后；通过 `bin/loom-admin` 入口）：

```
bin/loom-admin stop-check <task_id> [--mode=normal|salvage]   # 计算层 + 写 sample + block 状态
bin/loom-admin stop-check-pending                            # 扫所有 .ready task 跑 stop-check
bin/loom-admin proposal-decision <proposal.json>             # 封装 staging JSON status 流转（approved/rejected）
              --decision=approved|rejected [--reason="..."]   #  主 agent 用户审核后调用，JSON 是 SSOT 无额外表
bin/loom-admin commit-l4 <proposal.json>                     # L4 提案入库（不再复跑机器校验）
bin/loom-admin apply-card-edit <proposal.json>               # 已有卡更新入库（全量替换 content）
bin/loom-admin update-card <card_id> [--field=X]             # 更新已入库卡字段（跑校验 + 重新 embed；可修正 origin）
bin/loom-admin tag-card <card_id> --add='["X"]'              # 人明确维护 tag：增量添加，自动去重
bin/loom-admin tag-card <card_id> --remove='["X"]'           # 人明确维护 tag：增量移除，不存在则 no-op
bin/loom-admin delete-card <card_id>                         # 删除卡片（cascade）
bin/loom-admin rebuild-l4-index                              # 重建 L4 索引（pull 模式派生物）
bin/loom-admin rebuild-tag-index                             # 从 cards.tags 重建 card_tags 派生索引
bin/loom-admin rebuild-embeddings [--batch-size=N]           # 用当前 provider/model/dim 重建 cards_vec
```

注：旧的 `loom-admin commit` 入口已移除（008 §5）——它绕过 `.computed_passed.json`、语义自检和 `--semantic-passed`。

**探索工具**（低摩擦翻阅姿势）：

```
bin/loom search <query> [--mode=hybrid|fts|vector] [--top=N] [--ns=X] [--type=X]
            # hybrid（默认）：FTS + 向量融合排序（RRF）
            # fts：纯关键词，抓字面
            # vector：纯语义相似

bin/loom browse-tree <namespace>               # namespace 主题树（→ 主题卡 → 子卡）
bin/loom skim <card_id>                        # 轻量浏览（title + 首段 + links，不 bump use_count）
bin/loom wander <card_id> [--steps=N]          # link 图随机游走（打破信息茧房）
bin/loom suggest-links <card_id> [--top=N]     # 找未 link 的语义近邻（构建阶段补 link 缺口）

bin/loom browse <namespace> [prefix]           # 浏览 namespace 的卡（扁平列表）
bin/loom children <card_id>                    # 某卡的子卡（卢曼 ID 前缀）
bin/loom siblings <card_id>                    # 某卡的兄弟卡
bin/loom neighbors <card_id> [--depth=N]       # link 图遍历（无向）
```

search 的 hybrid 模式是默认——FTS 抓精确术语，向量抓同义改写，两者融合（RRF：Reciprocal Rank Fusion）覆盖更全。单独用 fts 或 vector 是特殊情况。

向量化是 Loom 的核心能力，但 provider 不是核心依赖。`LOOM_EMBED_PROVIDER` 支持本地 Ollama、OpenAI-compatible API、Zhipu preset；默认新用户路径推荐本地 Ollama。一个 DB 固定一个 embedding 维度，维度写入 `loom_meta.embedding_dim`；切换 provider/model/dim 后必须显式运行 `loom-admin rebuild-embeddings` 重建 `cards_vec`。卡片和 link 不受影响。

**数据边界**：Ollama 默认只向 `127.0.0.1` 发送文本；OpenAI-compatible / Zhipu 会把卡片或原文片段发送到所配置的远端 endpoint，属于显式选择的内容外发。Workbench 只监听 localhost，前端通过同源代理访问 API，后端不开放跨域读取。`LOOM_HOME` 及数据库、卡片镜像和派生索引默认使用 owner-only 权限。资源转换默认本地执行；远程 OCR 只有显式设置 `LOOM_OCR_REMOTE_HOST` 时才可上传材料。

**embedding 的两个用途严格区分**：

| 场景 | 时机 | 命令 | 为什么 |
|---|---|---|---|
| query → cards | 查询时 | `search` | query 是实时输入，必须实时算 |
| card → cards | 构建时 | `suggest-links` | 已 commit 卡之间的相似度，算一次沉淀进显性 link |

**设计哲学**：link 是显性真相，embedding 是辅助建立 link 的工具——不并存两套关联。查询时只有 search 调 embedding，其他工具全走 link 图。`suggest-links` 是构建阶段的辅助工具，把 embedding 的价值凝结进显性 link 网络。

**任务收尾命令**：

```
bin/loom commit-ready <task_id> --semantic-passed  # 子 agent 语义自检通过后整批入库（防 draft 被偷改）
```

**特权命令**（hook / 主 agent 在用户审核后调用，通过 `loom-admin` 暴露）：

```
loom-admin stop-check <task_id> [--mode=normal|salvage]  # 计算层 + 写 sample + block 状态
loom-admin stop-check-pending                       # 扫所有 .ready task 跑 stop-check
loom-admin proposal-decision <proposal.json>        # staging JSON status 流转（主 agent 用户审核后调用）
              --decision=approved|rejected [--reason=...]
loom-admin commit-l4 <proposal.json>                # L4 提案入库（主 agent 在用户审核后调用，不再复跑机器校验）
loom-admin apply-card-edit <proposal.json>          # 已有卡修正入库（同上，全量替换 content）
loom-admin update-card <card_id> [--field=X]        # 更新已入库卡字段（跑校验 + 重新 embed；可修正 origin）
loom-admin tag-card <card_id> --add='["X"]'         # 人明确维护 tag：增量添加，自动维护索引
loom-admin tag-card <card_id> --remove='["X"]'      # 人明确维护 tag：增量移除；不存在则 no-op
loom-admin delete-card <card_id>                    # 删除卡（cascade link/vec/meta/镜像）
loom-admin rebuild-l4-index                         # 重建 l4_index.md（pull 模式：读时按 mtime 触发）
loom-admin rebuild-tag-index                        # 从 cards.tags 重建 card_tags 派生索引
```

**commit-l4 / apply-card-edit 不复跑机器校验**——校验在 `propose-l4` / `propose-card-edit` 时跑过（拦截不合规提案），入库走人类审核是最终决策点，重复跑校验无价值反而妨碍用户决策。`commit-ready` 是例外：它跑的是 mtime 防篡改校验（draft 在计算层通过后到 commit 之间是否被改），不是内容校验。

**第四种检索（关系骨架翻译 + LLM 结构等同性判断）不是工具**：agent 拿到 L4 模式卡的骨架结构，面对新问题时自己判断"这个新问题的结构是否与该模式等同"。这是 LLM 的推理操作，发生在 THINK/USE 流程里。`orient` 主动读取立体目录（§三）是它的前置——agent 必须先"知道"有哪些模式骨架，才能做结构等同性判断。

**翻阅姿势的交替**（004 第 111 行的螺旋式深入）：THINK/USE 是查询→关联→思考→再查询的螺旋。`orient` 启动定位，`search` 凭关键词找入口，`browse-tree`/`children`/`siblings`/`neighbors` 展开结构，`skim` 快速判断一张卡是否值得深读，`read-cards` 批量深读，`wander` 在熟悉区域之外找意外关联，`suggest-links` 在思考后补全网络缺口。交替节奏由 agent 自己掌握，写在 THINK.md/USE.md 流程里教。

### 5.2 关键设计：agent 的入库路径只有 commit-ready

工具集刻意不含 `commit` / `create_card`（直接无校验入库的命令）。子 agent 能触发的入库命令只有 `commit-ready <task_id> --semantic-passed`——而且必须满足三个前置：

1. `.computed_passed.json` 存在（计算层跑过且通过）
2. drafts mtimes 与 `.computed_passed.json` 记录一致（draft 没在计算层通过后被改）
3. 子 agent 显式声明 `--semantic-passed`（完成语义自检的 flag）

commit-ready 做的是"防篡改"校验（mtime 比对），不是内容校验——内容校验已经在 write-draft（14 单卡）和 stop-check（4 整批）跑过。这是 ch10 最小权限的工程化：**入库只能走这条窄路**，agent 不能绕过计算层 + 语义层直接污染库。

### 5.3 计算型校验内联在 write-draft

校验逻辑写在 `bin/loom write-draft` 脚本内部——agent 调命令时当场校验，失败返回非零 exit code + 错误信息，agent 必须重新设计这张卡。

**为什么不用 Claude Code 的 PreToolUse hook 做校验**：计算校验逻辑简单（正则、计数、集合判断），进程内检查够快；Claude Code 的 PreToolUse hook 是进程外的 shell 脚本，每次工具调用 fork 进程，开销可能比校验本身还大。简单校验内联在工具实现里更高效。

**校验清单**（写死在 write-draft 脚本里，所有任务共用）：

| 校验 | 判据 | 失败动作 |
|---|---|---|
| type 合法 | type ∈ card type 合法集（8 种认知 type + `source`，008 §13）| 拒绝 |
| namespace 格式 | L1=`<域>:<书>:src:<单元>` / L2=`<域>:<书>:<卢曼ID>` / L3=`<域>:<卢曼ID>` / L4=`gen:<卢曼ID>`；完整匹配 §2.1 严格文法 | 拒绝 |
| **卢曼父级存在** | **L2/L3/L4 新卡的父级 ID 必须已存在（库中或同 task drafts）。父级不存在则拒绝（005 §2.1 树规则）** | **拒绝** |
| layer×type 矩阵 | 按 004 的 type×layer 表 + L1={source}（008 §13）| 拒绝 |
| 长度门禁 | content ≥ 30 字 | 拒绝（目录型） |
| link 目标存在 | links 中每个目标必须是已入库卡，或当前 task drafts 中的卡 | 拒绝 |
| L3 必须 link L2 | layer==L3 → 至少一个 link 目标 layer==L2（008 §20；L1 可补充但不满足门槛）| 拒绝 |
| L4 必须跨域锚定 | layer==L4 → links 覆盖 ≥2 个不同领域 namespace 的 L2/L3 卡（008 §19；L1 可 link 但不计门槛）| 拒绝 |
| 反思锚定 | type==反思 → link 目标 type ∈ {判断, 模式} | 拒绝 |
| L4 索引格式 | layer==L4 → content 第一段匹配 `^\[(探索期\|熟练期)\]` | 拒绝 |
| source 真实（按 layer）| L1：source 文件存在；L2：source 是存在的 L1 source card id；L3/L4：不强制（008 §17）| 拒绝 |
| card_id 唯一 | 新卡 id 不与现有卡或同 task drafts 撞 | 拒绝 |
| L2 不跨材料/跨领域/L4 | layer==L2 → links 禁止跨材料（不同 domain:book）、跨领域、L4（008 §1） | 拒绝 |

**几条校验的核心理由**（防止后续维护误删）：

- **link 目标存在**：link 是显性真相，不允许悬空边。单卡校验允许 intra-task 前向/后向引用，只要目标出现在同 task drafts 中；入库后图遍历、neighbors、use_count 才能保持一致。
- **L3 必须 link L2**：大模型不能凭空思考——L3 是生成层，必须锚到 L2 具体消化结论，否则就是空中楼阁。L1 source card 可作为原文证据补充，但不能替代 L2 消化结论（008 §20）。
- **L4 必须跨域锚定**：跨材料不等于跨领域。单领域内反复出现的可迁移结构默认是 L3；只有至少锚到多个不同领域的 L2/L3 血肉，才有资格作为 L4 元层模式。L1 可 link 但不计入跨域门槛（008 §19）。
- **反思锚定判断/模式**：反思是元认知操作，对象必须是认知产物（判断/模式）。概念/结构/机制/案例是材料不是判断，单独支撑不了反思（004 第 51 行）。
- **长度门禁 30 字**：是防目录型的下限，**不是密度目标**——真实密度由认知结构决定（简单概念 30 字够，复杂机制 800 字才讲清，看单元本身。004 第 112 行）。
- **L2 不跨材料/跨领域/L4**：DIGEST 是"本材料内的消化"——本材料内允许 link（表达材料内部结构），跨材料/跨领域/L4 放到 L3 THINK 阶段（008 §1）。"必须 link 主题卡"由整批校验（§5.4）强制，不在单卡内联层。
- **卢曼父级存在**：卢曼树是 004 定义的结构性回忆工具。父级不存在就建子节点，会让 `children`/`siblings` 导航断裂，也让新卡失去在思想序列中的明确位置。
- **父卡不是目录占位**：父级存在门禁只保证树闭合，不授权创建空父卡；父卡仍须通过独立认知单元和内容密度校验。
- **source 按 layer 区分**：L1.source 是 markdown 路径；L2.source 固定指向 L1 source card id（008 §17/§21）；L3/L4 不强制 source，依据靠 links。

这 14 条都是**确定性检查**（集合/正则/SQL），无歧义，零启发式。改校验 = 改脚本 + git 提交。

注意：老 001 的"列举型检测"（"第\d+条"≥3 次）这里**不保留**——001 自己测出来误报率 40%，价值低；列举型让语义层判。

### 5.4 整批校验（stop-check 计算层，不在 write-draft 内联）

有些校验依赖整批 drafts 的状态，单张 write-draft 时无法判断，必须在任务完成时整批检查。这些放在 stop-check 计算层（§六 6.3）：

| 校验 | 判据 | 时机 |
|---|---|---|
| L2 主题卡数量 | scout → ≥1 张 type=主题；deep → ==0 张主题卡 | stop-check |
| L2 卡 link 主题卡 | 非 topic 的 L2 卡 links 必须含主题卡：scout 跳过；deep 校 plan.topic_card 存在且已 commit | stop-check |
| id 整批唯一 | 同 task drafts 内无撞 id（兜底单卡层 card_id_unique，防并行子 agent 撞）| stop-check |
| 无重复卡 | 同 task drafts 内 difflib 相似度 > 0.7 拒绝（防切碎/抄录型重复）| stop-check |
| THINK 主题覆盖 | 任务有 think_state.json 时：每个主题要么被 ≥1 张产出 `--from-topics` 承接，要么有 think-skip 理由；不允许静默跳过 | stop-check |
| THINK 读过才算数 | 任务有 think_state.json 时：drafts 中 L3 卡 link 的每张 L2 必须出现在本任务 `.read_trace.jsonl`（凭标题 link 视为未读）| stop-check |

**注意**：`无重复卡` 是整批层**唯一的启发式**（difflib 0.7 阈值）——单卡层 14 条零启发式，整批层允许这一条，因为"重复"本质是相对判断。

主题卡是 L2 消化的入口（§2.5），必须由 Scout 阶段先建立并 commit。Scout/Deep 的 phase 约束（scout 只写主题卡、deep 不写主题卡）由 write-draft **前置拦截**（read plan.phase → type 不符当场拒），不等 stop-check。Deep 阶段必须在 plan.json 中显式声明 `topic_card`，整批校验在 conn 可用时复核它真实存在且 type=主题。

**注意**：L2 write-draft 强制 `phase ∈ {scout, deep}`（无 phase 时拒绝）。代码中 `check_l2_has_topic` / `check_l2_links_topic` 的 `default` 分支是防御性兜底，正常路径不可达。

### 5.5 read-cards 对 L1 的轻量返回（008 §18/§23）

L1 source card 纳入 search、read-cards、read-source，但 **agent 命令默认不返回全文 content**——L1 卡的 content 可能是几百 KB 的整章 markdown，进入 agent context 会让响应体爆炸、token 浪费。

**agent 命令对 L1 的处理规则**（harness 强制）：

| 命令 | 对 L1 的返回 |
|---|---|
| `read-cards <L1_ID>` | `id/title/type/layer/source/use_count/search_count/snippet/content_size/has_full_content/links`（不返回 content）|
| `search` 命中 L1 | 按 snippet + score 返回（不返回 content）|
| `read-source <L1_ID\|path>` | **显式**返回全文 content——agent 想读 L1 原文时唯一入口 |

核心规则一句话：**agent 调 read-cards 对 L1 默认轻量返回（snippet/content_size），全文必须显式调 read-source**——read-cards 是图谱探索工具，read-source 是原文阅读工具，职责分离。

`read-cards` 与 `read-source` 的边界（008 §14）：
- `read-cards <id>` 可读 L1/L2/L3/L4，但对 L1 默认轻量
- `read-source <id|path>` 是显式读 L1 全文的入口（id 优先；path 兜底未导入文件）
- `read-source` 是读操作，不负责创建——创建走 `import-source`

**前端 API（workbench `/api/*` 端点）属展示层细节，不在 harness 规格内**——前端图谱渲染、详情接口对 L1 怎么返回由 workbench 自己管（实现见 `workbench/backend/main.py` 的 `_brief` / `_full_card`）。harness 规格只管 agent 命令的 L1 处理。

## 六、Hook 层：反馈闭合

### 6.1 Claude Code / Codex hook

不重新发明 hook 框架。Claude Code 和 Codex 都有原生 hook 机制；Loom 在两边都挂 `SubagentStop` / `Stop`。完整安装默认写入全局 hook 配置，因为 stop-check 自动触发是 Loom agent 闭环的一部分；不想改全局 agent 配置时可用 `install.sh --no-hooks` 跳过，或用 `install.sh --project` 写项目级配置。Claude Code 全局配置是 `~/.claude/settings.json`，Codex 全局配置是 `$CODEX_HOME/hooks.json`；项目级分别是 `.claude/settings.json` 和 `.codex/hooks.json`。

关键实现：**guard 和 stop-check 必须在同一个 command hook 里串行执行**。Claude Code 和 Codex 对同事件的多个 command hook 都可能并发执行，不能把 `loom hook-guard` 和 `loom-admin stop-check-pending` 拆成两条 hook 来表达先后。Loom 用单一 wrapper `loom-hook`：先检查 `loom hook-guard`，激活时再跑 `loom-admin stop-check-pending`，只有需要回炉时输出 `decision:block`。

| hook 事件 | 何时触发 | Loom 用途 | 类型 |
|---|---|---|---|
| SubagentStop | 子 agent 退出 | **消化/深化任务完成验收**：扫 `.ready` task → 跑计算层校验 → block 回 agent 做语义自检 → agent 调 commit-ready 入库 | command (`loom-hook`) |
| Stop | 主 agent 响应结束 | 兜底扫描 `.ready` task，防遗漏 | command (`loom-hook`) |

**早期版本曾用 SessionStart hook 自动注入 L4 索引**，后改为使用方主动 `orient`（§3.2）——被动注入会让 AI 把 L4 当背景信息而非思考激活，且 session 开始时不一定进入 loom 心智。

### 6.2 三种结束形态共用一个 hook 入口

子 agent 的三种"结束"情况：

| 情况 | 机制 | hook 触发 |
|---|---|---|
| 正常完成 | 子 agent / 当前 agent 调 `loom mark-ready <task_id>` 写 `.ready` 文件 | Claude/Codex：SubagentStop 或 Stop 触发 `loom-hook` |
| agent 放弃 | 同上（mark-ready 后退出，drafts 空由计算层拒）| 同上 |
| 超时 | 外层 `timeout` 杀进程（来不及 mark-ready）| 主 agent 检测 exit code=124，手动走 salvage |

**多 task 并发**：`.ready` 文件机制让一次 `stop-check-pending` 能处理多个并发 task（扫描所有 ready 的）。每个 task 独立走"计算层 → 语义层 → commit-ready"流程，互不阻塞。`stop-check-pending` 扫描所有 `.ready` task 时，会把各 task 的 `.semantic_sample.json` 聚合为 `/tmp/loom_task/.semantic_sample.json`（方便主 agent 一次性审阅多 task 的抽样情况）。

**session 归属与并发写保护**：主 agent / batch agent 派发任务时可设置 `LOOM_SESSION_ID`，子 agent 继承后写入 `/tmp/loom_task/<task_id>/.session_id` 与 `task_trace.session_id`。`stop-check-pending` 默认只处理当前 session 的 `.ready` task（旧/空 session 兼容处理），避免 batch 模式下扫到别的会话任务并把 block 信号发给无人接收的 agent。`write-draft` 对每个 task 目录使用 `.drafts.lock`，同一时刻只允许一个进程写同一 task；若不同 session 复用同一 task_id，会被拒绝，必须换唯一 task_id。

**batch 打捞**：`loom-admin salvage-pending` 用于列出长时间停在 ready/computed_passed、但未 commit 的 task。默认只报告；带 `--run-stop-check` 时按 salvage 模式重跑 stop-check，仍写 `.semantic_sample.json`，仍需 agent/主 agent 做语义自检后显式 `commit-ready --semantic-passed`，不自动入库。旧参数名 `--auto-commit` 只是兼容别名，不会自动 commit。

唯一需要主 agent 显式处理的是超时（Claude Code 没有原生任务超时）。

### 6.3 stop-check 计算层 + 语义层

**为什么不在多个 hook 里串行跑计算 + 语义**：Claude Code / Codex 的同事件多个 command hook 都可能并发执行，hook 之间没有可靠数据传递通道。计算层和语义层必须有先后（语义自检依赖计算层抽样的 `.semantic_sample.json`），所以不能拆成两个并发 hook。

落地方案是 **block-back 模式**——把语义判断还给当前 agent，不靠外部 judge：

```
子 agent 写完 drafts 调 loom mark-ready → 退出
  ↓
SubagentStop/Stop hook（`loom-hook` command）触发：
  1. 扫所有 .ready 但未 done 的 task
  2. 对每 task 跑计算层校验（14 单卡 + 4 整批；THINK 任务另有 §5.4 两条覆盖校验）
  3. 计算层失败 → 输出 decision:block + 错误清单
     → runtime 让 agent 继续修 draft
  4. 计算层通过 → 写两个文件：
     - .computed_passed.json（draft IDs + mtimes，防 commit 前 draft 被改）
     - .semantic_sample.json（每 task 随机 3 张抽样）
     同时把 task_trace.status 流转到 `computed_passed`（不写 ended_at，中间状态）
     然后仍 decision:block + 提示子 agent 做语义自检
  ↓
agent 被 block 唤回（不是退出）：
  1. 读 .semantic_sample.json
  2. 对每张抽样卡按 §七 四判据自判（type_match / single_unit /
     genuine_digest / self_contained）
  3. 失败 → 修 draft → 重新 mark-ready（重跑计算层 + 重新抽样）
  4. 通过 → 调 loom commit-ready <task_id> --semantic-passed
  ↓
  THINK 任务（存在 think_state.json）的语义自检另有三条必答项，
  与四判据同属 block-back 强制确认清单，不确认不得 commit-ready：
  a. think-skip 的理由是否都建立在真实 skim/深读上且成立
     （凭标题跳过或理由空泛 → 回去补读或改为深挖）
  b. 挑出的深挖对象是否真是全书最有外联价值的 L2——对照主题
     清单自问"这个主题里真的没有更值得深挖的吗"
  c. 每张带 --from-topics 的产出是否忠实承接了声称覆盖的主题
     （只沾边 → 去掉该 from-topics 或充实内容）
  ↓
commit-ready 验证：
  - .computed_passed.json 存在（计算层跑过）
  - drafts mtimes 未变（draft 没在计算层通过后被偷改）
  - drafts 集合未变（没偷偷加新卡）
  全过 → 整批入库（单事务，任一 UNIQUE 冲突回滚整批）→ 标 task_trace.status=done
```

**task_trace.status 状态机**：

主路径很简单：`running → computed_passed → done`。其他状态只是异常/兼容记录，不代表 agent 正常要走复杂分支。

| 状态 | 含义 | 是否终态 |
|---|---|---|
| `running` | 任务进行中（mark-ready 之前） | 否 |
| `computed_passed` | 计算层通过、等语义自检（中间握手状态，不写 ended_at） | 否 |
| `done` | 整批入库成功 | 是 |
| `failed` | 任务被明确放弃、无 drafts、或运行时上限后由主 agent 判定失败；普通计算层 rejected 不立刻终结，仍 block 回 agent 修复 | 是 |
| `timeout` | 外层 timeout 杀进程后由主 agent 记录（若有可用 drafts，可再走 salvage） | 是 |
| `salvaged` | 历史/兼容保留态；当前 salvage 正常仍走 computed_passed → 语义自检 → done，不绕过入库链路 | 是 |

**关键约束**：
- **commit-ready 必须带 `--semantic-passed`**——这是子 agent 显式声明已完成语义自检的 flag，没有此 flag 直接拒
- **mtime 防篡改**：`.computed_passed.json` 记录每张 draft 的 mtime；commit-ready 时若发现某张 draft mtime 不一致 → 拒（要求重跑 mark-ready）
- **没有"hook 自动 commit"路径**——commit 由子 agent 在语义自检通过后**显式触发**，主 agent 验收时查 `task_trace.status=done` 即可
- **task_trace.status==done 时 commit-ready 幂等返回**——防御子 agent 重试场景下的重复调用（不重做、不报错）
- **L4 索引是 pull 模式**（下次 `read-l4-index` 按 mtime 触发重建），不在 commit 时主动触发

### 6.4 Hook 铁律

**Hook 输出止步于诊断与状态机协议提示**（008 §4）：

| 允许 | 禁止 |
|---|---|
| `decision:block` + 失败校验清单（"哪张卡哪条校验失败"）| 具体修复方案（"请改成 X"）|
| 状态机协议提示（"计算层已通过，请读 `.semantic_sample.json` 按四判据自检；通过后走 commit-ready 协议"）| 替 agent 改 content |
| 下一步协议命令名（如 `loom commit-ready <task_id> --semantic-passed`），作为人/agent 可读提示 | hook 自己执行工具调用，或要求 runtime 自动执行工具调用 |
| 抽样 ID 与判据名称（type_match/single_unit/genuine_digest/self_contained）| 把诊断输出伪装成需要立即执行的修复脚本 |

`decision:block` 是框架级控制信号（让 agent 不退出/继续处理），不是执行指令。它只告诉 agent "你有这些问题 / 该做下一步什么类型的动作"。agent 自修复和 commit-ready 调用是它读 reason 后的自主行为，不是 hook 指令的执行。

防 Hook 风暴（ch11 §11.5）：如果 hook 的输出包含工具调用指令，可能被解析成实际执行，触发 hook → 工具调用 → hook 的递归。职责切分（hook 只诊断，agent 才执行）把潜在的递归变成 agent 驱动的线性流程。

### 6.5 回炉上限

Claude Code / Codex 的 Stop/SubagentStop 输入都带有 `stop_hook_active` 一类的续跑状态。Loom 不自己拍一个 N=3 之类的回炉上限；需要防无限回炉时优先使用运行时提供的 stop hook 状态与内置保护。

运行时保护触发后意味着：任务可能未完成，drafts 可能不完整。此时主 agent 收到 hook 的最终错误报告，决定是放弃、改 plan 重试、还是升级到人类。

### 6.6 salvage 模式（超时救卡）

超时不是任务全失败——drafts 里可能有大量好卡。主 agent 检测到 timeout exit code=124 后，走 salvage 路径：

```bash
loom-admin stop-check $TASK_ID --mode=salvage
```

salvage 模式的 hook 行为（008 §2，与 normal 共用同一 block-back 路径）：
- 与 normal 模式相同：跑计算层校验 → 写 `.computed_passed.json` + `.semantic_sample.json` → block 回 agent 做语义自检
- 唯一区别：block reason 文本里标明 `mode=salvage`，让 agent 知道这是"超时救卡"语境
- 子 agent 仍需调 `commit-ready --semantic-passed` 才能入库——salvage 不绕过语义层
- salvage 不是绕过任何校验的捷径，只是允许在不完整 drafts 上跑 stop-check 的入口

## 七、语义层判据

### 7.1 判据来源与权威性

语义层判据**不是 hook 现场发明的**，全部从 004 设计原则推导：

| 判据 | 推导来源 |
|---|---|
| type_match | 004 type 系统（每种 type 回答什么核心问题） |
| single_unit | 004 卢曼 Zettelkasten 原则：卡片是**原子化的**（= 一句问句能概括） |
| genuine_digest | 004 卢曼 Zettelkasten 原则：卡片是**用自己的话写的**（非抄录原文） |
| self_contained | 004 卢曼 Zettelkasten 原则的隐含要求：原子化 = 自足可读（不依赖 source 上下文） |

判据的权威性来自设计文档（004），子 agent 做的是**按既定标准的模式匹配**，不是主观打分。

### 7.2 四项判据

**判据 1：type_match（type 与内容一致）**

```
问题：这张卡的主体内容，是否在回答它声明的 type 该回答的核心问题？

每个 type 的核心问题（来自 004）：
  概念 → "这是什么"（定义/边界/分类）
  结构 → "由什么组成/怎么排列"（静态组织）
  机制 → "如何发生/为什么变化"（因果动力学）
  案例 → "具体发生过什么"
  判断 → "结论是什么/为什么相信/为什么这么做"
  反思 → "某判断/模式的适用条件、例外、反例"
  模式 → "从多个案例抽象出的可迁移结构"
  主题 → "这份材料的整体论点/全局视野"

pass：卡的主体内容在回答对应核心问题
fail：卡的主体在回答别的 type 的问题（type 标错或内容偏移）
```

**判据 2：single_unit（独立认知单元）**

```
问题：这张卡能否用一句问句概括？

pass：一个明确的问句能覆盖全部 content
fail：需要多个不同的问句才能问完；或存在"第X讲A，第Y讲B"的列举结构
```

**判据 3：genuine_digest（真消化非抄录）**

```
问题：这是真消化还是抄录原文？

pass：content 用自己的话重组了 source，引入了 source 没有的结构
      （关联、抽象、举例、对比、跨域连接）
fail：content 是 source 的逐字搬运，或仅做了表面改写（换词不换义）
```

**判据 4：self_contained（独立可读）**

```
问题：不看 source 原文能否独立读懂这张卡？

pass：卡本身信息自足，读者不需翻原文就能理解
fail：依赖 source 上下文才能读懂（如出现"如上文所述"但没有复制上下文）
```

### 7.3 判据的载体与版本管理

判据的权威位置是 `skills/_loom_core.md` 与各模式 skill 的收尾协议（008 §3）。Loom 不引入 prompt hook——保留 stop-check command + skill 协议的组合。

具体落地：
- `cmd_stop_check` 计算层通过后写 `.semantic_sample.json`，并通过 hook block 回 agent
- block reason 只提示"请读 `.semantic_sample.json` 按 type_match / single_unit / genuine_digest / self_contained 自检"
- 判据定义、四项判据的展开、每项的 pass/fail 例子都在 skill 文本里
- 调整判据 = 改 skill 文本 = git 提交

判据**不自动更新**（ch13 §13.1 信息论视角）：自动更新等价于在无验证信号下移动 R-D 工作点，会把已验证的低失真编码替换为未验证的高失真编码。调整判据必须人类审核。

## 八、可观测性（极简）

Loom 是人工驱动、单次消化的系统，不是持续运行的在线服务。Harness Engineering ch12 的黄金集、在线评估、四层指标体系**不适配 Loom**——它们解决的是 Loom 没有的问题（分布漂移、生产流量监控）。

Loom 真正需要的可观测性就两件事，都来自 004：

**1. use_count / search_count 自动维护**

bump 时机（**狭义**——只算 agent 真正把卡作为认知对象，不算辅助工具的图遍历）：

| 操作 | bump use_count | bump search_count |
|---|---|---|
| `read-cards` 显式深读 | ✓ | — |
| `add_link`（被引用方） | ✓ | — |
| `search` 命中 | — | ✓ |
| `neighbors` / `suggest-links` / `browse` / `skim` | ✗（辅助图遍历，不算认知使用） | — |

004 原话："不做复杂分析，只累积 raw data。"后续可用于召回策略、沉默卡提醒、L4 升级信号。

**两个指标的互补含义**（004 第 182-184 行）——不单独看，组合解读：

| 模式 | 含义 | 提示 |
|---|---|---|
| search 高 + use 低 | 搜到了但没用 | 检索精准度问题，或卡片质量不行（搜到了发现没用） |
| use 高 + search 低 | 用得多但搜不到 | 靠 link 或直接 ID 调用，不在关键词/语义检索覆盖内 |
| 双低 | 沉默卡 | 可能过时、可能孤立、可能从未被需要 |
| 双高 | 高价值卡 | 常被检索也常被使用 |

上表是**解读框架**（人或 agent 读 raw data 时怎么理解），不是要实现的分析逻辑。004 明确"不做复杂分析"。已实现的两个诊断 CLI：
- `loom silent-cards` — 列出 use_count=0 AND search_count=0 的沉默卡
- `loom l4-upgrade-candidates` — 列出符合升级信号（use_count 高 + 有反思修正 + 跨域 link）的 L4 候选，**只列不升**，升级仍由人类决定（§9.2）

**reject_log 表**：记录每次 write-draft 拒绝（task_id / card_id / check_id / reason / stage），用于：① skill 流程诊断失败任务（如 `loom-pipeline/SKILL.md` 在 task_trace.status=failed 时查 reject_log 决定重起哪个阶段）② 验收统计密度门禁拒绝率（006 §指标）。是密度门禁可观测性的关键信号源——拒绝率高说明校验严或 agent 质量差，需要看 reason 分布调整。

**2. 任务 trace**

一次任务的记录（起止时间、产出卡清单、回炉次数、最终入库/失败/超时）。`task_trace.committed_ids` 记录最终入库卡清单；`/tmp/loom_task/<task_id>/.read_trace.jsonl` 记录本任务所有 `read-cards` 深读轨迹。用于事后复盘"这次消化为什么卡了"。不需要 ch12 的完整 trace_id/span_id 分布式追踪——Loom 是单机单进程。

砍掉的部分：黄金集、在线评估、A/B 实验、影子模式、四层指标体系、归因方法。

## 九、Skill 层：Loom 自身的演化闭环

### 9.1 四个 skill 文件

| 文件 | 场景（004） | 内容 |
|---|---|---|
| `_loom_core.md` | 共同前置 | 铁律 / 合约 / 工具集 / type 易错点——所有模式 skill 通过 markdown 链接强制 Read |
| `loom-digest/SKILL.md` | 场景 1 消化（L1→L2） | **两阶段**：Scout 通读建主题卡 + Deep 每章并行精读 |
| `loom-think/SKILL.md` | 场景 2 深入思考（L3+） | 反复查询-关联-思考的循环，**不预设产出 layer** |
| `loom-use/SKILL.md` | 场景 3 具体问题（用网络） | 默认回答，必要时提示沉淀 |
| `loom-pipeline/SKILL.md` | 多资源并行全链路 | INGEST/SCOUT/DEEP/PER-BOOK/CROSS 5 阶段编排（实践派生，不在 004 三场景内）|

**没有"总入口" skill**——agent skill 系统靠 name + description 匹配触发，主 agent 按场景直接起对应 skill 的子 agent。

每个子 skill = 一份可复用的 Plan 模板（ch09 §9.4）。THINK 不预设 layer。USE 的 `plan.layer=L3` 仅表示沉淀时 draft 目标层为 L3。

**DIGEST 两阶段流程展开**（L1→L2）：

```
主 agent 阶段（起子 agent 之前）：
  0. L1 捕获：原始材料（PDF/epub/整本 markdown）→ 按章切分
     落盘到 sources/<领域>/<编号>-<书名>/<ch>.md
     已切分的材料跳过
  0'. 价值判断（§4.4）：检视 L1，决定 layer（L1 / L2_light / L2）
  0''. 写两份 plan.json：scout plan + 每章 deep plan

第一次子 agent（DIGEST-Scout，1 个）：
  1. 分次 read-source 所有章节（整本书读完，建立整本书认知）
  2. 整本书读完后，回头建立每章主题卡（type=主题，layer=L2）
     命名：<ns>:<章节号>（如 he:01、he:12）
     内容：该章整体论点 + 结构骨架 + 在整本书的位置（跨章语境）
     已存在的主题卡不覆盖
  3. write-draft N 张主题卡（write-draft 前置拦截：phase=scout 只允许 type=主题）
  4. 调 loom mark-ready → hook 跑计算层校验
  5. 计算层通过 → 读 .semantic_sample.json 做语义自检
  6. 语义通过 → 调 loom commit-ready $SCOUT_ID --semantic-passed → 整批入库

主 agent 中转检查：scout task_trace status=done + N 张主题卡已入库

第二次子 agent（DIGEST-Deep，每章一个，并行 N 个）：
  1. read-source <该章>.md（该章 L1 全文）
  2. read-cards <该章主题卡 id> --task-id $DEEP_ID（Scout 已 commit）
     → 此时状态 = "已读过一遍"（持有 L1 全文 + 主题卡全局视野）
  3. 精读细节，产出该章其余 L2 卡（概念/结构/机制/案例/判断/反思）
     write-draft 前置拦截：phase=deep 不允许 type=主题
     每张 L2 卡 link 该章主题卡
     本材料内为了表达材料内部结构可以 link 其他 L2 / L1 source card（同 domain:book 前缀）
     反思卡额外锚定判断/模式卡
     **不主动做跨材料/跨领域/网络化 link**——跨材料/跨域关联是 L3 THINK 阶段的事
     **不 link L4**（计算层 l2_no_cross_domain 强制，§5.3）
     理由：DIGEST 专注消化材料（L1→L2），本材料内 link 表达内部结构；
           跨材料/跨域网络化是 THINK 阶段主动建的。两阶段分工清晰。
  4. （可选副产物）发现新模式涌现 → 记录 L4 候选（不需要读已有 L4），不在 L2 drafts 未入库前正式 propose-l4
  5. 调 loom mark-ready → hook 跑计算层校验
  6. 计算层通过 → 读 .semantic_sample.json 做语义自检
  7. 语义通过 → 调 loom commit-ready $DEEP_ID --semantic-passed → 整批入库
  8. 若候选确实跨领域且相关 L2/L3 已入库，再由主 agent / THINK agent 调 propose-l4 写 staging

**DIGEST 完全 L4-blind**：子 agent 不读 L4 索引、不 read-cards L4 卡、不 link L4。
专注消化材料，不被元层干扰。L4 连接是后续 THINK 阶段的事。
```

**两阶段的本质**：把"通读 + 精读产出"切成两次独立子 agent。Scout 通读整本书但产出少（每章 1 张主题卡）；Deep 每章独立 context 专注精读产出多张 L2 卡，N 章并行互不污染。

L3L4 不在 DIGEST 时强制建（除非材料本身触发明显的生成/模式）。L4 涌现走 §9.2 的 Skill 闭环。

### 9.2 L4 演化走 Skill 闭环

L4 卡的新增/升级不自动入库，走 ch13 §13.5 的"观察-提案-审核-验证"循环：

```
工作 agent 思考时发现新模式
  ↓
提案（写到 staging 区，status="pending"，不直接入库）
  ↓
主 agent 收到提案 → 向用户汇报（核心命题/跨域真实性/边界/related_cards）
  ↓
人类审核（L4 是认知架构，决定权在人）
  ↓
批准 → 主 agent 将 proposal JSON 的 status 改为 "approved"
  ↓
主 agent 调 `loom-admin commit-l4 <proposal.json>` 入库（CLI 校验 status=approved）
  （不复跑机器校验，人工审核是最终决策点）
```

**L4 模式成熟度升级**（[探索期]→[熟练期]）也是人工决定。harness 基于正向信号（use_count 高 + 被反思修正次数多 + 跨域验证）提醒"这张卡可能该升级"，但不自动升级。

### 9.3 派生物按需重建

`data/l4_index.md` 和 `data/orient.md` 都是派生缓存，不是事实源。commit 新 L4、更新已有 L4、删除 L4 等写入路径只更新数据库和卡片镜像，不主动重建派生物；下次 `orient` 或 `read-l4-index` 读取时，按 L4 卡 `updated_at` 与派生物 mtime 判断是否过期，过期才全量重建。

手动入口 `loom-admin rebuild-l4-index` 只用于调试或批量维护；正常思考链路走 pull 模式。

### 9.4 主 agent 起 sub agent 的合约

**子 agent 必须主动加载 skill 文件**——不依赖主 agent prompt 复制流程。

**为什么**：skill 是 SSOT；主 agent 复制流程会漂移（skill 改了 prompt 没同步 → 子 agent 按旧流程跑）。

**主 agent prompt 模板**（4 步全部写明，子 agent 必须按序执行）：
1. 第一步：加载流程（必做）—— Read `skills/_loom_core.md` + 对应模式 skill（如 `skills/loom-digest/SKILL.md`）
2. 第二步：任务上下文 —— task_id / 工作目录 / 具体任务（L1 路径、章节范围、目标 layer）
3. 第三步：按 skill 流程执行（不复制流程内容到 prompt，让 skill 是 SSOT）
4. 第四步：收尾协议（有 drafts 才执行）——
   - 调 `loom mark-ready <task_id>` 写 .ready 文件
   - SubagentStop/Stop hook 扫到 .ready → 跑计算层校验 → 写 .computed_passed.json + .semantic_sample.json → block 回 agent
   - agent 读 .semantic_sample.json 做语义自检（type_match / single_unit / genuine_digest / self_contained）
   - 通过后调 `loom commit-ready <task_id> --semantic-passed` 整批入库，然后自然退出

**禁止**：主 agent prompt 复制 skill 流程细节。

## 十、项目激活作用域

**实际架构**：默认完整安装全局 hook 配置（Claude Code: `~/.claude/settings.json`；Codex: `$CODEX_HOME/hooks.json`），靠 `loom-hook` 内部的 `hook-guard` + active project list 实现"只在激活项目里跑"。`install.sh --no-hooks` 提供 CLI/skills-only 安装；`install.sh --project` 安装项目级配置（`<根>/.claude/settings.json` 和 `<根>/.codex/hooks.json`），适合只想让当前 checkout 生效的场景。

机制：
- `loom on` 把当前项目加入 `~/.loom/active/projects`
- SubagentStop / Stop hook 触发时运行 `loom-hook`
- `loom-hook` 内部先跑 `loom hook-guard`
- 不在 active list → 静默退出
- 在 active list → 跑 `loom-admin stop-check-pending`，需要回炉时输出 `decision:block`

**默认全局安装的价值**：全局安装 + hook-guard 比"每个项目复制一份 settings.json"更灵活——一个项目 `loom on` 即激活，`loom off` 即关闭，不用改文件。代价是配置不在项目 git 里（但 `config/claude-settings.json.example` 在 git 里作为参考模板，install.sh 基于它合并到全局）。公开用户如果不想改全局 agent 配置，可以运行 `install.sh --no-hooks`，或者用 `install.sh --project` 把 hook 限定在当前 checkout。

## 十一、整个链路一次完整跑通

以"消化《与天为敌》第 3 章"为例：

```
1. 用户："消化第 3 章"

2. 主 agent（在 Claude Code 或 Codex 会话里）：
   - 按 DIGEST.md 流程：
     - 判断 L1 切分（第 3 章是一个单元）
     - 写 plan.json 到 /tmp/loom_task/abc123/
       {task_id, task, source, layer:"L2", skill:"DIGEST"}

3. 主 agent Bash 起子 agent（带超时）：
   timeout 600 claude --print "..." > /tmp/loom_task/abc123/result.json 2>&1

4. 子 agent（独立 Claude 进程，工具集被裁剪）：
   - 子 agent 主动加载 skill 文件（DIGEST.md）
   - DIGEST 流程不读 L4（完全 L4-blind，§9.1）
   - read-source 读 L1
   - 识别认知单元 → write-draft 写到 drafts/
     每次 write-draft 触发内联计算校验，失败当场拒
   - 自然停止 → 进程退出

5. 收尾触发：
   - Claude Code / Codex 自动触发 SubagentStop 或 Stop hook（`loom-hook`）

   随后：
   - 计算层：扫 drafts 跑 14 条单卡计算校验 + 4 条整批校验
     失败 → decision:block + 错误清单 → agent 修 draft
   - 计算层通过 → 写 .computed_passed.json + .semantic_sample.json
     然后要求 agent 做语义自检
   - agent 读 .semantic_sample.json，按 §七 四判据自判
     失败 → 修 draft，重新 mark-ready（重跑计算层 + 重新抽样）
   - 通过 → 子 agent 调 `loom commit-ready <task_id> --semantic-passed`
     commit-ready 验 mtime 未变 → 整批入库（单事务）→ 标 task done
     L4 派生索引不在写入时重建，下次 `orient` / `read-l4-index` 按需刷新

6. 主 agent 检测 task_trace.status：
   done → 任务完成，drafts 已入库
   computed_passed 但未 done → agent 在做语义自检；Codex 下主 agent 应主动推进
   failed → 看错误报告决定放弃 / 改 plan / 升级人类
   超时（外层 timeout exit code=124）→ 走 salvage：loom-admin stop-check <task_id> --mode=salvage

7. 回炉上限：若运行时有连续 block 上限，主 agent 收到最终错误报告后决定放弃/改 plan/升级人类

8. 全程 trace 记录 → use_count/search_count 更新
```

## 十二、与 001 的关系

001 的"三层架构"（L1 Skill / L2 Tool / L3 Orchestration）是粗糙映射——它把 Context 和 Plan 漏了，把 Tool 和 Hook 混在一起。

005 用 Harness Engineering 的五构件语言重新组织：

| 001 的层 | 005 的对应 |
|---|---|
| L1 Skill（原则性，靠自觉） | Prompt 层（AGENTS.md + skill 文件） |
| L2 Tool（带 guardrails） | Tool 层（write-draft 内联计算校验）+ Plan 层 |
| L3 Orchestration（lint 审计） | Hook/收尾层（stop-check 双层校验）+ 可观测性 |

001 的核心洞察保留并精确化：
- **规则下沉到工具层**——保留。005 把计算校验内联在 write-draft，不靠 prompt 自觉。
- **lint 事后审计**——保留。005 的 stop-check 语义层就是事后审计；Claude Code / Codex 都用原生 hook 触发，不再造独立 lint 脚本。
- **Agent-as-Judge**——保留。005 的语义层就是 spawn 独立子 agent 判，但判据来自 004/AGENTS.md 不是现场拍。

001 被取代的部分：
- 独立的 `lint_card.py` / `lint_chunk.py` 脚本——005 合并进 stop-check
- 三层架构图——005 用五构件 + 支撑层替代

**注意**：ID 由 agent 在命令行显式指定（`<card_id>` 位置参数，write-draft 和 propose-l4 都一样），不自动分配——这要求 skill 教 agent 按卢曼 ID 规范自主命名。L4 ID `gen:<卢曼ID>` 也是卢曼树形（`gen:1a` 顶级、`gen:1a1` 深化），agent 提案时按树形关系拍 ID。

001 文档作为历史记录保留（不删除），但实施以 005 为准。
