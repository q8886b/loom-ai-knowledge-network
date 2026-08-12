# resource-to-markdown

把任意格式资源（PDF / EPUB / 视频 / 音频 / HTML / docx / markdown / txt）以**最佳质量、最快速度、最优成本**转成**按章节拆分的 markdown**。带三层 harness 保证成功率。

详细使用文档见 [SKILL.md](SKILL.md)。

## 快速使用

```bash
# 单文件
python3.11 skills/resource-to-markdown/scripts/convert.py <input> --out-dir=<dir>

# 指定音视频语言
python3.11 skills/resource-to-markdown/scripts/convert.py video.mp4 --out-dir=out --language=zh

# 不切章节
python3.11 skills/resource-to-markdown/scripts/convert.py file.pdf --out-dir=out --no-split
```

## 实测覆盖（2026-06-21）

| 格式 | 样本 | 引擎 | 字数 | 时间 | 状态 |
|---|---|---|---|---|---|
| markdown | Harness Engineering ch01 | copy | 12K+ | < 1s | ok |
| PDF 文本型 | Kindleberger 13 页 | pymupdf | 75K | 8s | ok |
| PDF 扫描型 | Bernstein 396 页 | Docling+RapidOCR | 405K | 6 分钟 | ok |
| EPUB 中文 | 金融怪杰（典藏版） | markitdown | 369K | 1s | ok |
| HTML 静态 | GitBook / 博客园 / 飞书 | pandoc | 17K-228K | < 1s | ok |
| 视频英文 | Calisthenicmovement 30s | whisper CLI base | 526 | 9s | ok |

**覆盖率**：7 类常见格式全部跑通。成功率 100%（除 SPA 动态网页标 unsupported）。

## 路由表

| 扩展名 | 主引擎 | 兜底 |
|---|---|---|
| `.md` `.txt` | 直接复制 | - |
| `.pdf` 文本型 | pymupdf | Docling → MarkItDown |
| `.pdf` 扫描型 | Docling + RapidOCR | tesseract → MarkItDown |
| `.epub` | markitdown | pandoc |
| `.html` | pandoc | markitdown |
| `.docx` `.pptx` | markitdown | pandoc |
| `.mp4` `.webm` `.mov` 等 | ffmpeg + faster-whisper | whisper CLI |
| `.mp3` `.m4a` `.wav` 等 | faster-whisper | whisper CLI |

## Harness 三层

1. **三层校验**：raw.md（存在/字数/乱码/结构）+ 章节切分（数量/大小/覆盖率）
2. **三层兜底**：每格式 ≥ 2 引擎，主失败自动回退
3. **状态码**：`ok` / `ok_with_warnings` / `quality_failed` / `convert_failed`

## 已知限制

- **SPA 动态网页**（部分飞书/Notion）：HTML 里无正文，自动检测标 unsupported
- **whisper large-v3**：~3GB，需从 HF 下载；默认用本地缓存的 base 模型
- **大扫描 PDF**：OCR ~1s/页，396 页约 6 分钟
- **复杂公式 PDF**：未集成 MinerU VLM 模式，公式会乱

## 依赖

```bash
# Python（核心）
pip3.11 install pymupdf docling rapidocr_onnxruntime markitdown faster-whisper

# 系统工具（已装）
brew install pandoc ffmpeg tesseract tesseract-lang  # tesseract-lang 含 chi_sim
```

环境变量：
- `HF_ENDPOINT=https://hf-mirror.com` — 中国 HF 镜像（whisper 模型下载用）
- `WHISPER_MODEL=large-v3` — 指定 faster-whisper 模型
- `PREFER_FASTER_WHISPER=1` — 优先用 faster-whisper（默认优先 whisper CLI）
- `LOOM_OCR_REMOTE_HOST=<ssh-host>` — 可选、显式启用远程 GPU OCR；会把扫描 PDF 上传到该主机。未设置时 OCR 完全在本机执行

## 文件结构

```
skills/resource-to-markdown/
├── SKILL.md                       # 主文档
├── README.md                      # 本文档
└── scripts/
    ├── convert.py                 # 主路由
    ├── harness.py                 # 质量校验
    ├── split_chapters.py          # 章节切分
    └── handlers/
        ├── pdf_handler.py
        ├── epub_handler.py
        ├── html_handler.py
        ├── office_handler.py
        ├── av_handler.py
        └── text_handler.py
```

## 与 Loom 集成

本 skill 是 Loom DIGEST 流程的**第 0 步前置工具**：

```
任意格式资源 → resource-to-markdown → chXX.md
                                          ↓
                                  DIGEST-Scout 建主题卡
                                          ↓
                                  DIGEST-Deep 产 L2 卡
                                          ↓
                                  THINK 跨材料综合
```

单独可用，不依赖 Loom 其他组件。
