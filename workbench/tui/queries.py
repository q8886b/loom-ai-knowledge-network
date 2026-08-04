"""TUI 数据层——对 workbench.backend.main 查询函数的薄封装。

main.py 的 endpoint 函数默认参数是 FastAPI Query(...) 对象，直接调用会崩，
这里显式传全部参数。main.py 模块级副作用（init_db / app 创建）幂等安全。
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

# workbench 在 repo 根；loom 包在 repo/src。两处都进 path 才能 import。
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workbench.backend import main as wb  # noqa: E402

from loom import store  # noqa: E402

# 域英文简写 → 中文名（显示用）
DOMAIN_NAMES = {
    "llm": "LLM", "fin": "金融", "med": "医学", "law": "法律", "sw": "软件",
    "phil": "哲学", "prod": "产品", "fit": "健身", "psy": "心理学",
    "hist": "历史", "soc": "社会学", "sci": "科学", "gen": "元层",
}


def domain_display(ns: str) -> str:
    """域显示名：如 'fin' → '金融 (fin)'。"""
    cn = DOMAIN_NAMES.get(ns, "")
    return f"{cn} ({ns})" if cn else ns


def books(ns: str) -> list[dict[str, Any]]:
    return wb.books(ns)["books"]


def cards_by_ns(ns: str, tag: str | None = None, book: str | None = None) -> dict[str, Any]:
    return wb.cards_by_ns(ns, tag=tag, book=book)


def get_card(card_id: str) -> dict[str, Any] | None:
    try:
        return wb.get_card(card_id)
    except Exception:
        return None


def graph_expand(card_id: str) -> dict[str, Any]:
    return wb.graph_expand(card_id)


def graph_cluster(card_id: str, depth: int = 2) -> dict[str, Any]:
    return wb.graph_cluster(card_id, depth=depth)


def search(q: str, ns: str | None = None, tag: str | None = None, top: int = 30) -> list[dict[str, Any]]:
    return wb.search(q, ns=ns, tag=tag, top=top)["results"]


def tree(ns: str | None = None, tag: str | None = None) -> list[dict[str, Any]]:
    return wb.tree(ns=ns, tag=tag)["roots"]


def tags() -> list[dict[str, Any]]:
    return wb.tags()["tags"]


def children(card_id: str) -> list[str]:
    return store.get_children(card_id)


def l3_by_ns(ns: str) -> list[dict[str, Any]]:
    """某域的 L3 卡（ns:luhmann，无书段）。"""
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT id, title, type, layer, origin, tags FROM cards c
               WHERE c.id >= ? AND c.id < ? AND c.layer = 'L3'
               ORDER BY c.id""",
            (f"{ns}:", f"{ns};"),
        ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            r["tags"] = store.parse_tags_json(r.get("tags") or "[]")
        return out


def l4_all() -> list[dict[str, Any]]:
    """全部 L4 卡（gen 域）。"""
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT id, title, type, layer, origin, tags FROM cards c
               WHERE c.layer = 'L4' ORDER BY c.id"""
        ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            r["tags"] = store.parse_tags_json(r.get("tags") or "[]")
        return out


def recent_books(limit: int = 5) -> list[dict[str, Any]]:
    """最近读的书（按进度更新倒序），跨域。每项含 ns/book/name/read/total。"""
    progress = store.list_reading_progress()
    recent = sorted(progress, key=lambda p: p["updated_at"], reverse=True)[:limit * 3]
    out = []
    seen: set[str] = set()
    books_cache: dict[str, list[dict]] = {}

    def _books_of(ns: str) -> list[dict]:
        if ns not in books_cache:
            books_cache[ns] = wb.books(ns)["books"]
        return books_cache[ns]

    import json as _json
    for p in recent:
        scope = p["scope"]
        if ":" not in scope or scope in seen:
            continue
        seen.add(scope)
        ns, book = scope.split(":", 1)
        meta = next((b for b in _books_of(ns) if b["book"] == book), None)
        if not meta:
            continue
        read = len(_json.loads(p["read_ids"])) if p.get("read_ids") else 0
        out.append({
            "ns": ns, "book": book, "name": meta["name"],
            "read": read, "total": meta["count"],
        })
        if len(out) >= limit:
            break
    return out


def theme_roots(ns: str) -> list[dict[str, Any]]:
    """全量树的根：ns 范围内的 主题/结构 卡（graph_overview 的 trunk 逻辑）。"""
    with store.connect() as conn:
        rows = conn.execute(
            """SELECT id, title, type, layer, origin, tags FROM cards c
               WHERE c.id >= ? AND c.id < ? AND c.type IN ('主题', '结构')
               ORDER BY c.id""",
            (f"{ns}:", f"{ns};"),
        ).fetchall()
        out = [dict(r) for r in rows]
        for r in out:
            r["tags"] = store.parse_tags_json(r.get("tags") or "[]")
        return out


def neighbors(card_id: str, depth: int = 1) -> list[str]:
    return store.get_neighbors(card_id, depth)


def stats() -> dict[str, Any]:
    return wb.stats()


# ---------------------------------------------------------------------------
# store 直写：批注 / 进度 / 标签（TUI 是唯一写入口，agent 只读）
# ---------------------------------------------------------------------------

def set_annotation(card_id: str, note: str) -> dict[str, Any]:
    return store.set_annotation(card_id, note)


def get_annotation(card_id: str) -> dict[str, Any] | None:
    return store.get_annotation(card_id)


def list_annotations() -> list[dict[str, Any]]:
    return store.list_annotations()


def update_card_tags(card_id: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict[str, Any]:
    return store.update_card_tags(card_id, add=add or [], remove=remove or [])


def get_reading_progress(scope: str) -> dict[str, Any] | None:
    return store.get_reading_progress(scope)


def list_reading_progress(ns: str | None = None) -> list[dict[str, Any]]:
    return store.list_reading_progress(ns)


def mark_card_read(scope: str, card_id: str) -> dict[str, Any]:
    return store.mark_card_read(scope, card_id)
