"""loom.cli — CLI dispatcher and command implementations.

Each command prints JSON to stdout (success) and exits 0,
or prints an error to stderr and exits non-zero.

Privileged commands (commit-l4, apply-card-edit, update-card, delete-card,
rebuild-l4-index, stop-check) are exposed only through loom-admin. Destructive
or knowledge-architecture changes still require main-agent/user review; stop
checks may be called by Claude/Codex hooks or by an explicit manual fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from . import store, embed, checks, think_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _err(msg: str, code: int = 1) -> int:
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr)
    return code


def _current_session_id() -> str:
    """获取当前 agent session_id：env LOOM_SESSION_ID（subagent / 命令行手动模式）。

    主 agent / batch agent 派发任务前应 export LOOM_SESSION_ID=<batch_id 或 runtime session_id>；
    子 agent subprocess 继承 env，调 loom CLI 时即可读到。无 env 则空字符串（兼容旧调用方）。
    """
    return os.environ.get("LOOM_SESSION_ID", "").strip()


def _hook_session_id() -> str:
    """hook 模式：从 stdin JSON 读 session_id（Claude/Codex hook 协议）；
    命令行手动模式（无 stdin）：退化到 env LOOM_SESSION_ID。"""
    if not sys.stdin.isatty():
        try:
            data = json.load(sys.stdin)
            sid = (data.get("session_id") or "").strip()
            if sid:
                return sid
        except (json.JSONDecodeError, ValueError):
            pass
    return _current_session_id()


def _read_content(arg: str | None, file_arg: str | None) -> str:
    if file_arg:
        return Path(file_arg).read_text(encoding="utf-8")
    if arg:
        return arg
    return ""


def _json_tags_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return store.parse_tags_json(value)


def _csv_tags_arg(value: str | None) -> list[str]:
    if not value:
        return []
    return store.normalize_tags([s.strip() for s in value.split(",") if s.strip()])


def _decode_card_tags(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for row in rows:
        if "tags" in row:
            row["tags"] = store.parse_tags_json(row.get("tags") or "[]")
        row["origin"] = row.get("origin") or "ai"
    return rows


def _trace_task_id(args) -> str:
    """Task id used for read/use trace.

    Keep this explicit: either the caller passes --task-id or the surrounding
    runner exports LOOM_TASK_ID. Guessing from /tmp/loom_task is unsafe when
    batch agents run concurrently.
    """
    return (getattr(args, "task_id", None) or os.environ.get("LOOM_TASK_ID") or "").strip()


def _handle_from_topics(args, task_id: str, output_id: str, *, required: bool) -> str | None:
    """--from-topics 落账（005 §4.1）。返回错误信息或 None。

    THINK 任务（存在 think_state.json）的 L3 draft / L4 提案必须声明承接的
    主题；其余产出可选。非 THINK 任务忽略该参数。
    """
    from_topics = [
        s.strip() for s in (getattr(args, "from_topics", None) or "").split(",") if s.strip()
    ]
    if think_state.load(task_id) is None:
        return None
    if required and not from_topics:
        return (
            f"THINK 任务的产出必须用 --from-topics 声明承接的主题卡"
            f"（loom think-coverage {task_id} 查看覆盖账）；"
            f"与该产出无关的主题用 loom think-skip 记理由"
        )
    if from_topics:
        try:
            think_state.record_output(task_id, output_id, from_topics)
        except think_state.ThinkStateError as e:
            return str(e)
    return None


def cmd_think_init(args) -> int:
    materials = [m.strip() for m in (args.materials or "").split(",") if m.strip()]
    if not materials:
        return _err("think-init 需要 --materials=<domain>:<book>[,<domain>:<book>...]")
    with store.connect() as conn:
        try:
            state = think_state.init(args.task_id, args.goal, materials, conn)
        except think_state.ThinkStateError as e:
            return _err(str(e))
    _print_json({
        "status": "ok",
        "task_id": args.task_id,
        "goal": state["goal"],
        "topics_total": len(state["topics"]),
        "topics": [
            {"id": tid, "title": t["title"]}
            for tid, t in sorted(state["topics"].items())
        ],
        "hint": (
            "通读主题清单，从 L2 血肉（非主题卡）中挑最有外联价值的深挖对象；"
            "明确不深挖的主题逐个 loom think-skip 记具体理由"
        ),
    })
    return 0


def cmd_think_skip(args) -> int:
    try:
        think_state.skip(args.task_id, args.topic_id, args.reason or "")
    except think_state.ThinkStateError as e:
        return _err(str(e))
    _print_json({"status": "ok", "task_id": args.task_id, "skipped": args.topic_id})
    return 0


def cmd_think_coverage(args) -> int:
    try:
        _print_json(think_state.coverage(args.task_id))
    except think_state.ThinkStateError as e:
        return _err(str(e))
    return 0


def _append_read_trace(task_id: str, cards: list[dict[str, Any]],
                       not_found: list[str]) -> None:
    if not task_id:
        return
    try:
        trace_file = Path(f"/tmp/loom_task/{task_id}/.read_trace.jsonl")
        trace_file.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": time.time(),
            "command": "read-cards",
            "card_ids": [c["id"] for c in cards],
            "not_found": not_found,
            "cards": [
                {
                    "id": c["id"],
                    "title": c.get("title"),
                    "type": c.get("type"),
                    "layer": c.get("layer"),
                }
                for c in cards
            ],
        }
        with trace_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _record_l4_refs(task_id: str, l4_refs_update: list[str]) -> None:
    if not task_id or not l4_refs_update:
        return
    try:
        refs_file = Path(f"/tmp/loom_task/{task_id}/.l4_refs")
        refs = json.loads(refs_file.read_text(encoding="utf-8")) if refs_file.exists() else []
        for cid in l4_refs_update:
            if cid not in refs:
                refs.append(cid)
        refs_file.parent.mkdir(parents=True, exist_ok=True)
        refs_file.write_text(json.dumps(refs, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

WORKBENCH_PORT = 8765


def _ensure_workbench_backend() -> None:
    """端口探测 workbench 后端；不活则后台拉起（常驻，不随 TUI 退出关闭）。

    网页版与 TUI 共用同一后端。拉起失败（如依赖缺失）不阻塞 TUI——TUI 直连
    数据库，不依赖 HTTP。
    """
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        try:
            sock.connect(("127.0.0.1", WORKBENCH_PORT))
            return  # 已存活
        except OSError:
            pass
    finally:
        sock.close()

    import pathlib
    import subprocess
    import sys

    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    log = pathlib.Path(os.environ.get("LOOM_HOME", str(pathlib.Path.home() / ".loom")))
    log = log / "workbench.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"\n--- loom tui: 拉起 workbench 后端 ({time.strftime('%Y-%m-%d %H:%M:%S')}) ---\n")
        f.flush()
        subprocess.Popen(
            [sys.executable, "-m", "workbench.backend.main"],
            cwd=str(repo_root),
            stdout=f,
            stderr=f,
            start_new_session=True,
        )


def cmd_tui(args) -> int:
    """终端卡片阅读器（loom tui）。"""
    import pathlib
    import sys

    # workbench 包在 repo 根（bin/loom 只把 src 加入 path）
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    _configure_textual_ime()
    _ensure_workbench_backend()
    from workbench.tui.app import WorkbenchApp

    app = WorkbenchApp()
    app.run()
    return 0


def _configure_textual_ime() -> None:
    """Keep iTerm2's IME in control of candidate-selection keys.

    Textual's Kitty keyboard mode asks iTerm2 to report every key. On macOS this
    makes Pinyin candidate keys such as 1/2/3 reach the TUI before the IME can
    commit the selected text. Textual reads this switch at import time, so it
    must be set before importing ``workbench.tui``.
    """
    is_iterm = (
        os.environ.get("TERM_PROGRAM") == "iTerm.app"
        or os.environ.get("LC_TERMINAL") == "iTerm2"
    )
    if sys.platform == "darwin" and is_iterm:
        os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"


def cmd_import_source(args) -> int:
    """注册 L1 source card：layer=L1, type=source。

    把 markdown 注册成统一卡片体系中的一张 L1 卡，全文写入 cards.content，
    source 字段保留原始 markdown 路径，同步 FTS / embedding / 卡片镜像。
    """
    source_id = args.source_id
    title = args.title
    path = args.path
    if not store.NS_PATTERN_L1.match(source_id):
        return _err(
            f"L1 source card id 不符合 namespace 格式: {source_id}；"
            f"期望 <领域>:<书>:src:<单元ID>，如 llm:harness:src:08 或 llm:harness:src:full"
        )
    p = Path(path)
    if not p.is_absolute():
        p = store.PROJECT_ROOT / path
    if not p.exists():
        return _err(f"file not found: {p}")
    existing = store.get_card(source_id)
    if existing:
        return _err(
            f"card_id {source_id} 已存在（layer={existing.get('layer')}），"
            f"请改用其他 id 或先 loom-admin delete-card"
        )

    try:
        emb_vec = embed.embed(p.read_text(encoding="utf-8"))
    except Exception as e:
        sys.stderr.write(f"warning: embedding failed: {e}\n")
        emb_vec = None

    card = store.insert_source_card(
        source_id=source_id,
        title=title,
        path=str(p),
        embedding=emb_vec,
    )
    _print_json({
        "status": "imported",
        "card_id": source_id,
        "title": title,
        "source": card["source"],
        "content_size": len(card["content"]),
    })
    return 0


def cmd_read_source(args) -> int:
    path_or_id = args.path
    card = store.get_card(path_or_id)
    if card and card.get("layer") == "L1" and card.get("type") == "source":
        text = card["content"]
        store.bump_l1_card_use_by_path(card["source"] or path_or_id)
        _print_json({"card": card, "content": text, "size": len(text)})
        return 0

    path = Path(path_or_id)
    if not path.is_absolute():
        path = store.PROJECT_ROOT / path
    if not path.exists():
        return _err(f"file not found: {path}")
    text = path.read_text(encoding="utf-8")
    source_card_id = store._l1_source_card_id_from_path(str(path))
    if source_card_id:
        store.bump_l1_card_use_by_path(str(path.relative_to(store.PROJECT_ROOT)))
    _print_json({"path": str(path), "size": len(text), "content": text, "source_card_id": source_card_id})
    return 0


def cmd_read_cards(args) -> int:
    """读一张或多张卡（bump use_count，记录 read trace / L4 refs）。"""
    ids = args.ids
    cards = []
    not_found = []
    l4_refs_update: list[str] = []
    task_id = _trace_task_id(args)

    for card_id in ids:
        card = store.get_card(card_id, increment_use=True)
        if card is None:
            not_found.append(card_id)
            continue
        card["links"] = store.get_links(card_id)
        card["origin"] = card.get("origin") or "ai"
        card["tags"] = store.parse_tags_json(card.get("tags") or "[]")
        if card.get("layer") == "L1":
            card = {
                "id": card["id"],
                "title": card["title"],
                "type": card["type"],
                "layer": card["layer"],
                "source": card.get("source") or "",
                "origin": card["origin"],
                "tags": card["tags"],
                "use_count": card.get("use_count", 0),
                "search_count": card.get("search_count", 0),
                "snippet": card["content"][:200],
                "content_size": len(card["content"]),
                "has_full_content": True,
                "links": card["links"],
            }
        cards.append(card)
        if card.get("layer") == "L4":
            l4_refs_update.append(card_id)

    _append_read_trace(task_id, cards, not_found)
    _record_l4_refs(task_id, l4_refs_update)

    _print_json({"cards": cards, "not_found": not_found})
    return 0 if cards else 1


def cmd_read_l4_index(args) -> int:
    # Cached read: rebuild only when L4 cards are newer than the index file.
    if store.L4_INDEX_PATH.exists():
        index_mtime = store.L4_INDEX_PATH.stat().st_mtime
        with store.connect() as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) FROM cards WHERE layer = 'L4'"
            ).fetchone()
            latest_l4 = float(row[0]) if row and row[0] else 0.0
        if latest_l4 > index_mtime:
            store.rebuild_l4_index()
    else:
        store.rebuild_l4_index()

    text = store.L4_INDEX_PATH.read_text(encoding="utf-8")
    _print_json({"path": str(store.L4_INDEX_PATH), "content": text})
    return 0


def cmd_search(args) -> int:
    top = args.top or 10
    tags = _csv_tags_arg(args.tag)
    if args.mode == "fts":
        results = store.search_fts(args.query, top=top, ns=args.ns, type_=args.type, tags=tags)
    elif args.mode == "vector":
        try:
            vec = embed.embed(args.query)
        except Exception as e:
            return _err(f"embedding failed: {e}")
        results = store.search_vector(vec, top=top, ns=args.ns, type_=args.type, tags=tags)
    else:  # hybrid (default)
        try:
            vec = embed.embed(args.query)
        except Exception as e:
            sys.stderr.write(f"warning: embedding failed, falling back to fts: {e}\n")
            results = store.search_fts(args.query, top=top, ns=args.ns, type_=args.type, tags=tags)
            _print_json({"mode": "fts_fallback", "count": len(results), "results": _decode_card_tags(results)})
            return 0
        results = store.search_hybrid(args.query, vec, top=top, ns=args.ns, type_=args.type, tags=tags)

    _print_json({"mode": args.mode, "count": len(results), "results": _decode_card_tags(results)})
    return 0


def cmd_browse(args) -> int:
    ns = args.namespace
    prefix = args.prefix or ""
    pattern = f"{ns}:%{prefix}%"
    tags = _csv_tags_arg(args.tag)
    with store.connect() as conn:
        sql = "SELECT id, title, type, layer, origin, tags FROM cards c WHERE c.id LIKE ?"
        params: list[Any] = [pattern]
        tag_sql, tag_params = store._tag_filter_sql(tags, alias="c")
        sql += tag_sql + " ORDER BY c.id"
        params.extend(tag_params)
        rows = conn.execute(sql, params).fetchall()
    _print_json({"namespace": ns, "count": len(rows), "cards": _decode_card_tags([dict(r) for r in rows])})
    return 0


def cmd_children(args) -> int:
    children = store.get_children(args.id)
    _print_json({"parent": args.id, "count": len(children), "children": children})
    return 0


def cmd_siblings(args) -> int:
    sibs = store.get_siblings(args.id)
    _print_json({"id": args.id, "count": len(sibs), "siblings": sibs})
    return 0


def cmd_neighbors(args) -> int:
    nbrs = store.get_neighbors(args.id, depth=args.depth or 1)
    _print_json({"id": args.id, "depth": args.depth or 1,
                 "count": len(nbrs), "neighbors": nbrs})
    return 0


def cmd_stats(args) -> int:
    _print_json(store.stats())
    return 0


def cmd_namespaces(args) -> int:
    _print_json({"namespaces": store.namespaces()})
    return 0


# ---------------------------------------------------------------------------
# Exploration tools (低摩擦翻阅姿势)
# ---------------------------------------------------------------------------

def cmd_orient(args) -> int:
    """启动时定位——读 orient.md（namespace 全貌 + L4 全量含命题摘要）。
    带缓存：任何卡 updated_at > 目录 mtime 即重建（orient 包含 namespace 全貌，不只 L4）。
    """
    if store.ORIENT_PATH.exists():
        dir_mtime = store.ORIENT_PATH.stat().st_mtime
        with store.connect() as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) FROM cards"
            ).fetchone()
            latest = float(row[0]) if row and row[0] else 0.0
        if latest > dir_mtime:
            store.rebuild_directory()
    else:
        store.rebuild_directory()

    text = store.ORIENT_PATH.read_text(encoding="utf-8")
    _print_json({"path": str(store.ORIENT_PATH), "content": text})
    return 0


def cmd_skim(args) -> int:
    """轻量浏览一张卡——title + 首段 + links，不 bump use_count。
    用于快速判断一张卡是否值得深读（read-cards）。"""
    card = store.get_card(args.id, increment_use=False)
    if card is None:
        return _err(f"card not found: {args.id}")

    first_para = card["content"].lstrip().split("\n\n", 1)[0].strip()
    _print_json({
        "id": card["id"],
        "title": card["title"],
        "type": card["type"],
        "layer": card["layer"],
        "source": card.get("source"),
        "first_paragraph": first_para,
        "links": store.get_links(args.id),
    })
    return 0


def cmd_wander(args) -> int:
    """link 图随机游走——从一张卡出发，随机走 N 步，返回路径。
    用于打破信息茧房，发现意外关联。"""
    import random
    steps = args.steps or 3
    if store.get_card(args.id, increment_use=False) is None:
        return _err(f"card not found: {args.id}")

    path = [args.id]
    seen = {args.id}
    current = args.id
    for _ in range(steps):
        nbrs = [t for t in store.get_links(current) if t not in seen]
        if not nbrs:
            break
        current = random.choice(nbrs)
        path.append(current)
        seen.add(current)

    cards = []
    with store.connect() as conn:
        for cid in path:
            row = conn.execute(
                "SELECT id, title, type, layer FROM cards WHERE id=?", (cid,)
            ).fetchone()
            if row:
                cards.append(dict(row))
    _print_json({"start": args.id, "steps": steps, "path": path, "cards": cards})
    return 0


def cmd_suggest_links(args) -> int:
    """给定一张卡，跑 embedding 找未 link 的语义近邻。
    用于构建阶段补全 loom 网络的缺口（embedding 的价值沉淀进显性 link）。"""
    card = store.get_card(args.id, increment_use=False)
    if card is None:
        return _err(f"card not found: {args.id}")

    existing_links = set(store.get_links(args.id))
    existing_links.add(args.id)

    query_text = f"{card['title']}\n{card['content']}"
    try:
        query_vec = embed.embed(query_text)
    except Exception as e:
        return _err(f"embedding failed: {e}")

    top_k = args.top or 10
    candidates = store.search_vector(query_vec, top=top_k * 3, bump_search=False)
    suggestions = []
    for c in candidates:
        if c["id"] in existing_links:
            continue
        suggestions.append(c)
        if len(suggestions) >= top_k:
            break

    _print_json({
        "card_id": args.id,
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
    })
    return 0


def cmd_browse_tree(args) -> int:
    """namespace 主题树——namespace → 主题卡 → 卢曼结构后代。"""
    ns = args.namespace
    with store.connect() as conn:
        rows = [dict(row) for row in conn.execute("""
            SELECT id, title, type FROM cards
            WHERE id LIKE ?
            ORDER BY id
        """, (f"{ns}:%",)).fetchall()]

        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            parent_id = store._parent_id(row["id"])
            if parent_id is not None:
                children_by_parent.setdefault(parent_id, []).append(row)

        def descendants(ancestor_id: str) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            pending = list(children_by_parent.get(ancestor_id, []))
            while pending:
                row = pending.pop()
                result.append(row)
                pending.extend(children_by_parent.get(row["id"], []))
            return sorted(result, key=lambda row: row["id"])

        tree = []
        for r in (row for row in rows if row["type"] == "主题"):
            theme_id = r["id"]
            children_rows = descendants(theme_id)
            tree.append({
                "theme": {"id": theme_id, "title": r["title"]},
                "children_count": len(children_rows),
                "children": children_rows,
            })

    _print_json({"namespace": ns, "themes_count": len(tree), "themes": tree})
    return 0


# ---------------------------------------------------------------------------
# write-draft (with inline computational checks)
# ---------------------------------------------------------------------------

def cmd_write_draft(args) -> int:
    """Write a draft card. Runs all 12 per-card checks inline.

    Draft files are stored at /tmp/loom_task/<task_id>/drafts/<card_id>.md
    with YAML frontmatter + content body. NOT committed to DB yet.

    layer 语义：plan/task 可以写 L1 / L2_light / L2 / L3 / L4（task target）；
    card layer 只有 L1 / L2 / L3 / L4。L2_light 任务的 draft 写入时被强制
    归一为 L2（card layer 不存在 L2_light，§008-25）。
    """
    task_id = args.task_id
    content = _read_content(None, args.content_file) or args.content or ""
    plan = checks.load_plan(task_id) or {}
    plan_target_layer = plan.get("layer")
    layer = args.layer or plan_target_layer or "L2"
    if layer == "L2_light":
        layer = "L2"

    # B0: task_id 占用检测 + flock 防并行 race（#10 修复）
    # 第一个创建 task 目录的 agent 写 .session_id；后续调用必须匹配同一 session，
    # 否则视为并行 agent 撞 task_id（deep_psy_X_05 被两个 agent 同时派发等场景）。
    task_dir = Path(f"/tmp/loom_task/{task_id}")
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_path = task_dir / ".drafts.lock"
    current_sid = _current_session_id()
    import fcntl
    with open(lock_path, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _err(
                f"task {task_id} 正被另一进程写入（drafts.lock 被占）；"
                f"若确实并行处理同一 task，让原 agent 完成后再调。"
            )
        sid_file = task_dir / ".session_id"
        if sid_file.exists():
            owner_sid = sid_file.read_text(encoding="utf-8").strip()
            if current_sid and owner_sid and owner_sid != current_sid:
                return _err(
                    f"task_id {task_id!r} 已被 session {owner_sid} 占用（status=running），"
                    f"当前 session={current_sid} 不能再写——并行 agent 应使用唯一 task_id "
                    f"（加时间戳/uuid 后缀，如 {task_id}_{int(time.time())}）。"
                )
        else:
            # 本 agent 是第一个；记录占用者
            sid_file.write_text(current_sid, encoding="utf-8")
        # flock 在 with 块退出时自动释放

    # B1: 强制 L2 plan 必须有 phase ∈ {scout, deep}（堵"单阶段绕过"漏洞）
    if layer == "L2":
        phase = plan.get("phase")
        if phase not in ("scout", "deep"):
            return _err(
                f"L2 任务 plan 必须有 phase=scout|deep（当前 phase={phase!r}）。"
                f"两阶段流程：scout 先建主题卡，deep 再产 L2 卡。"
            )
        # B1a: Scout 只能写主题卡；Deep 不能写主题卡
        if phase == "scout" and args.type != "主题":
            return _err(
                f"Scout 阶段只允许写 type=主题 的卡（当前 type={args.type!r}）。"
                f"主题卡由 Scout 建立，其余 type 在 Deep 阶段产出。"
            )
        if phase == "deep" and args.type == "主题":
            return _err(
                "Deep 阶段不允许写 type=主题 的卡——主题卡应由 Scout 阶段建立。"
            )
        # B1b: Deep 任务前置校验 topic_card 存在性——避免 agent 白跑整章后被 stop-check 拒
        if phase == "deep":
            topic_card_id = plan.get("topic_card")
            if not topic_card_id:
                return _err(
                    f"Deep 任务 plan.json 缺 topic_card 字段——"
                    f"应指定已 commit 的主题卡 id（Scout 产物）"
                )
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT type, layer FROM cards WHERE id=?", (topic_card_id,)
                ).fetchone()
            if row is None:
                return _err(
                    f"Deep 任务 plan.json 的 topic_card {topic_card_id} 未在库中——"
                    f"Scout 是否 commit 成功？本任务无法继续，需先完成 Scout"
                )
            if row["type"] != "主题":
                return _err(
                    f"Deep 任务的 topic_card {topic_card_id} 不是主题卡"
                    f"（type={row['type']}）——plan.json 配置有误"
                )

    links = []
    if args.links:
        links = [s.strip() for s in args.links.split(",") if s.strip()]
    try:
        origin = store.normalize_origin(args.origin)
    except ValueError as e:
        return _err(str(e))

    draft = checks.CardDraft(
        id=args.card_id,
        title=args.title or args.card_id,
        type=args.type,
        content=content,
        source=args.source,
        layer=layer,
        origin=origin,
        links=links,
    )

    # Load existing drafts in the task (for intra-task link checks)
    existing_drafts = checks.list_drafts(task_id)
    # exclude self if re-writing
    existing_drafts = [d for d in existing_drafts if d.id != draft.id]

    # Run all 12 per-card checks
    with store.connect() as conn:
        results = checks.run_per_card_checks(draft, conn, existing_drafts + [draft])

    failures = [r for r in results if not r.passed]
    if failures:
        for f in failures:
            store.log_reject(task_id, draft.id, f.check_id, f.reason, "write_draft")
        out = {
            "status": "rejected",
            "card_id": draft.id,
            "failures": [
                {"check_id": f.check_id, "reason": f.reason} for f in failures
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2  # non-zero to signal Claude Code / caller

    # THINK 任务：--from-topics 落账（L3 draft 必须声明，其余可选）
    topic_err = _handle_from_topics(args, task_id, draft.id, required=(draft.layer == "L3"))
    if topic_err:
        return _err(topic_err)

    # All passed — write the draft file
    drafts_dir = Path(f"/tmp/loom_task/{task_id}/drafts")
    drafts_dir.mkdir(parents=True, exist_ok=True)
    draft_path = drafts_dir / f"{draft.id}.md"
    fm_lines = [
        "---",
        f"id: {draft.id}",
        f"title: {draft.title}",
        f"type: {draft.type}",
        f"layer: {draft.layer}",
        f"source: {draft.source or ''}",
    ]
    if draft.origin == "human":
        fm_lines.append("origin: human")
    fm_lines += [
        f"links: {','.join(draft.links)}",
        "---",
        "",
        draft.content,
        "",
    ]
    draft_path.write_text("\n".join(fm_lines), encoding="utf-8")
    _print_json({
        "status": "ok",
        "card_id": draft.id,
        "draft_path": str(draft_path),
        "checks_passed": [r.check_id for r in results],
    })
    return 0


# ---------------------------------------------------------------------------
# Proposal commands (L4 evolution, write to staging)
# ---------------------------------------------------------------------------


def cmd_propose_l4(args) -> int:
    """L4 新模式提案。agent 显式指定 <card_id>（卢曼树形 ID），不自动分配。

    卢曼 ID 表达树形关系（005 §2.1）：
    - 新顶级模式 → gen:Na（N 是下一个数字）
    - 已有模式 gen:Xa 的深化 → gen:XaY
    agent 在提案时知道这张卡在 L4 树里的位置，自己拍 ID。
    """
    task_id = args.task_id
    card_id = args.card_id
    content = _read_content(None, args.content_file) or args.content or ""
    related = []
    if args.related:
        related = [s.strip() for s in args.related.split(",") if s.strip()]

    draft = checks.CardDraft(
        id=card_id,
        title=args.title,
        type=args.type,
        content=content,
        layer="L4",
        links=related,
    )
    with store.connect() as conn:
        results = checks.run_per_card_checks(draft, conn, [draft])
    failures = [
        r for r in results
        if not r.passed and r.check_id not in ("source_real", "l2_no_cross_domain")
    ]
    if failures:
        for f in failures:
            store.log_reject(task_id, card_id, f.check_id, f.reason, "propose_l4")
        print(json.dumps({
            "status": "rejected",
            "card_id": card_id,
            "failures": [
                {"check_id": f.check_id, "reason": f.reason} for f in failures
            ],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    # THINK 任务：L4 提案必须 --from-topics 落账
    topic_err = _handle_from_topics(args, task_id, card_id, required=True)
    if topic_err:
        return _err(topic_err)

    staging_dir = Path(f"/tmp/loom_task/{task_id}/staging")
    staging_dir.mkdir(parents=True, exist_ok=True)
    proposal_id = f"prop_{uuid.uuid4().hex[:8]}"
    record = {
        "proposal_id": proposal_id,
        "task_id": task_id,
        "kind": "new",
        "target_id": card_id,
        "title": args.title,
        "content": content,
        "related_cards": related,
        "type": args.type,
        "status": "pending",
        "checks_passed": [r.check_id for r in results if r.check_id != "source_real"],
        "created_at": time.time(),
    }
    out_path = staging_dir / f"{proposal_id}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({"status": "ok", "proposal_id": proposal_id, "card_id": card_id, "path": str(out_path)})
    return 0



def cmd_propose_card_edit(args) -> int:
    """提案更新已有卡（L2/L3/L4 通用）。写 staging，全量替换语义。
    子 agent 需先 read-cards 获取原内容，编辑成完整新版后提案。
    """
    task_id = args.task_id
    content = _read_content(None, args.content_file) or args.content or ""
    existing = store.get_card(args.card_id)
    if not existing:
        return _err(f"card not found: {args.card_id}")
    new_title = args.title or existing["title"]
    related = []
    if args.related:
        related = [s.strip() for s in args.related.split(",") if s.strip()]

    links = store.get_links(args.card_id)
    merged_links = list(dict.fromkeys([*links, *related]))
    draft = checks.CardDraft(
        id=args.card_id,
        title=new_title,
        type=existing["type"],
        content=content,
        source=existing["source"],
        layer=existing["layer"],
        links=merged_links,
    )
    with store.connect() as conn:
        results = checks.run_per_card_checks(draft, conn, [draft])
    failures = [r for r in results if not r.passed and r.check_id != "card_id_unique"]
    if failures:
        for f in failures:
            store.log_reject(task_id, args.card_id, f.check_id, f.reason, "propose_card_edit")
        print(json.dumps({
            "status": "rejected",
            "card_id": args.card_id,
            "failures": [
                {"check_id": f.check_id, "reason": f.reason} for f in failures
            ],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    # THINK 任务：card-edit 提案可选 --from-topics 落账（修正不算主题承接，不强制）
    topic_err = _handle_from_topics(args, task_id, args.card_id, required=False)
    if topic_err:
        return _err(topic_err)

    staging_dir = Path(f"/tmp/loom_task/{task_id}/staging")
    staging_dir.mkdir(parents=True, exist_ok=True)
    proposal_id = f"edit_{uuid.uuid4().hex[:8]}"
    record = {
        "proposal_id": proposal_id,
        "task_id": task_id,
        "kind": "edit",
        "target_id": args.card_id,
        "title": new_title,
        "old_title": existing["title"],
        "content": content,
        "related_cards": related,
        "edit_type": args.type,
        "status": "pending",
        "checks_passed": [r.check_id for r in results if r.check_id != "card_id_unique"],
        "created_at": time.time(),
    }
    out_path = staging_dir / f"{proposal_id}.json"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({"status": "ok", "proposal_id": proposal_id, "path": str(out_path)})
    return 0


# ---------------------------------------------------------------------------
# Privileged commands (only hook or main agent after user review)
# ---------------------------------------------------------------------------

PRIVILEGED_NOTE = "（特权命令：仅 hook 或主 agent 用户审核后调用）"


def cmd_commit_l4(args) -> int:
    """Commit an approved L4 proposal from staging. Privileged."""
    prop_path = Path(args.proposal)
    if not prop_path.exists():
        return _err(f"proposal not found: {prop_path}")
    record = json.loads(prop_path.read_text(encoding="utf-8"))
    if record.get("kind") != "new":
        return _err(f"proposal kind is '{record.get('kind')}', commit-l4 requires kind='new'")
    if record.get("status") != "approved":
        return _err(f"proposal status is '{record.get('status')}', must be 'approved'")

    content = record["content"]
    title = record["title"]
    related = record.get("related_cards", [])
    l4_type = record.get("type", "模式")
    if l4_type not in ("模式", "判断", "反思"):
        return _err(f"invalid L4 type: {l4_type} (must be 模式/判断/反思)")
    card_id = record.get("target_id")
    if not card_id:
        return _err("approved proposal missing target_id; re-run propose-l4 with current CLI")

    try:
        emb_vec = embed.embed(f"{title}\n{content}")
    except Exception as e:
        sys.stderr.write(f"warning: embedding failed: {e}\n")
        emb_vec = None

    store.insert_card(
        card_id=card_id, title=title, type_=l4_type,
        content=content, layer="L4", source=None,
        links=related, embedding=emb_vec,
    )
    # mark proposal file as consumed（避免重复 commit；JSON 是 SSOT，无额外表）
    record["status"] = "consumed"
    prop_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({"status": "committed", "card_id": card_id, "privileged": True})
    return 0


def cmd_proposal_decision(args) -> int:
    """loom-admin proposal-decision <proposal.json> --decision=approved|rejected [--reason=...]

    封装 staging JSON 的 status 流转——主 agent 用户审核后调用，避免手改 JSON。
    JSON 是 SSOT，无额外表。rejected 留在 staging/ 不删，方便事后审计。
    """
    prop_path = Path(args.proposal)
    if not prop_path.exists():
        return _err(f"proposal not found: {prop_path}")
    record = json.loads(prop_path.read_text(encoding="utf-8"))
    if record.get("status") == "consumed":
        return _err(f"proposal already consumed (card committed); cannot change decision")
    record["status"] = args.decision
    if args.reason:
        record["decision_reason"] = args.reason
    record["decided_at"] = time.time()
    prop_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({
        "status": "decided", "proposal_id": record.get("proposal_id"),
        "decision": args.decision, "path": str(prop_path), "privileged": True,
    })
    return 0


def cmd_apply_card_edit(args) -> int:
    """特权：全量替换已有卡 content（L2/L3/L4 通用）。
    提案 content 即为新完整内容，替换而非追加。
    """
    prop_path = Path(args.proposal)
    if not prop_path.exists():
        return _err(f"proposal not found: {prop_path}")
    record = json.loads(prop_path.read_text(encoding="utf-8"))
    if record.get("kind") != "edit":
        return _err(f"proposal kind is '{record.get('kind')}', apply-card-edit requires kind='edit'")
    if record.get("status") != "approved":
        return _err(f"proposal status is '{record.get('status')}', must be 'approved'")
    target_id = record["target_id"]
    existing = store.get_card(target_id)
    if not existing:
        return _err(f"card not found: {target_id}")
    new_content = record["content"]
    new_title = record.get("title") or existing["title"]
    try:
        emb_vec = embed.embed(f"{new_title}\n{new_content}")
    except Exception:
        emb_vec = None
    store.update_card_content(target_id, content=new_content, title=new_title, embedding=emb_vec)
    # add related links if any
    if record.get("related_cards"):
        with store.connect() as conn:
            for lid in record["related_cards"]:
                store.add_link(conn, target_id, lid)
    record["status"] = "consumed"
    prop_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({
        "status": "applied",
        "card_id": target_id,
        "title": new_title,
        "edit_type": record.get("edit_type"),
    })
    return 0


def cmd_rebuild_l4_index(args) -> int:
    count = store.rebuild_l4_index()
    _print_json({"status": "ok", "l4_count": count, "path": str(store.L4_INDEX_PATH)})
    return 0


def cmd_rebuild_tag_index(args) -> int:
    count = store.rebuild_tag_index()
    _print_json({"status": "ok", "indexed_tags": count})
    return 0


def _chunks(items: list[dict[str, str]], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def cmd_rebuild_embeddings(args) -> int:
    batch_size = max(1, args.batch_size)
    cfg = embed.get_config()
    cards = store.list_cards_for_embedding()
    if cards:
        try:
            embed.embed(f"{cards[0]['title']}\n{cards[0]['content']}")
        except Exception as e:
            return _err(f"embedding provider check failed: {e}")

    store.reset_vector_index(cfg.dim)
    embedded = 0
    for batch in _chunks(cards, batch_size):
        texts = [f"{c['title']}\n{c['content']}" for c in batch]
        try:
            vectors = embed.embed_batch(texts)
        except Exception as e:
            return _err(f"embedding batch failed after {embedded} cards: {e}")
        store.upsert_embeddings_batch([c["id"] for c in batch], vectors)
        embedded += len(batch)

    _print_json({
        "status": "ok",
        "provider": cfg.provider,
        "model": cfg.model,
        "dim": cfg.dim,
        "embedded": embedded,
    })
    return 0


def cmd_tag_card(args) -> int:
    if args.add is None and args.remove is None:
        return _err("tag-card requires --add and/or --remove")
    try:
        add = _json_tags_arg(args.add)
        remove = _json_tags_arg(args.remove)
        card = store.update_card_tags(args.card_id, add=add, remove=remove)
    except ValueError as e:
        return _err(str(e))
    _print_json({
        "status": "updated",
        "card_id": args.card_id,
        "tags": store.parse_tags_json(card.get("tags") or "[]"),
        "privileged": True,
    })
    return 0


def cmd_update_card(args) -> int:
    """[特权] 更新已入库卡片字段。主 agent 用户审核后调用。

    支持 title/type/content/source/origin/links 字段更新；layer 是卡的认知身份，不支持热改。
    跑 per-card 校验（作用于更新后的状态；更新场景跳过 card_id_unique）。
    content 改变时自动重新 embed。L4 派生索引走 pull 模式，不在更新时重建。
    """
    card_id = args.card_id
    existing = store.get_card(card_id)
    if not existing:
        return _err(f"card not found: {card_id}")

    # 计算新值（None 表示不变）
    new_title = args.title if args.title is not None else existing["title"]
    new_type = args.type if args.type is not None else existing["type"]
    if args.content is not None:
        new_content = args.content
    elif args.content_file:
        new_content = Path(args.content_file).read_text(encoding="utf-8")
    else:
        new_content = existing["content"]
    new_layer = existing["layer"]
    new_source = args.source if args.source is not None else existing["source"]
    try:
        new_origin = store.normalize_origin(args.origin if args.origin is not None else existing.get("origin"))
    except ValueError as e:
        return _err(str(e))
    if args.links is not None:
        new_links = [s.strip() for s in args.links.split(",") if s.strip()]
    else:
        new_links = store.get_links(card_id)

    # 跑校验（更新后的状态）
    draft = checks.CardDraft(
        id=card_id, title=new_title, type=new_type,
        content=new_content, source=new_source,
        layer=new_layer, origin=new_origin, links=new_links,
    )
    with store.connect() as conn:
        results = checks.run_per_card_checks(draft, conn, [draft])
    failures = [r for r in results if not r.passed and r.check_id != "card_id_unique"]
    if failures:
        for f in failures:
            store.log_reject(None, card_id, f.check_id, f.reason, "update_card")
        print(json.dumps({
            "status": "rejected", "card_id": card_id,
            "failures": [{"check_id": f.check_id, "reason": f.reason} for f in failures],
        }, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    # content 改变时重新 embed
    embedding = None
    if new_content != existing["content"]:
        try:
            embedding = embed.embed(f"{new_title}\n{new_content}")
        except Exception as e:
            sys.stderr.write(f"warning: embedding failed: {e}\n")

    content_for_update = None
    if args.content is not None:
        content_for_update = args.content
    elif args.content_file:
        content_for_update = Path(args.content_file).read_text(encoding="utf-8")

    store.update_card(
        card_id=card_id,
        title=args.title,
        type_=args.type,
        content=content_for_update,
        source=args.source,
        origin=args.origin,
        links=new_links if args.links is not None else None,
        embedding=embedding,
    )

    _print_json({
        "status": "updated", "card_id": card_id,
        "privileged": True,
    })
    return 0


def cmd_delete_card(args) -> int:
    """[特权] 删除卡片。主 agent 用户审核后调用。

    Cascade 删 links / cards_vec / cards_fts（trigger）+ cards/<ns>/<id>.md 镜像。
    """
    card_id = args.card_id
    existing = store.get_card(card_id)
    if not existing:
        return _err(f"card not found: {card_id}")
    try:
        store.delete_card(card_id)
    except ValueError as exc:
        return _err(str(exc))
    _print_json({
        "status": "deleted", "card_id": card_id,
        "privileged": True,
    })
    return 0


def cmd_l4_upgrade_candidates(args) -> int:
    """L4 升级候选（基于 use_count + 反思数 + 跨域），纯 SQL 不调 LLM。

    信号来自 004 §15「L4 模式的演化机制」：use_count 高 + 反思修正次数多 + 跨域验证。
    maturity 从 content 第一段即时提取（pull 模式，不依赖额外表）。
    """
    use_thr = args.use_count or 10
    refl_thr = args.reflections or 2
    dom_thr = args.domains or 2

    candidates = []
    with store.connect() as conn:
        rows = conn.execute("""
            SELECT id, title, use_count, content
            FROM cards
            WHERE layer = 'L4'
        """).fetchall()

        for row in rows:
            # maturity 从 content 第一段提取（pull 模式）
            first_line = (row["content"] or "").lstrip().split("\n", 1)[0]
            m = store.MATURITY_PATTERN.match(first_line)
            maturity = m.group(1) if m else "探索期"
            if maturity != "探索期":
                continue  # 已熟练，不列为升级候选

            cid = row["id"]
            reflections = conn.execute("""
                SELECT COUNT(*) FROM links l
                JOIN cards c ON l.source_id = c.id
                WHERE l.target_id = ? AND c.type = '反思'
            """, (cid,)).fetchone()[0]

            domains = conn.execute("""
                SELECT COUNT(DISTINCT substr(c.id, 1, instr(c.id, ':')-1))
                FROM links l
                JOIN cards c ON l.source_id = c.id
                WHERE l.target_id = ? AND instr(c.id, ':') > 0
                  AND substr(c.id, 1, instr(c.id, ':')-1) != 'gen'
            """, (cid,)).fetchone()[0]

            if (row["use_count"] >= use_thr
                and reflections >= refl_thr
                and domains >= dom_thr):
                candidates.append({
                    "card_id": cid, "title": row["title"],
                    "use_count": row["use_count"],
                    "reflections": reflections, "domains": domains,
                })

    _print_json({
        "candidates": candidates,
        "thresholds": {"use_count": use_thr, "reflections": refl_thr, "domains": dom_thr},
        "hint": "升级走 loom-admin update-card <id> --content-file=<...>（[探索期] → [熟练期]）",
    })
    return 0


def cmd_silent_cards(args) -> int:
    """沉默卡（use_count=0 AND search_count=0），纯 SQL。

    来自 004 §13.4「后续可基于这两个指标做沉默卡提醒」。
    当前由 agent 主动调用；不再通过 SessionStart 自动注入。
    """
    min_age_days = args.min_age_days or 0
    age_cutoff = time.time() - min_age_days * 86400 if min_age_days > 0 else 0

    with store.connect() as conn:
        rows = conn.execute("""
            SELECT id, title, type, layer, created_at
            FROM cards
            WHERE use_count = 0 AND search_count = 0 AND created_at <= ?
            ORDER BY created_at
        """, (age_cutoff,)).fetchall()

    _print_json({
        "silent_count": len(rows),
        "cards": [dict(r) for r in rows],
        "hint": "沉默卡可能过时/孤立/从未被需要——回炉或删除",
    })
    return 0


# ---------------------------------------------------------------------------
# Session activation / global hooks guard
# Hooks are installed globally but only run for projects/sessions marked active.
# ---------------------------------------------------------------------------

ACTIVE_DIR = store.PROJECT_ROOT / "active"
PROJECTS_FILE = ACTIVE_DIR / "projects"


def _active_projects() -> list[str]:
    if not PROJECTS_FILE.exists():
        return []
    try:
        return [l.strip() for l in PROJECTS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []


def _current_project() -> str:
    return os.getcwd()


def _is_active_project() -> bool:
    """Return True if cwd is under an activated project path."""
    if os.environ.get("LOOM_ACTIVE"):
        return True
    cwd = _current_project()
    for p in _active_projects():
        if cwd == p or cwd.startswith(p + os.sep):
            return True
    return False


def cmd_activate(args) -> int:
    """Activate Loom hooks for the current project/session."""
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    cwd = _current_project()
    projects = set(_active_projects())
    projects.add(cwd)
    PROJECTS_FILE.write_text("\n".join(sorted(projects)) + "\n", encoding="utf-8")
    print(f"Loom activated for {cwd}", file=sys.stderr)
    _print_json({"status": "activated", "project": cwd})
    return 0


def cmd_deactivate(args) -> int:
    """Deactivate Loom hooks for the current project/session."""
    cwd = _current_project()
    projects = [p for p in _active_projects() if p != cwd]
    if projects:
        PROJECTS_FILE.write_text("\n".join(projects) + "\n", encoding="utf-8")
    else:
        PROJECTS_FILE.unlink(missing_ok=True)
    print(f"Loom deactivated for {cwd}", file=sys.stderr)
    _print_json({"status": "deactivated", "project": cwd})
    return 0


def cmd_hook_guard(args) -> int:
    """Guard used by global hooks. Inactive sessions emit JSON continue=false to halt hook chain."""
    active = _is_active_project()
    if getattr(args, "hook", False):
        if active:
            _print_json({"continue": True, "suppressOutput": True})
        else:
            _print_json({"continue": False, "suppressOutput": True})
        return 0
    return 0 if active else 1


def _write_semantic_sample(task_id: str, drafts: list[checks.CardDraft]) -> Path:
    import random

    sample_size = min(3, len(drafts))
    sample = random.sample(drafts, sample_size) if drafts else []
    sample_ids = [d.id for d in sample]
    sample_file = Path(f"/tmp/loom_task/{task_id}/.semantic_sample.json")
    sample_payload = {
        "task_id": task_id,
        "sample_ids": sample_ids,
        "cards": [
            {
                "card_id": d.id,
                "type": d.type,
                "layer": d.layer,
                "title": d.title,
                "source": d.source,
                "content": d.content,
            }
            for d in sample
        ],
    }
    sample_file.write_text(
        json.dumps(sample_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return sample_file


# ---------------------------------------------------------------------------
# stop-check (called by Claude/Codex hooks or manual fallback)
# ---------------------------------------------------------------------------

def cmd_stop_check(args) -> int:
    """Stop-check entry. Runs computational checks + (optional) reports
    semantic-sample needs.

    Modes:
      normal  — full checks (computational + semantic block-back); on pass, commits
      salvage — same full path as normal (computational + semantic); block reason marks salvage context (008 §2)
    """
    task_id = args.task_id
    mode = args.mode or "normal"

    # 清理上次 rejected 的残留文件（本次 stop-check 会在 rejected 时重写它）
    Path(f"/tmp/loom_task/{task_id}/.rejected.json").unlink(missing_ok=True)

    # 幂等：已 done 的任务直接返回（防 hook 重复触发重复 commit）
    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, drafts_count, committed_count FROM task_trace WHERE task_id=?",
            (task_id,),
        ).fetchone()
    if row and row["status"] == "done":
        _print_json({
            "status": "already_done", "task_id": task_id,
            "drafts_count": row["drafts_count"], "committed_count": row["committed_count"],
        })
        return 0

    drafts = checks.list_drafts(task_id)
    plan = checks.load_plan(task_id) or {}

    # Register task trace if not yet present
    if not plan.get("task_id"):
        plan = {"task_id": task_id, **plan}
    store.start_task(task_id, plan)

    if not drafts:
        out = {"status": "no_drafts", "task_id": task_id}
        rejected_file = Path(f"/tmp/loom_task/{task_id}/.rejected.json")
        rejected_file.write_text(
            json.dumps({
                "status": "rejected",
                "mode": mode,
                "failures": [{
                    "card": "(task)",
                    "check": "no_drafts",
                    "reason": f"task {task_id} 没有 drafts，无法进入计算层校验",
                }],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(out, ensure_ascii=False), file=sys.stderr)
        store.end_task(task_id, "failed", drafts_count=0)
        return 2

    # Computational layer (always)
    all_failures: list[dict] = []
    with store.connect() as conn:
        for d in drafts:
            results = checks.run_per_card_checks(d, conn, drafts)
            for r in results:
                if not r.passed:
                    all_failures.append({"card": d.id, "check": r.check_id, "reason": r.reason})
                    store.log_reject(task_id, d.id, r.check_id, r.reason, "stop_hook")
        batch_results = checks.run_batch_checks(drafts, plan, conn)
        for r in batch_results:
            # "跳过"过滤只适用于 L2 phase 检查的兼容分支（scout/非L2任务的兜底文案），
            # 不能按子串匹配所有检查——think_coverage 的失败理由天然包含"跳过"
            legacy_skip = r.check_id in ("l2_has_topic", "l2_links_topic") and "跳过" in r.reason
            if not r.passed and not legacy_skip:
                all_failures.append({"card": "(batch)", "check": r.check_id, "reason": r.reason})
                store.log_reject(task_id, None, r.check_id, r.reason, "stop_hook")

    if all_failures:
        out = {"status": "rejected", "mode": mode, "failures": all_failures}
        rejected_file = Path(f"/tmp/loom_task/{task_id}/.rejected.json")
        rejected_file.write_text(
            json.dumps(out, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(out, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    # 计算层通过：写 computed_passed + semantic_sample，并 block 回 agent 做语义自检。
    # normal 与 salvage 共用此路径——salvage 也不绕过语义层（008 §2）。
    store.end_task(task_id, "computed_passed", drafts_count=len(drafts),
                   set_ended_at=False)  # 中间状态，任务未结束
    sample_file = _write_semantic_sample(task_id, drafts)
    computed_file = Path(f"/tmp/loom_task/{task_id}/.computed_passed.json")
    computed_file.write_text(
        json.dumps({
            "status": "computed_passed",
            "task_id": task_id,
            "mode": mode,
            "drafts_count": len(drafts),
            "drafts": [
                {
                    "id": d.id,
                    "mtime": (Path(f"/tmp/loom_task/{task_id}/drafts") / f"{d.id}.md").stat().st_mtime,
                }
                for d in drafts
            ],
            "semantic_sample": str(sample_file),
            "created_at": time.time(),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # L4 引用统计（005 §3.2）：THINK/USE 零引用 → WARN，DIGEST 不 WARN
    l4_warn = ""
    skill = plan.get("skill", "")
    if skill in ("THINK", "USE"):
        l4_refs_file = Path(f"/tmp/loom_task/{task_id}/.l4_refs")
        l4_refs = []
        if l4_refs_file.exists():
            try:
                l4_refs = json.loads(l4_refs_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                pass
        if not l4_refs:
            l4_warn = (
                f"\n[WARN] {skill} 任务全程零 L4 引用——"
                f"思考时未用上 Loom 沉淀的元层模式。可能原因：L4 索引质量不够 / "
                f"orient 后未按需 read-cards L4 / 本次思考确实不需要 L4。"
            )
        else:
            l4_warn = f"\n[L4 引用统计] 本次引用 {len(l4_refs)} 张 L4 卡: {l4_refs}"

    # THINK 任务：语义自检另有三条必答项（005 §6.3），与四判据同属强制确认清单
    think_warn = ""
    if think_state.load(task_id) is not None:
        think_warn = (
            f"\n[THINK 语义必答] 除四判据外还须逐条确认："
            f"a) think-skip 的理由都建立在真实 skim/深读上且成立（空泛或凭标题跳过 → 回去补读）；"
            f"b) 深挖对象确为全书最有外联价值的 L2——对照 loom think-coverage {task_id} 的清单自问；"
            f"c) 每张 --from-topics 产出忠实承接了声称覆盖的主题（只沾边 → 去掉 from-topics 或充实内容）。"
            f"任一不成立 → 修正后重新 mark-ready。"
        )

    rejected_file = Path(f"/tmp/loom_task/{task_id}/.rejected.json")
    rejected_file.write_text(
        json.dumps({
            "status": "semantic_required",
            "mode": mode,
            "failures": [{
                "card": "(semantic_sample)",
                "check": "semantic_quality_required",
                "reason": (
                    f"[mode={mode}] 计算层已通过；请读取 {sample_file}，按 type_match / "
                    f"single_unit / genuine_digest / self_contained 做语义质检。"
                    f"若失败，修 draft 后重新 mark-ready；若通过，调用 "
                    f"loom commit-ready {task_id} --semantic-passed。"
                    f"{l4_warn}"
                    f"{think_warn}"
                ),
            }],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 2



def cmd_commit_ready(args) -> int:
    """Commit drafts after current agent has completed semantic quality check."""
    task_id = args.task_id
    if not args.semantic_passed:
        return _err("commit-ready requires --semantic-passed after semantic quality check")

    task_dir = Path(f"/tmp/loom_task/{task_id}")
    computed_file = task_dir / ".computed_passed.json"
    if not computed_file.exists():
        return _err(
            f"task {task_id} has no computed_passed state; run mark-ready and stop-check first"
        )

    computed = json.loads(computed_file.read_text(encoding="utf-8"))
    expected = computed.get("drafts", [])
    current_drafts = checks.list_drafts(task_id)
    current_by_id = {
        d.id: (task_dir / "drafts" / f"{d.id}.md").stat().st_mtime
        for d in current_drafts
    }
    for item in expected:
        if current_by_id.get(item.get("id")) != item.get("mtime"):
            return _err(
                f"draft {item.get('id')} changed after computational checks; re-run mark-ready"
            )
    if len(current_drafts) != len(expected):
        return _err("draft set changed after computational checks; re-run mark-ready")

    with store.connect() as conn:
        row = conn.execute(
            "SELECT status, drafts_count, committed_count FROM task_trace WHERE task_id=?",
            (task_id,),
        ).fetchone()
    if row and row["status"] == "done":
        _print_json({
            "status": "already_done",
            "task_id": task_id,
            "drafts_count": row["drafts_count"],
            "committed_count": row["committed_count"],
        })
        return 0

    drafts = current_drafts
    if not drafts:
        return _err(f"no drafts found for task {task_id}")

    # 批量插入成功后、task_trace 落账前若进程被打断，重试不应因唯一键冲突而卡死。
    # 仅在当前 drafts 与库中同 ID 卡片（含 links）完全一致时，恢复任务状态。
    committed_ids = []
    for draft in drafts:
        card = store.get_card(draft.id)
        if card is None or any(
            card[field] != getattr(draft, field)
            for field in ("title", "type", "content", "source", "layer", "origin")
        ) or sorted(store.get_links(draft.id)) != sorted(draft.links):
            break
        committed_ids.append(draft.id)
    else:
        store.end_task(task_id, "done", drafts_count=len(drafts),
                       committed_count=len(committed_ids), committed_ids=committed_ids)
        computed_file.unlink(missing_ok=True)
        (task_dir / ".ready").unlink(missing_ok=True)
        _print_json({
            "status": "recovered_already_committed",
            "task_id": task_id,
            "committed": committed_ids,
        })
        return 0

    commit_result = _commit_drafts(task_id, drafts)
    _print_json(commit_result)
    if commit_result.get("status") == "rejected":
        rejected_file = task_dir / ".rejected.json"
        rejected_file.write_text(
            json.dumps({
                "status": "rejected",
                "mode": "commit_ready",
                "failures": [{
                    "card": "(commit)",
                    "check": "commit_unique",
                    "reason": commit_result["reason"],
                }],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 2

    computed_file.unlink(missing_ok=True)
    (task_dir / ".ready").unlink(missing_ok=True)
    return 0


def _commit_drafts(task_id: str, drafts: list[checks.CardDraft]) -> dict[str, Any]:
    """embed + 批量入库（单事务，冲突整批 rollback）。

    入库逻辑封装在 store.insert_cards_batch；本函数只负责 embed + 组装 card
    dict + 处理结果（end_task 或返回 rejected 详情）。
    """
    texts = [f"{d.title}\n{d.content}" for d in drafts]
    try:
        embeddings = embed.embed_batch(texts)
    except Exception as e:
        sys.stderr.write(f"warning: embedding failed: {e}\n")
        embeddings = [None] * len(drafts)

    cards = [
        {
            "id": d.id, "title": d.title, "type": d.type,
            "content": d.content, "source": d.source, "layer": d.layer,
            "origin": d.origin,
            "links": d.links,
        }
        for d in drafts
    ]
    result = store.insert_cards_batch(cards, embeddings)

    if result["status"] == "rejected":
        conflicts = result["conflicts"]
        return {
            "status": "rejected",
            "task_id": task_id,
            "conflicts": conflicts,
            "reason": (
                f"commit 阶段 {len(conflicts)} 个 card_id 撞 UNIQUE——"
                f"并发写入冲突。冲突 ID（需改名后重新 mark-ready）: {conflicts[:10]}"
            ),
        }

    committed = result["committed"]
    store.end_task(task_id, "done", drafts_count=len(drafts),
                   committed_count=len(committed), committed_ids=committed)
    return {
        "status": "committed",
        "task_id": task_id,
        "committed": committed,
    }


def cmd_mark_ready(args) -> int:
    """子 agent 在所有 drafts 写完后调，标记 task ready 让 stop-check 跑校验。

    替代 /tmp/loom_current_task 单文件机制——支持多 task 并发。
    子 agent 流程末尾必须调 mark-ready，否则 stop-check 扫描不到，drafts 不会 commit。

    session_id：从 env LOOM_SESSION_ID 读（主 agent 派发 Task 前 export）；
    写入 .session_id 文件 + task_trace.session_id 列。
    Claude hook 或手动 stop-check-pending 按此过滤——只处理当前 session 的 task，
    避免 batch agent 模式下扫到别的 session 的 task 写出无人接收的 block 信号。
    """
    task_id = args.task_id
    task_dir = Path(f"/tmp/loom_task/{task_id}")
    if not task_dir.exists():
        return _err(f"task dir not found: {task_dir}")
    drafts = checks.list_drafts(task_id)
    if not drafts:
        return _err(f"no drafts in task {task_id}——mark-ready 需在有 drafts 后调")

    session_id = _current_session_id()
    ready_file = task_dir / ".ready"
    ready_file.write_text(str(len(drafts)), encoding="utf-8")
    (task_dir / ".session_id").write_text(session_id, encoding="utf-8")

    # 镜像到 task_trace.session_id（salvage-pending 用 SQL 查询超时 task）
    plan = checks.load_plan(task_id) or {}
    if not plan.get("task_id"):
        plan = {"task_id": task_id, **plan}
    store.start_task(task_id, plan, session_id=session_id)

    _print_json({
        "status": "ready", "task_id": task_id,
        "drafts_count": len(drafts),
        "session_id": session_id or "(unset)",
    })
    return 0


def _aggregate_semantic_samples(
    task_root: Path, processed: list[dict[str, Any]]
) -> None:
    """Collect per-task .semantic_sample.json files, write one combined file."""
    import json as _j
    all_cards: list[dict[str, Any]] = []
    all_sample_ids: list[str] = []
    for entry in sorted(task_root.iterdir()):
        if not entry.is_dir():
            continue
        sf = entry / ".semantic_sample.json"
        if sf.exists():
            try:
                data = _j.loads(sf.read_text(encoding="utf-8"))
                all_sample_ids.extend(data.get("sample_ids", []))
                all_cards.extend(data.get("cards", []))
            except (_j.JSONDecodeError, ValueError):
                continue
    if all_cards:
        combined = Path("/tmp/loom_task/.semantic_sample.json")
        combined.write_text(
            _j.dumps(
                {"sample_ids": all_sample_ids, "cards": all_cards},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def cmd_stop_check_pending(args) -> int:
    """[特权] 收尾入口：扫描所有 .ready 但未 done 的 task，挨个跑 stop-check。

    替代单文件 /tmp/loom_current_task 机制——支持多 task 并发。
    每个 task 独立目录，互不干扰；已 done 的跳过（幂等）。

    **session_id 过滤（防 batch agent 模式信号丢失）**：
    - hook 模式从 stdin JSON 读 session_id（Claude/Codex hook 协议字段）
    - 命令行手动模式从 env LOOM_SESSION_ID 或 --current-session 读
    - 只跳过 `.session_id` 明确属于其他 session 的 task；旧/空 session task 继续处理（兼容未设置 LOOM_SESSION_ID 的调用）
    - `--all-sessions` 跳过过滤（手动维护用）

    关键：当任何 task 被拒时，输出 {"decision": "block", "reason": "..."}——
    Claude/Codex hook 识别此格式后，reason 会作为新消息注入回 agent，
    让它在退出前修复问题（而不是静默失败留烂摊子给主调度 agent）。
    """
    task_root = Path("/tmp/loom_task")
    if not task_root.exists():
        _print_json({"status": "no_task_dir", "checked": 0,
                     "processed_count": 0, "skipped_done": 0})
        return 0

    all_sessions = getattr(args, "all_sessions", False)
    current_session = ""
    if not all_sessions:
        current_session = getattr(args, "current_session", "") or _hook_session_id()

    checked = 0
    skipped_session = []
    processed = []
    skipped_done = []
    rejected_tasks: list[dict] = []  # 收集所有被拒 task 的失败原因

    for entry in sorted(task_root.iterdir()):
        if not entry.is_dir():
            continue
        tid = entry.name
        ready_file = entry / ".ready"
        if not ready_file.exists():
            continue

        # session_id 过滤：跳过非当前 session 的 task
        if not all_sessions and current_session:
            sid_file = entry / ".session_id"
            task_sid = sid_file.read_text(encoding="utf-8").strip() if sid_file.exists() else ""
            # 旧 task / 未设置 LOOM_SESSION_ID 的 task 没有 session_id，继续处理以保持兼容；
            # 只有明确属于别的 session 的 task 才跳过。
            if task_sid and task_sid != current_session:
                skipped_session.append({"task_id": tid, "session_id": task_sid or "(unset)"})
                continue

        checked += 1

        # 已 done 的跳过
        with store.connect() as conn:
            row = conn.execute(
                "SELECT status FROM task_trace WHERE task_id=?", (tid,)
            ).fetchone()
        if row and row["status"] == "done":
            skipped_done.append(tid)
            continue

        # 跑 stop-check（构造 Namespace）；rejected 详情从 .rejected.json 读
        import argparse as _ap
        ns = _ap.Namespace(task_id=tid, mode="normal")
        try:
            result_code = cmd_stop_check(ns)
            processed.append({"task_id": tid, "exit_code": result_code})
            if result_code == 2:
                rejected_file = entry / ".rejected.json"
                detail = (
                    rejected_file.read_text(encoding="utf-8").strip()
                    if rejected_file.exists() else ""
                )
                rejected_tasks.append({"task_id": tid, "detail": detail})
        except Exception as e:
            processed.append({"task_id": tid, "error": str(e)})
            rejected_tasks.append({"task_id": tid, "detail": f"exception: {e}"})

    _aggregate_semantic_samples(task_root, processed)

    # 若有 rejection，输出 decision:block 让子 agent 自修复
    if rejected_tasks:
        # 聚合原因：每个 task 一段，截取关键 failures
        lines = ["[Loom stop-check] 以下 task 校验未通过，需修复后重新 mark-ready："]
        for r in rejected_tasks:
            lines.append(f"\n--- task: {r['task_id']} ---")
            # detail 是 .rejected.json 的 JSON，尝试解析提取 failures
            try:
                payload = json.loads(r["detail"])
                for f in payload.get("failures", []):
                    card = f.get("card", "(?)")
                    check = f.get("check", "?")
                    reason = f.get("reason", "?")
                    lines.append(f"  [{check}] {card}: {reason}")
            except (json.JSONDecodeError, ValueError):
                # 非 JSON，直接附原始文本（截断）
                lines.append(f"  {r['detail'][:500]}")
        reason = "\n".join(lines)
        _print_json({
            "decision": "block",
            "reason": reason,
            "rejected_tasks": [r["task_id"] for r in rejected_tasks],
            "current_session": current_session or "(unset)",
            "processed_count": len(processed),
            "skipped_done_count": len(skipped_done),
            "skipped_session_count": len(skipped_session),
        })
        return 0  # exit 0 + decision:block，Claude Code 会 block；Codex 需主动处理

    _print_json({
        "status": "scanned",
        "checked": checked,
        "current_session": current_session or "(unset)",
        "processed_count": len(processed),
        "processed": processed,
        "skipped_done": skipped_done,
        "skipped_session": skipped_session,
    })
    return 0


def cmd_salvage_pending(args) -> int:
    """[特权] batch agent 收尾工具：扫描所有 computed_passed/ready 但超时未 commit 的 task，
    列出来供主 agent 整体语义复检。

    **触发场景**：batch agent 模式下，子 agent 退出时 stop-check 写了 computed_passed，
    但 block 信号无人接收 → task 卡在 computed_passed 永久 running。
    主 agent 在批量处理完成后调用此命令统一打捞。

    --timeout-min N：默认 30 分钟。task started_at 早于 now()-N*60 视为卡死。
    --run-stop-check：对 stale task 跑 salvage 模式 stop-check，仍需语义自检后 commit-ready。
    --auto-commit：旧兼容别名；不会自动入库。

    默认（不带 --run-stop-check）：只输出待打捞清单 + 给主 agent 的复检提示，
    不实际 commit（避免绕过语义层）。
    """
    timeout_min = getattr(args, "timeout_min", 30)
    run_stop_check = getattr(args, "run_stop_check", False)
    now_ts = time.time()
    cutoff = now_ts - timeout_min * 60

    task_root = Path("/tmp/loom_task")
    if not task_root.exists():
        _print_json({"status": "no_task_dir", "stale_count": 0})
        return 0

    stale: list[dict] = []
    with store.connect() as conn:
        for entry in sorted(task_root.iterdir()):
            if not entry.is_dir():
                continue
            tid = entry.name
            ready_file = entry / ".ready"
            if not ready_file.exists():
                continue
            row = conn.execute(
                "SELECT status, started_at, ended_at, drafts_count FROM task_trace WHERE task_id=?",
                (tid,),
            ).fetchone()
            if not row:
                continue
            if row["status"] == "done":
                continue
            started = row["started_at"] or 0
            # 超时判定：started_at 早于 cutoff 且未 done
            if started > cutoff:
                continue
            stale.append({
                "task_id": tid,
                "status": row["status"],
                "started_at": started,
                "age_min": round((now_ts - started) / 60, 1),
                "drafts_count": row["drafts_count"],
                "has_computed_passed": (entry / ".computed_passed.json").exists(),
            })

    if not stale:
        _print_json({"status": "no_stale", "stale_count": 0, "timeout_min": timeout_min})
        return 0

    # 默认仅报告；--run-stop-check 才实际跑 salvage 模式 stop-check，仍不自动入库。
    if run_stop_check:
        checked = []
        for s in stale:
            import argparse as _ap
            ns = _ap.Namespace(task_id=s["task_id"], mode="salvage")
            try:
                # salvage 模式：走完整 stop-check（含语义 block-back），但 reason 标记 salvage 上下文
                # 主 agent 看到 salvage 标记后应主动调 commit-ready --semantic-passed
                rc = cmd_stop_check(ns)
                checked.append({"task_id": s["task_id"], "exit_code": rc})
            except Exception as e:
                checked.append({"task_id": s["task_id"], "error": str(e)})
        _print_json({
            "status": "stop_check_ran",
            "stale_count": len(stale),
            "timeout_min": timeout_min,
            "checked": checked,
            "note": "已用 salvage 模式跑 stop-check；主 agent 仍需做语义质检后调 commit-ready --semantic-passed",
        })
        return 0

    _print_json({
        "status": "stale_list",
        "stale_count": len(stale),
        "timeout_min": timeout_min,
        "stale_tasks": stale,
        "next_step": (
            "对这些 task 做语义复检（type_match / single_unit / genuine_digest / self_contained）；"
            "通过则调 `loom-admin stop-check <task_id> --mode=salvage` 跑计算层 + 写 sample；"
            "再调 `loom commit-ready <task_id> --semantic-passed` 入库。"
            "或重跑 `loom-admin salvage-pending --run-stop-check`（仍走 salvage stop-check，不绕过语义层）。"
        ),
    })
    return 0


# ---------------------------------------------------------------------------
# arg parsing / dispatch
# ---------------------------------------------------------------------------

def _build_parser(entrypoint: str = "loom") -> argparse.ArgumentParser:
    is_admin = entrypoint == "loom-admin"
    p = argparse.ArgumentParser(prog=entrypoint, description="Loom CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    if is_admin:
        sp = sub.add_parser("commit-l4", help="[特权] L4 提案入库")
        sp.add_argument("proposal")
        sp.set_defaults(func=cmd_commit_l4)

        sp = sub.add_parser("apply-card-edit", help="[特权] 已有卡更新入库（全量替换 content）")
        sp.add_argument("proposal")
        sp.set_defaults(func=cmd_apply_card_edit)

        sp = sub.add_parser("rebuild-l4-index", help="[特权] 重建 L4 索引")
        sp.set_defaults(func=cmd_rebuild_l4_index)

        sp = sub.add_parser("rebuild-tag-index", help="[特权] 从 cards.tags 重建 tag 派生索引")
        sp.set_defaults(func=cmd_rebuild_tag_index)

        sp = sub.add_parser("rebuild-embeddings", help="[特权] 用当前 embedding provider 重建向量索引")
        sp.add_argument("--batch-size", type=int, default=32)
        sp.set_defaults(func=cmd_rebuild_embeddings)

        sp = sub.add_parser("tag-card", help="[特权] 人类明确维护单卡 tag（增量 add/remove）")
        sp.add_argument("card_id")
        sp.add_argument("--add", help='JSON 字符串数组，如 ["安全边际"]')
        sp.add_argument("--remove", help='JSON 字符串数组，如 ["旧tag"]')
        sp.set_defaults(func=cmd_tag_card)

        sp = sub.add_parser("update-card", help="[特权] 更新卡片字段（跑校验 + 重新 embed）")
        sp.add_argument("card_id")
        sp.add_argument("--title")
        sp.add_argument("--type")
        sp.add_argument("--source")
        sp.add_argument("--origin", choices=["ai", "human"])
        sp.add_argument("--links")
        sp.add_argument("--content")
        sp.add_argument("--content-file")
        sp.set_defaults(func=cmd_update_card)

        sp = sub.add_parser("delete-card", help="[特权] 删除卡片（cascade）")
        sp.add_argument("card_id")
        sp.set_defaults(func=cmd_delete_card)

        sp = sub.add_parser("stop-check-pending", help="[特权] 收尾入口：扫描所有 .ready 但未 done 的 task")
        sp.add_argument("--current-session", default="", help="当前 session_id（默认从 stdin JSON 或 env LOOM_SESSION_ID 读）")
        sp.add_argument("--all-sessions", action="store_true", help="跳过 session_id 过滤（手动维护用）")
        sp.set_defaults(func=cmd_stop_check_pending)

        sp = sub.add_parser("salvage-pending", help="[特权] 打捞 computed_passed/ready 超时未 commit 的 task")
        sp.add_argument("--timeout-min", type=int, default=30, help="task started_at 早于 now()-N*60 视为卡死（默认 30）")
        sp.add_argument("--run-stop-check", "--auto-commit", dest="run_stop_check", action="store_true", help="对卡死 task 跑 salvage 模式 stop-check（仍写 sample，主 agent 仍需语义自检后 commit-ready；--auto-commit 为旧别名，不会自动入库）")
        sp.set_defaults(func=cmd_salvage_pending)

        sp = sub.add_parser("stop-check", help="[特权] 单 task stop-check 入口（手动触发用）")
        sp.add_argument("task_id")
        sp.add_argument("--mode", choices=["normal", "salvage"], default="normal")
        sp.set_defaults(func=cmd_stop_check)

        sp = sub.add_parser(
            "proposal-decision",
            help="[特权] 封装 staging JSON 的 status 流转（主 agent 用户审核后调用）",
        )
        sp.add_argument("proposal", help="staging JSON 路径")
        sp.add_argument("--decision", choices=["approved", "rejected"], required=True)
        sp.add_argument("--reason", help="决策理由（写入 JSON decision_reason 字段）")
        sp.set_defaults(func=cmd_proposal_decision)

        return p

    # tui（终端卡片阅读器）
    sp = sub.add_parser("tui", help="终端卡片阅读器（TUI）")
    sp.set_defaults(func=cmd_tui)

    # import-source（L1 source card 创建入口，§008-24）
    sp = sub.add_parser("import-source", help="注册 L1 source card（layer=L1,type=source）")
    sp.add_argument("source_id", help="L1 source card id，如 llm:harness:src:08")
    sp.add_argument("--title", required=True)
    sp.add_argument("--path", required=True, help="原始 markdown 路径（相对项目根或绝对）")
    sp.set_defaults(func=cmd_import_source)

    # read-source
    sp = sub.add_parser("read-source", help="读 L1 source card 全文（id 或 markdown 路径）")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_read_source)

    # read-cards (支持单/多)
    sp = sub.add_parser("read-cards", help="读一张或多张卡（bump use_count + 记录 read trace / L4 引用）")
    sp.add_argument("ids", nargs="+")
    sp.add_argument("--task-id", help="记录 read trace / L4 引用归属的 task（也可用 LOOM_TASK_ID 环境变量）")
    sp.set_defaults(func=cmd_read_cards)

    # read-l4-index（思考中重读 L4 的轻量入口）
    sp = sub.add_parser("read-l4-index", help="周期性重读 L4 索引（轻量）")
    sp.set_defaults(func=cmd_read_l4_index)

    # search
    sp = sub.add_parser("search", help="检索")
    sp.add_argument("query")
    sp.add_argument("--mode", choices=["hybrid", "fts", "vector"], default="hybrid")
    sp.add_argument("--top", type=int, default=10)
    sp.add_argument("--ns")
    sp.add_argument("--type")
    sp.add_argument("--tag", help="逗号分隔；多个 tag 按 AND 过滤")
    sp.set_defaults(func=cmd_search)

    # structure traversal
    sp = sub.add_parser("browse", help="浏览 namespace")
    sp.add_argument("namespace")
    sp.add_argument("prefix", nargs="?", default="")
    sp.add_argument("--tag", help="逗号分隔；多个 tag 按 AND 过滤")
    sp.set_defaults(func=cmd_browse)

    sp = sub.add_parser("children", help="子卡")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_children)

    sp = sub.add_parser("siblings", help="兄弟卡")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_siblings)

    sp = sub.add_parser("neighbors", help="link 图遍历")
    sp.add_argument("id")
    sp.add_argument("--depth", type=int, default=1)
    sp.set_defaults(func=cmd_neighbors)

    sp = sub.add_parser("stats", help="统计")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("namespaces", help="列出 namespace")
    sp.set_defaults(func=cmd_namespaces)

    # exploration tools
    sp = sub.add_parser("orient", help="启动时定位——读立体目录（L4 + namespace 全貌）")
    sp.set_defaults(func=cmd_orient)

    sp = sub.add_parser("skim", help="轻量浏览一张卡（不 bump use_count）")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_skim)

    sp = sub.add_parser("wander", help="link 图随机游走")
    sp.add_argument("id")
    sp.add_argument("--steps", type=int, default=3)
    sp.set_defaults(func=cmd_wander)

    sp = sub.add_parser("suggest-links", help="找未 link 的语义近邻（辅助补 link）")
    sp.add_argument("id")
    sp.add_argument("--top", type=int, default=10)
    sp.set_defaults(func=cmd_suggest_links)

    sp = sub.add_parser("browse-tree", help="namespace 主题树（namespace → 主题卡 → 子卡）")
    sp.add_argument("namespace")
    sp.set_defaults(func=cmd_browse_tree)

    # write-draft
    sp = sub.add_parser("write-draft", help="写 draft（内联计算校验）")
    sp.add_argument("task_id")
    sp.add_argument("card_id")
    sp.add_argument("--type", required=True)
    sp.add_argument("--title")
    sp.add_argument("--source")
    sp.add_argument("--layer")
    sp.add_argument("--origin", choices=["ai", "human"], default="ai")
    sp.add_argument("--links")
    sp.add_argument("--content")
    sp.add_argument("--content-file")
    sp.add_argument("--from-topics", help="THINK 任务：声明承接的主题卡 id（逗号分隔）；L3 draft 必填")
    sp.set_defaults(func=cmd_write_draft)

    # THINK 覆盖账（005 §4.1）
    sp = sub.add_parser("think-init", help="单书 THINK：从该书主题卡生成覆盖清单")
    sp.add_argument("task_id")
    sp.add_argument("--goal", required=True)
    sp.add_argument("--materials", required=True, help="<domain>:<book>[,<domain>:<book>...]")
    sp.set_defaults(func=cmd_think_init)

    sp = sub.add_parser("think-skip", help="对明确不深挖的主题记具体理由")
    sp.add_argument("task_id")
    sp.add_argument("topic_id")
    sp.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_think_skip)

    sp = sub.add_parser("think-coverage", help="查看覆盖账：各主题 → 承接产出 / skip 理由 / 未结")
    sp.add_argument("task_id")
    sp.set_defaults(func=cmd_think_coverage)

    # proposals
    sp = sub.add_parser("propose-l4", help="L4 新模式提案（agent 显式指定 gen:<卢曼ID>）")
    sp.add_argument("task_id")
    sp.add_argument("card_id", help="gen:<卢曼ID>，卢曼树形 ID（如 gen:1a / gen:1a1）—— 新顶级模式用 Na，已有模式 Xa 的深化用 XaY")
    sp.add_argument("--title", required=True)
    sp.add_argument("--content")
    sp.add_argument("--content-file")
    sp.add_argument("--related")
    sp.add_argument("--from-topics", help="THINK 任务：声明承接的主题卡 id（逗号分隔）")
    sp.add_argument("--type", default="模式",
                    choices=["模式", "判断", "反思"],
                    help="L4 type（默认模式）")
    sp.set_defaults(func=cmd_propose_l4)

    sp = sub.add_parser("propose-card-edit", help="已有卡更新提案（L2/L3/L4 通用）")
    sp.add_argument("task_id")
    sp.add_argument("card_id")
    sp.add_argument("--title")
    sp.add_argument("--content")
    sp.add_argument("--content-file")
    sp.add_argument("--related", help="审核通过后追加的 links，逗号分隔；提案时会随完整新版一起校验")
    sp.add_argument("--from-topics", help="THINK 任务：可选，声明承接的主题卡 id")
    sp.add_argument("--type", default="修正",
                    choices=["修正", "补充", "重写", "更新"])
    sp.set_defaults(func=cmd_propose_card_edit)

    sp = sub.add_parser("l4-upgrade-candidates", help="L4 升级候选（纯 SQL）")
    sp.add_argument("--use-count", type=int, default=10)
    sp.add_argument("--reflections", type=int, default=2)
    sp.add_argument("--domains", type=int, default=2)
    sp.set_defaults(func=cmd_l4_upgrade_candidates)

    sp = sub.add_parser("silent-cards", help="沉默卡（use_count=0 AND search_count=0）")
    sp.add_argument("--min-age-days", type=int, default=0,
                    help="最小入库天数（过滤刚入库的卡）")
    sp.set_defaults(func=cmd_silent_cards)

    sp = sub.add_parser("commit-ready", help="语义质检通过后提交 ready drafts")
    sp.add_argument("task_id")
    sp.add_argument("--semantic-passed", action="store_true")
    sp.set_defaults(func=cmd_commit_ready)

    sp = sub.add_parser("mark-ready", help="标记 task drafts 完成（子 agent 流程末尾调）")
    sp.add_argument("task_id")
    sp.set_defaults(func=cmd_mark_ready)

    # session activation / hooks guard
    sp = sub.add_parser("on", help="在当前项目激活 Loom hooks")
    sp.set_defaults(func=cmd_activate)

    sp = sub.add_parser("off", help="在当前项目关闭 Loom hooks")
    sp.set_defaults(func=cmd_deactivate)

    sp = sub.add_parser("hook-guard", help="[hook] 若当前项目未激活则退出 1")
    sp.add_argument("--hook", action="store_true", help="输出 JSON continue 信号供 hook 框架读取")
    sp.set_defaults(func=cmd_hook_guard)

    return p


def main(argv: list[str], entrypoint: str = "loom") -> int:
    parser = _build_parser(entrypoint)
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _err(str(e), code=3)
