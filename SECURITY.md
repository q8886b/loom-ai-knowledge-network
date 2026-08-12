# Security Policy

Loom is a local-first tool. It is designed to run on your own machine and to
store private card data under `~/.loom` or `LOOM_HOME`.

## Supported Versions

The project is pre-1.0. Security fixes target the `main` branch until release
branches exist.

## Reporting a Vulnerability

Please open a private security advisory on GitHub if the repository is hosted
there, or contact the maintainers through the repository issue tracker asking
for a private disclosure channel. Do not publish exploit details before a fix
or mitigation is available.

## Local Data Boundary

Do not publish:

- `~/.loom`, `.loom-local`, `data`, `cards`, or `sources`
- SQLite databases, WAL/SHM files, source documents, card exports, screenshots,
  or generated workbench bundles that include private content
- `.env` files or API keys
- Claude Code / Codex local settings that include private paths or commands

## Workbench Exposure

The Workbench backend is intended for localhost use. It exposes read endpoints
for card content, binds to `127.0.0.1`, and does not enable cross-origin browser
access. Do not add a public bind or permissive CORS policy without authentication.

## External Processing

The default Ollama embedding path is local. OpenAI-compatible and Zhipu
embedding providers send up to the configured input limit of card or source
text to the selected endpoint. Remote PDF OCR is disabled by default; setting
`LOOM_OCR_REMOTE_HOST` explicitly authorizes scanned PDFs to be sent to that SSH
host. Review both settings before processing confidential or licensed material.

Loom creates its managed home and data directories with owner-only permissions
and stores databases, card mirrors, and derived indexes as owner-readable files.

## Hook Safety

Agent hooks can run commands at stop events. Loom installs its stop-check hooks
by default because they are part of the full agent workflow, but they are
guarded by `loom on` and stay silent outside activated projects. Use
`./install.sh --no-hooks` for CLI-only installs, and review generated hook
settings before enabling Loom in sensitive environments.
