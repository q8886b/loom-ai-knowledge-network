# Loom：以知识网络为形态的认知 Harness

**语言：** [English](README.md) | 简体中文

Loom 让 Claude Code 和 Codex 能够持久、可检查地把材料转化为理解。书籍、
论文、网页、视频和音频会经过四个认知层级：保留的证据、单材料消化、
跨材料综合，以及可复用的思考模式。

**Loom 的核心闭环：** Agent 在使用前先消化材料，通过有认知类型、显式相连
的卡片推理；新洞察只有经过计算、语义检查和审核才能回流。最终得到的是一张
本地优先的 AI 知识网络，它不仅保存内容，也保留每个想法是如何形成的。

Loom 可以用于研究、Zettelkasten 式个人知识管理和 AI 第二大脑工作流。但它的
核心产物不是 Wiki 页面、向量索引、聊天记录或自动抽取的实体图谱，而是一套
构建和复用理解的可审核生命周期。

![Loom Workbench 展示跨领域聚焦知识网络](docs/assets/loom-workbench.png)

## Loom 如何工作

```text
原始材料
    ↓
L1 · 原文
    ↓  Scout 通读全书；Deep 带着全局视野再次精读每个单元
L2 · 单材料理解
    ↓  THINK 跨材料连接证据
L3 · 综合、判断与新产出
    ↓  跨领域模式经过提案与人工审核
L4 · 可复用的思考方式
    ↓
USE 把网络带回 Agent context
    ↺ 新洞察经过审核后可以回流网络
```

四层表达的是认知深度，不是存储层级：

| 层 | 内容 | 目的 |
|---|---|---|
| **L1** | 保留的原文 | 让综合结果可追溯、可逆 |
| **L2** | 对一份材料的真消化 | 把材料转成原子化、自足可读的认知单元 |
| **L3** | 跨材料综合 | 形成比较、判断、反例与新想法 |
| **L4** | 跨领域模式 | 为 Agent 提供带适用边界的可复用思考框架 |

## 核心差异

- **真消化，不是摘录。** Scout 先建立整份材料的全局地图，Deep 再带着地图
  精读每个单元。
- **认知类型明确。** 卡片标明自己的角色：概念、结构、机制、案例、判断、
  反思、模式或主题。
- **Link 是真相。** 显式 link 承载经过确认的关联；embedding 只负责发现候选。
- **入库有门禁。** Draft 必须先通过确定性计算校验和语义校验，才能进入 SQLite。
- **使用后有回流。** 提问、决策和复盘可以形成待审核提案，而不是静默改写网络。
- **本地优先、可检查。** 卡片、link、全文索引、向量和任务 trace 都保存在本地；
  embedding 可选本地模型或 OpenAI-compatible API。

## Loom 不等于

下列品类与 Loom 相邻，也可以和 Loom 组合，但它们的首要问题不同。

| 品类 | 核心产物 | 通常何时综合 | Loom 的区别 |
|---|---|---|---|
| **AI Wiki** | 持续维护的页面 | 摄入和页面维护时 | 原子卡片有明确 type，并沿 L1 → L4 形成认知深度 |
| **RAG 知识库** | 原文 chunk 与检索索引 | 查询时 | 使用前先完成消化，并用经过确认的显式 link 连接 |
| **Agent Memory** | 对话、事实、偏好、经历 | 会话中或会话后 | 从选定材料和审核后的思考中形成持久认知 |
| **知识图谱** | 实体与关系 | 信息抽取时 | 图中同时表达认知角色、生成层级和反馈门禁 |

## 快速开始

需要 macOS 或 Linux、Python 3.11+、Claude Code 或 Codex，以及一个
embedding 模型。

```bash
git clone https://github.com/q8886b/loom-ai-knowledge-network.git
cd loom-ai-knowledge-network
./install.sh
loom on
```

完全本地使用时，先安装 [Ollama](https://ollama.com)，再配置
`~/.loom/.env`：

```bash
ollama pull bge-m3
```

```dotenv
LOOM_EMBED_PROVIDER=ollama
LOOM_EMBED_MODEL=bge-m3
LOOM_EMBED_DIM=1024
```

把一份 Markdown 材料放进 `~/.loom/sources/`，然后直接对 Agent 说：

> 用 `loom-digest` 把这份材料消化到 L2：`/材料的绝对路径/source.md`

Agent 会读原文、写 draft、执行计算与语义门禁，并且只在两层校验都通过后
入库。随后可以这样探索：

```bash
loom search "你的主题"
loom tui
```

embedding provider、hook、Web Workbench 和项目级安装方式见
[快速开始文档](docs/quickstart.md)。

## 内置 Agent Skills

`./install.sh` 会把完整 skill 链接到 Claude Code 和 Codex；仓库内的文件始终是
唯一事实源。

| Skill | 用途 |
|---|---|
| `loom-digest` | 用两阶段阅读把 L1 原文消化为 L2 |
| `loom-think` | 跨材料研究、综合与反思 |
| `loom-use` | 通过已有网络回答问题、辅助决策 |
| `loom-pipeline` | 编排多资源摄入、消化与综合任务 |
| `resource-to-markdown` | 把 PDF、EPUB、网页、Office、音频和视频转成 Markdown |

## 存储与检索

- SQLite + FTS5：卡片与全文检索
- `sqlite-vec`：可选语义召回
- 显式双向 link：持久知识图谱
- Markdown 镜像：保存原文与可读卡片产物
- TUI 与本地 Web Workbench：浏览、搜索、批注与图谱探索

Workbench 是本地工具，会在没有鉴权的情况下提供卡片内容。不要直接暴露到
公网。

## 设计基准

- [004 — 分层重设计：目的与思想](docs/design/004-layered-redesign-purpose.md)
- [005 — 分层重设计的 Harness 落地](docs/design/005-layered-redesign-harness.md)

## 当前状态

Loom 仍处于 Alpha。数据模型和受控入库链路已经实现，但界面与安装细节在 1.0
之前仍可能变化。

## 开发

```bash
python3.11 -m pip install -e ".[dev]"
python3.11 -m pytest
```

参与贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)、
[安全策略](SECURITY.md) 和 [行为准则](CODE_OF_CONDUCT.md)。

## 许可证

[MIT](LICENSE)
