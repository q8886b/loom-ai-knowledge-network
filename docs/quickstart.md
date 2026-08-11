# Quick Start

This guide gets a fresh Loom checkout to a working local setup.

## 1. Install

Loom targets Python 3.11.

```bash
git clone git@github.com:q8886b/loom-ai-knowledge-network.git
cd loom-ai-knowledge-network
./install.sh
```

The default install creates `~/.loom`, installs CLI wrappers and skills, and
installs Claude Code / Codex stop-check hooks. Hooks stay quiet until you
activate Loom inside a project:

```bash
loom on
loom off
```

For package development:

```bash
python3.11 -m pip install -e ".[dev]"
```

## 2. Configure Embeddings

Vector search is part of Loom's core workflow. Configure one provider in
`~/.loom/.env`.

Recommended local setup with Ollama:

```bash
ollama pull bge-m3
cp .env.example ~/.loom/.env
```

Then set:

```bash
LOOM_EMBED_PROVIDER=ollama
LOOM_EMBED_BASE_URL=http://127.0.0.1:11434
LOOM_EMBED_MODEL=bge-m3
LOOM_EMBED_DIM=1024
```

OpenAI-compatible API:

```bash
LOOM_EMBED_PROVIDER=openai
LOOM_EMBED_BASE_URL=https://api.openai.com/v1
LOOM_EMBED_API_KEY=your-api-key
LOOM_EMBED_MODEL=text-embedding-3-small
LOOM_EMBED_DIM=1536
```

Zhipu compatibility preset:

```bash
LOOM_EMBED_PROVIDER=zhipu
ZHIPU_API_KEY=your-zhipu-api-key
LOOM_EMBED_MODEL=embedding-3
LOOM_EMBED_DIM=2048
```

One Loom database uses one embedding dimension. If you change provider, model,
or dimension after cards already exist, rebuild the vector index:

```bash
loom-admin rebuild-embeddings
```

This does not delete cards or links. It only rebuilds `cards_vec`.

## 3. Import A Source

Create a source Markdown file:

```bash
mkdir -p ~/.loom/sources/07-LLM/demo
cat > ~/.loom/sources/07-LLM/demo/ch01.md <<'EOF'
# Harness Notes

A harness is the surrounding system that turns a model into a reliable agent.
EOF
```

Register it as an L1 source card:

```bash
loom import-source llm:demo:src:01 \
  --title="Harness Notes - Chapter 1" \
  --path="$HOME/.loom/sources/07-LLM/demo/ch01.md"
```

Read and search:

```bash
loom read-source llm:demo:src:01
loom search "reliable agent" --mode=hybrid
loom search "reliable agent" --mode=fts
loom orient
```

If embeddings are not configured or the local model is not running, `hybrid`
search falls back to FTS with a warning.

## 4. Use Agent Skills

Loom's higher-level workflows live in `skills/`:

```text
loom-digest     L1 source material -> L2 cards
loom-think      cross-material synthesis -> L3/L4 cards
loom-use        answer concrete questions with the card network
loom-pipeline   larger end-to-end runs
```

Agents write drafts first. The commit path is:

```text
loom write-draft
loom mark-ready
loom-admin stop-check
loom commit-ready --semantic-passed
```

## 5. Optional Workbench

The Workbench is a local graph browser.

Backend:

```bash
python3.11 -m pip install -e ".[workbench]"
python3.11 workbench/backend/main.py
```

Frontend:

```bash
cd workbench/frontend
npm install
npm run dev
```

Open <http://127.0.0.1:8888>.

Do not expose the Workbench API to a public interface without authentication;
it serves card content.
