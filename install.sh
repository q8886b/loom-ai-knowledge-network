#!/usr/bin/env bash
# install.sh — install Loom globally (or project-locally)
#
# Usage:
#   ./install.sh               # Claude + Codex skills/hooks + ~/.loom setup
#   ./install.sh --no-hooks    # install CLI/skills only; skip agent hooks
#   ./install.sh --project     # project-local Claude + Codex hooks for this checkout
#   ./install.sh --codex-only  # Codex skills/hooks + ~/.loom, no Claude settings
#   ./install.sh --claude-only # Claude skills/hooks + ~/.loom
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$(realpath "$0")")" && pwd)"
LOOM_HOME="${LOOM_HOME:-$HOME/.loom}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
INSTALL_HOOKS=1
PROJECT_LOCAL=0
INSTALL_CLAUDE_SKILLS=1
INSTALL_CODEX_SKILLS=1
INSTALL_CLAUDE_HOOKS=1
INSTALL_CODEX_HOOKS=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-hooks) INSTALL_HOOKS=1 ;;
    --no-hooks) INSTALL_HOOKS=0 ; INSTALL_CLAUDE_HOOKS=0 ; INSTALL_CODEX_HOOKS=0 ;;
    --project) PROJECT_LOCAL=1 ; INSTALL_HOOKS=1 ;;
    --codex-only) INSTALL_CLAUDE_SKILLS=0 ; INSTALL_CODEX_SKILLS=1 ;;
    --claude-only) INSTALL_CLAUDE_SKILLS=1 ; INSTALL_CODEX_SKILLS=0 ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

if [[ "$INSTALL_HOOKS" == 0 ]]; then
  INSTALL_CLAUDE_HOOKS=0
  INSTALL_CODEX_HOOKS=0
else
  [[ "$INSTALL_CLAUDE_SKILLS" == 0 ]] && INSTALL_CLAUDE_HOOKS=0
  [[ "$INSTALL_CODEX_SKILLS" == 0 ]] && INSTALL_CODEX_HOOKS=0
fi

find_python311() {
  local candidate
  for candidate in \
    /opt/homebrew/bin/python3.11 \
    /usr/local/bin/python3.11 \
    "$(command -v python3.11 2>/dev/null || true)" \
    "$(command -v python3 2>/dev/null || true)" \
    "$(command -v python 2>/dev/null || true)"
  do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON_BIN="$(find_python311)"; then
  echo "ERROR: Python 3.11+ not found. Install Python 3.11 or add it to PATH." >&2
  exit 127
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ensure_dir() {
  mkdir -p "$1"
}

backup_if_exists() {
  local f="$1"
  if [[ -e "$f" && ! -e "${f}.bak" ]]; then
    cp -a "$f" "${f}.bak"
    echo "Backed up $f -> ${f}.bak"
  fi
}

# ---------------------------------------------------------------------------
# 1. Ensure Python runtime dependencies
# ---------------------------------------------------------------------------

echo "==> Checking Python runtime dependencies"
if ! "$PYTHON_BIN" -c "import certifi, sqlite_vec" >/dev/null 2>&1; then
  if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    echo "ERROR: pip for $PYTHON_BIN not found. Install pip, then run:" >&2
    echo "  $PYTHON_BIN -m pip install -r $REPO_DIR/requirements.txt" >&2
    exit 127
  fi
  PIP_INSTALL_ARGS=()
  if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    PIP_INSTALL_ARGS=(--user)
  fi
  if ! "$PYTHON_BIN" -m pip install "${PIP_INSTALL_ARGS[@]}" -r "$REPO_DIR/requirements.txt"; then
    echo "ERROR: failed to install Python runtime dependencies." >&2
    echo "Try installing them manually, then rerun install.sh:" >&2
    echo "  $PYTHON_BIN -m pip install --user -r $REPO_DIR/requirements.txt" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 2. Ensure ~/.loom directory tree
# ---------------------------------------------------------------------------

echo "==> Setting up Loom home: $LOOM_HOME"
ensure_dir "$LOOM_HOME/data"
ensure_dir "$LOOM_HOME/sources"
ensure_dir "$LOOM_HOME/cards"
ensure_dir "$LOOM_HOME/active"
chmod 700 "$LOOM_HOME" "$LOOM_HOME/data" "$LOOM_HOME/sources" "$LOOM_HOME/cards" "$LOOM_HOME/active"

if [[ ! -f "$LOOM_HOME/.env" ]]; then
  if [[ -f "$REPO_DIR/.env.example" ]]; then
    cp "$REPO_DIR/.env.example" "$LOOM_HOME/.env"
    echo "Created $LOOM_HOME/.env from example. Edit it to choose your embedding provider."
  fi
fi
chmod 600 "$LOOM_HOME/.env"

# Migrate existing data if present in the repo checkout and target is empty
if [[ -f "$REPO_DIR/data/brain.db" && ! -f "$LOOM_HOME/data/brain.db" ]]; then
  echo "==> Migrating existing data from $REPO_DIR to $LOOM_HOME"
  cp -R "$REPO_DIR/data" "$LOOM_HOME/"
  [[ -d "$REPO_DIR/sources" ]] && cp -R "$REPO_DIR/sources" "$LOOM_HOME/"
  [[ -d "$REPO_DIR/cards" ]] && cp -R "$REPO_DIR/cards" "$LOOM_HOME/"
fi

# Loom data can contain private source material and card content. Repair older
# installs that inherited a permissive umask before continuing.
find "$LOOM_HOME" -type d -exec chmod 700 {} +
find "$LOOM_HOME" -type f -exec chmod 600 {} +

# ---------------------------------------------------------------------------
# 3. Install `loom` command into PATH
# ---------------------------------------------------------------------------

echo "==> Installing loom CLI"
BIN_DIR=""
PYTHON_BIN_DIR="$(dirname "$PYTHON_BIN")"
if [[ -d "$PYTHON_BIN_DIR" && -w "$PYTHON_BIN_DIR" ]]; then
  BIN_DIR="$PYTHON_BIN_DIR"
elif [[ -w "/usr/local/bin" ]]; then
  BIN_DIR="/usr/local/bin"
elif [[ -d "$HOME/.local/bin" ]] || mkdir -p "$HOME/.local/bin"; then
  BIN_DIR="$HOME/.local/bin"
  if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo ""
    echo "WARNING: $HOME/.local/bin is not in your PATH."
    echo "Add this to your shell profile to use the 'loom' command:"
    echo "  export PATH=\"$HOME/.local/bin:\$PATH\""
    echo ""
  fi
fi

if [[ -n "$BIN_DIR" ]]; then
  ln -sfn "$REPO_DIR/bin/loom" "$BIN_DIR/loom"
  ln -sfn "$REPO_DIR/bin/loom-admin" "$BIN_DIR/loom-admin"
  ln -sfn "$REPO_DIR/bin/loom-hook" "$BIN_DIR/loom-hook"
  ln -sfn "$REPO_DIR/bin/loom-codex-hook" "$BIN_DIR/loom-codex-hook"
  echo "Linked loom -> $BIN_DIR/loom"
  echo "Linked loom-admin -> $BIN_DIR/loom-admin"
  echo "Linked loom-hook -> $BIN_DIR/loom-hook"
  echo "Linked loom-codex-hook -> $BIN_DIR/loom-codex-hook"
else
  echo "ERROR: Could not install loom to PATH" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 4. Install global skill symlinks
# ---------------------------------------------------------------------------

install_skill_links() {
  local skills_dir="$1"
  local label="$2"
  ensure_dir "$skills_dir"
  ln -sfn "$REPO_DIR/skills/loom-digest" "$skills_dir/loom-digest"
  ln -sfn "$REPO_DIR/skills/loom-think" "$skills_dir/loom-think"
  ln -sfn "$REPO_DIR/skills/loom-use" "$skills_dir/loom-use"
  ln -sfn "$REPO_DIR/skills/loom-pipeline" "$skills_dir/loom-pipeline"
  ln -sfn "$REPO_DIR/skills/resource-to-markdown" "$skills_dir/resource-to-markdown"
  ln -sfn "$REPO_DIR/skills/_loom_core.md" "$skills_dir/_loom_core.md"
  echo "Linked $label skills -> $skills_dir"
}

if [[ "$INSTALL_CLAUDE_SKILLS" == 1 ]]; then
  echo "==> Installing Claude Loom skills"
  install_skill_links "$HOME/.claude/skills" "~/.claude"
fi

if [[ "$INSTALL_CODEX_SKILLS" == 1 ]]; then
  echo "==> Installing Codex Loom skills"
  install_skill_links "$CODEX_HOME/skills" "\$CODEX_HOME"
fi

# ---------------------------------------------------------------------------
# 5. Install hooks
# ---------------------------------------------------------------------------

install_global_hooks() {
  local settings="$HOME/.claude/settings.json"
  local example="$REPO_DIR/config/claude-settings.json.example"
  local hook_cmd="$REPO_DIR/bin/loom-hook"
  ensure_dir "$HOME/.claude"
  backup_if_exists "$settings"

  # Read existing settings or empty object
  local existing
  if [[ -f "$settings" ]]; then
    existing=$(cat "$settings")
  else
    existing="{}"
  fi

  # Merge: keep non-Loom hooks, append/replace Loom hooks
  "$PYTHON_BIN" - "$existing" "$example" "$settings" "$hook_cmd" <<'PY'
import json, sys
existing_json, example_path, out_path, hook_cmd = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    cfg = json.loads(existing_json)
except json.JSONDecodeError:
    cfg = {}
with open(example_path, encoding="utf-8") as f:
    loom = json.load(f)

for entries in loom.get("hooks", {}).values():
    for entry in entries:
        for h in entry.get("hooks", []):
            if h.get("type") == "command" and h.get("command") == "loom-hook":
                h["command"] = hook_cmd

def is_loom_entry(entry):
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if h.get("type") == "command" and (
            "loom-hook" in cmd
            or "loom hook-guard" in cmd
            or "loom-admin stop-check-pending" in cmd
            or "loom-codex-hook" in cmd
        ):
            return True
    return False

cfg.setdefault("hooks", {})
# 1. 清理所有现有 Loom entries（包括旧版本中已删除事件的 entries）
for event in list(cfg["hooks"].keys()):
    cfg["hooks"][event] = [e for e in cfg["hooks"].get(event, []) if not is_loom_entry(e)]
# 2. 写入当前版本 Loom entries
for event, loom_entries in loom.get("hooks", {}).items():
    current = cfg["hooks"].get(event, [])
    current.extend(loom_entries)
    cfg["hooks"][event] = current

cfg.setdefault("permissions", {})
for mode, rules in loom.get("permissions", {}).items():
    current = cfg["permissions"].get(mode, [])
    merged = list(current)
    for rule in rules:
        if rule not in merged:
            merged.append(rule)
    cfg["permissions"][mode] = merged

# 旧配置若曾把 loom-admin 放进 deny，会硬拒绝且无法一次性授权；安装时清掉。
deny_rules = cfg["permissions"].get("deny")
if isinstance(deny_rules, list):
    cfg["permissions"]["deny"] = [r for r in deny_rules if r != "Bash(loom-admin *)"]

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  echo "Merged Loom hooks into $settings (backup at ${settings}.bak)"
}

install_project_hooks() {
  local target="$REPO_DIR/.claude/settings.json"
  ensure_dir "$REPO_DIR/.claude"
  backup_if_exists "$target"
  "$PYTHON_BIN" - "$REPO_DIR/config/claude-settings.json.example" "$target" "$REPO_DIR/bin/loom-hook" <<'PY'
import json, sys
example_path, out_path, hook_cmd = sys.argv[1], sys.argv[2], sys.argv[3]
with open(example_path, encoding="utf-8") as f:
    cfg = json.load(f)
for entries in cfg.get("hooks", {}).values():
    for entry in entries:
        for h in entry.get("hooks", []):
            if h.get("type") == "command" and h.get("command") == "loom-hook":
                h["command"] = hook_cmd
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  echo "Installed project-level hooks to $target"
}

install_global_codex_hooks() {
  local hooks="$CODEX_HOME/hooks.json"
  local example="$REPO_DIR/config/codex-hooks.json.example"
  local hook_cmd="$REPO_DIR/bin/loom-hook"
  ensure_dir "$CODEX_HOME"
  backup_if_exists "$hooks"

  local existing
  if [[ -f "$hooks" ]]; then
    existing=$(cat "$hooks")
  else
    existing="{}"
  fi

  "$PYTHON_BIN" - "$existing" "$example" "$hooks" "$hook_cmd" <<'PY'
import json, sys
existing_json, example_path, out_path, hook_cmd = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    cfg = json.loads(existing_json)
except json.JSONDecodeError:
    cfg = {}
with open(example_path, encoding="utf-8") as f:
    loom = json.load(f)

for entries in loom.get("hooks", {}).values():
    for entry in entries:
        for h in entry.get("hooks", []):
            if h.get("type") == "command" and h.get("command") == "loom-hook":
                h["command"] = hook_cmd

def is_loom_entry(entry):
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if h.get("type") == "command" and (
            "loom-hook" in cmd
            or "loom-codex-hook" in cmd
            or "loom-admin stop-check-pending" in cmd
            or "loom hook-guard" in cmd
        ):
            return True
    return False

cfg.setdefault("hooks", {})
for event in list(cfg["hooks"].keys()):
    cfg["hooks"][event] = [e for e in cfg["hooks"].get(event, []) if not is_loom_entry(e)]
for event, loom_entries in loom.get("hooks", {}).items():
    current = cfg["hooks"].get(event, [])
    current.extend(loom_entries)
    cfg["hooks"][event] = current

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  echo "Merged Loom hooks into $hooks (backup at ${hooks}.bak)"
}

install_project_codex_hooks() {
  local target="$REPO_DIR/.codex/hooks.json"
  ensure_dir "$REPO_DIR/.codex"
  backup_if_exists "$target"
  "$PYTHON_BIN" - "$REPO_DIR/config/codex-hooks.json.example" "$target" "$REPO_DIR/bin/loom-hook" <<'PY'
import json, sys
example_path, out_path, hook_cmd = sys.argv[1], sys.argv[2], sys.argv[3]
with open(example_path, encoding="utf-8") as f:
    cfg = json.load(f)
for entries in cfg.get("hooks", {}).values():
    for entry in entries:
        for h in entry.get("hooks", []):
            if h.get("type") == "command" and h.get("command") == "loom-hook":
                h["command"] = hook_cmd
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  echo "Installed project-level Codex hooks to $target"
}

if [[ "$INSTALL_HOOKS" == 1 ]]; then
  if [[ "$PROJECT_LOCAL" == 1 ]]; then
    echo "==> Installing project-local hooks"
    [[ "$INSTALL_CLAUDE_HOOKS" == 1 ]] && install_project_hooks
    [[ "$INSTALL_CODEX_HOOKS" == 1 ]] && install_project_codex_hooks
  else
    echo "==> Installing global hooks (active only after 'loom on' or LOOM_ACTIVE=1)"
    [[ "$INSTALL_CLAUDE_HOOKS" == 1 ]] && install_global_hooks
    [[ "$INSTALL_CODEX_HOOKS" == 1 ]] && install_global_codex_hooks
  fi
else
  echo "==> Skipping hooks (--no-hooks selected; agent stop-check hooks will not be installed)"
fi

# ---------------------------------------------------------------------------
# 6. Done
# ---------------------------------------------------------------------------

echo ""
echo "Loom installed."
echo "  Home:    $LOOM_HOME"
echo "  CLI:     $BIN_DIR/loom"
if [[ "$INSTALL_CLAUDE_SKILLS" == 1 ]]; then
  echo "  Claude skills: ~/.claude/skills/loom-{digest,think,use,pipeline}"
fi
if [[ "$INSTALL_CODEX_SKILLS" == 1 ]]; then
  echo "  Codex skills:  $CODEX_HOME/skills/loom-{digest,think,use,pipeline}"
fi
if [[ "$INSTALL_HOOKS" == 1 && "$PROJECT_LOCAL" != 1 && "$INSTALL_CLAUDE_HOOKS" == 1 ]]; then
  echo "  Claude hooks: global (~/.claude/settings.json)"
fi
if [[ "$INSTALL_HOOKS" == 1 && "$PROJECT_LOCAL" != 1 && "$INSTALL_CODEX_HOOKS" == 1 ]]; then
  echo "  Codex hooks:  global ($CODEX_HOME/hooks.json)"
fi
if [[ "$INSTALL_HOOKS" == 1 && "$PROJECT_LOCAL" == 1 && "$INSTALL_CLAUDE_HOOKS" == 1 ]]; then
  echo "  Claude hooks: project-local ($REPO_DIR/.claude/settings.json)"
fi
if [[ "$INSTALL_HOOKS" == 1 && "$PROJECT_LOCAL" == 1 && "$INSTALL_CODEX_HOOKS" == 1 ]]; then
  echo "  Codex hooks:  project-local ($REPO_DIR/.codex/hooks.json)"
fi
echo ""
echo "Next steps:"
echo "  1. Review embedding settings in $LOOM_HOME/.env"
if [[ "$INSTALL_HOOKS" == 1 ]]; then
  echo "  2. In any project where you want Loom hooks active, run: loom on"
else
  echo "  2. Hooks were not installed. Re-run without --no-hooks, or use --project, if you want agent stop-check hooks."
fi
if [[ "$INSTALL_CODEX_SKILLS" == 1 ]]; then
  echo "  3. Codex may ask you to trust the installed hook source on first run."
  echo "  4. Verify with: loom stats"
else
  echo "  3. Verify with: loom stats"
fi
