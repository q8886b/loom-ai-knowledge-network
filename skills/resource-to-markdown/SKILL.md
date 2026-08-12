---
name: resource-to-markdown
description: 把任意格式资源（PDF/EPUB/视频/音频/HTML/docx/txt）以最佳质量、最快速度转成按章节拆分的 markdown。当用户说"转 markdown"、"把这本书转成 md"、"转录这个视频"、"处理这个 PDF"时触发。带三层 harness 兜底保证成功率。
---

# resource-to-markdown Skill

## 何时触发

- 用户给一个任意格式文件，要求转成 markdown
- 用户要"消化一本书/PDF/视频"且没有现成 markdown
- Loom 的 DIGEST 流程第 0 步需要前置转换时
- 批量处理 sources/<领域>/ 下的资源

## 核心承诺

- **任意格式**：PDF（文本型 + 扫描型）、EPUB、视频/音频、HTML、docx/pptx、markdown/txt
- **最佳质量**：按格式选最佳引擎（pymupdf/docling/rapidocr/pandoc/markitdown/faster-whisper）
- **最快速度**：文本型 PDF < 10s/100 页；扫描型 PDF OCR ~1s/页；视频转写 ~30s/分钟
- **最优成本**：默认全本地，零 API 费用；模型用本地缓存
- **成功率保证**：三层兜底 + harness 校验，失败时清晰报告"罕见情况"

## 用法

```bash
# 基础用法
python3.11 skills/resource-to-markdown/scripts/convert.py <input> --out-dir=<dir>

# 指定音视频语言
python3.11 skills/resource-to-markdown/scripts/convert.py video.mp4 --out-dir=out --language=zh

# 不切章节（只产出 raw.md）
python3.11 skills/resource-to-markdown/scripts/convert.py file.pdf --out-dir=out --no-split

# 自定义章节大小
python3.11 skills/resource-to-markdown/scripts/convert.py book.epub --out-dir=out --min=1000 --max=30000
```

## 输出

每个文件产出 3 类输出到 `<out-dir>/`：

```
<stem>.raw.md           # 原始转换（完整 markdown）
<stem>.quality.json     # 质量报告（引擎/字数/校验结果/警告）
ch01.md, ch02.md, ...   # 按章节切分（默认切，--no-split 跳过）
```

`quality.json` 关键字段：

```json
{
  "status": "ok|ok_with_warnings|quality_failed|convert_failed",
  "convert": {"engine": "...", "chars": ..., "strategy": "..."},
  "raw_summary": {"passed": ..., "failures": [...]},
  "unit_summary": {"passed": ..., "failures": [...]},
  "warnings": [...],
  "duration_sec": ...
}
```

## 路由策略（按扩展名 + 内容）

| 格式 | 主引擎 | 兜底 |
|---|---|---|
| `.md`/`.txt` | 直接复制（去 BOM、统一换行） | - |
| `.pdf`（文本型，>50 字/页） | pymupdf sort=True | Docling → MarkItDown |
| `.pdf`（扫描型，<50 字/页） | RapidOCR 直接（pdftoppm + GPU） | Docling OCR → tesseract → MarkItDown |
| `.epub` | markitdown | pandoc |
| `.html`/`.htm` | pandoc | markitdown |
| `.docx`/`.pptx`/`.xlsx` | markitdown | pandoc（docx） |
| `.mp4`/`.webm`/`.mov` 等 | ffmpeg + faster-whisper | whisper CLI |
| `.mp3`/`.m4a`/`.wav` 等 | faster-whisper | whisper CLI |

## Harness：三层校验 + 三层兜底

### 三层校验（输出前）

**raw.md 层**：
- `exists`：文件存在且非空
- `min_chars`：字数 ≥ 预期（PDF 按页数 × 200 字估）
- `garbage_ratio`：连续 > 30 字乱码字符占比 < 5%
- `has_heading`：有 markdown 标题或中文章节模式（或内容足够长且无乱码——纯文本提取放宽）

**章节切分层**：
- `units_exist`：至少切出 1 个单元
- `unit_min`：每 chXX.md ≥ 500 字（默认）
- `unit_max`：每 chXX.md ≤ 50000 字（默认）
- `coverage`：Σ chXX.md 字数 / raw.md 字数 ≥ 60%

### 三层兜底（每格式）

每类格式至少 2 个引擎，主失败自动回退：
- PDF：pymupdf → Docling → MarkItDown（文本型）；RapidOCR → Docling OCR → tesseract → MarkItDown（扫描型）
- EPUB：markitdown → pandoc
- HTML：pandoc → markitdown
- 音视频：faster-whisper → whisper CLI

## 章节切分策略（split_chapters.py）

按优先级：
1. **H1 边界**（`# 标题`）
2. **H2 边界**（H1 单元太长时降级）
3. **中文章节模式**（`第X章`/`第X部分`/`Chapter N`）
4. **字数硬切**（无结构信号时，按句号优先 + 段落边界）

最后：
- 超长单元（> max_chars）按 H2 再切，仍超长按句号硬切
- 太短单元（< min_chars）合并到下一个

## 状态码与处理建议

| status | 含义 | 主 agent 应对 |
|---|---|---|
| `ok` | 全部校验过 | 直接用 chXX.md 进入下游 |
| `ok_with_warnings` | raw 过但切分有小瑕疵 | 看 warnings 决定是否手工补 |
| `quality_failed` | raw 或切分严重失败 | 看具体失败项，可能换输入或放弃 |
| `convert_failed` | 所有引擎都失败 | 标记为罕见情况，记录到 `_failures.md` |

## 限制与边界

1. **SPA 动态网页**（如飞书、Notion）：HTML 里只有空 div 和 JS bundle，pandoc 转不出正文。需要 headless browser 渲染，本 skill 不支持，标记 unsupported。
2. **大书扫描 PDF**：GPU ~1.2s/页（598 页约 12 分钟），CPU ~4s/页。建议优先用 GPU 机器。
3. **复杂公式 PDF**：Docling/pymupdf 都不识别 LaTeX，公式会乱。需要 MinerU（暂未集成）。
4. **多栏英文 PDF**：pymupdf 默认按 Y 坐标排序会交错左右栏，输出排版乱但信息完整。后续 LLM 清洗可修复。
5. **whisper large-v3 模型**：~3GB，需要从 HuggingFace 下载。环境变量 `HF_ENDPOINT=https://hf-mirror.com` 走中国镜像。或用 base 模型（本地缓存）。
6. **远程 OCR 是显式选择**：默认不会探测远端主机或上传材料。只有用户明确设置 `LOOM_OCR_REMOTE_HOST=<ssh-host>` 时，扫描型 PDF 才可能通过 SSH/SCP 发往该主机；运行时会在 stderr 显示外发提示，并在成功或失败后清理远端临时目录。

## 与 Loom 集成

本 skill 是 Loom DIGEST 流程的**第 0 步**前置工具：

```
用户给资源 → resource-to-markdown → chXX.md → DIGEST-Scout 建主题卡 → DIGEST-Deep 产 L2 卡 → THINK 跨材料
```

PIPELINE.md（待写）会编排端到端流程。本 skill 单独可用。

## 文件结构

```
skills/resource-to-markdown/
├── SKILL.md                       # 本文档
├── scripts/
│   ├── convert.py                 # 主路由
│   ├── harness.py                 # 质量校验
│   ├── split_chapters.py          # 章节切分
│   └── handlers/
│       ├── pdf_handler.py         # PDF（pymupdf/Docling/tesseract/MarkItDown）
│       ├── epub_handler.py        # EPUB（markitdown/pandoc）
│       ├── html_handler.py        # HTML（pandoc/markitdown）
│       ├── office_handler.py      # docx/pptx（markitdown/pandoc）
│       ├── av_handler.py          # 音视频（ffmpeg + faster-whisper/whisper CLI）
│       └── text_handler.py        # md/txt（直接复制）
└── README.md                      # 详细说明
```

## 调试与扩展

### 单独测试某个 handler

```bash
python3.11 skills/resource-to-markdown/scripts/handlers/pdf_handler.py input.pdf out.md
```

### 添加新格式

1. 在 `handlers/` 加 `<format>_handler.py`，实现 `convert(input, out_md) -> dict`
2. 在 `convert.py` 的 `EXT_MAP` 注册扩展名
3. 在 `estimate_min_chars` 加字数估算
4. 测试 ≥ 2 个真实样本
