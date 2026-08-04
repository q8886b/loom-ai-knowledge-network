"""阅读器——默认界面：连续翻阅一本书的卡，像浏览器里滚动网页。

窄屏优先三栏布局：
  左：书目录（当前书全部卡，占满列高，可滚动）
  右上：关联横条（来源L1/子卡/邻居，折行显示）
  右下：内容（markdown 主体）

键盘：↑/↓ 前后翻卡（书顺序，跳过 L1），←/→ 历史前进后退，
Tab 在 目录/关联/内容 间切焦点。
"""
from __future__ import annotations

import bisect

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Label, ListItem, ListView, Markdown, Static

from . import queries as q
from .books import BooksScreen
from .notes_screen import NotesScreen
from .pops import ExpandPop, NotePop
from .tree_screen import TreeScreen

LAYER_COLORS = {"L1": "grey62", "L2": "#6366f1", "L3": "#10b981", "L4": "#f59e0b"}
L1_TEXT_LIMIT = 500_000  # L1 原文超此长度用纯文本渲染，防 markdown 卡顿


class ReaderScreen(Screen):
    BINDINGS = [
        # 翻卡/历史为"焦点感知"：焦点在内容区时生效；焦点在目录/关联列表时
        # ↑↓ 由列表消费（选列表项），←→ 仍全局生效（列表不消费左右键）
        Binding("up", "prev", "上一张"),
        Binding("down", "next", "下一张"),
        Binding("n", "next", "下一张"),
        Binding("p", "prev", "上一张"),
        Binding("left", "back", "后退"),
        Binding("right", "forward", "前进"),
        Binding("space", "page_down", "翻页"),
        Binding("tab", "toggle_panel", "目录/关联/内容"),
        Binding("e", "expand", "扩展"),
        Binding("a", "note", "批注"),
        Binding("[", "back", "后退"),
        Binding("]", "forward", "前进"),
        Binding("t", "tree", "树"),
        Binding("m", "notes", "批注列表"),
        Binding("b", "books", "书"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.card_id = ""
        self.card: dict = {}
        self._mounted = False
        self._pending: dict | None = None
        self._link_items: list[str | None] = []
        self._toc_items: list[tuple[str, dict]] = []
        self._toc_key = ""  # 目录缓存键（上下文变化才重建，翻卡只移动光标）
        self._read_ids: set[str] = set()

    def compose(self):
        yield Static("", id="reader-header")
        with Horizontal(id="reader-main"):
            yield ListView(id="reader-toc")
            with Vertical(id="reader-right"):
                yield ListView(id="reader-links")
                yield Markdown("", id="reader-body")

    def on_mount(self) -> None:
        self._mounted = True
        if self._pending:
            self._apply_card(self._pending["id"], self._pending)
        # 默认无焦点（内容区不可聚焦）：↑↓ 翻卡由 Screen 绑定接管；
        # Tab 聚焦到目录/关联列表后 ↑↓ 选列表项

    def set_card(self, card_id: str) -> None:
        card = q.get_card(card_id)
        if not card:
            card = {"id": card_id, "title": f"(卡不存在: {card_id})", "content": ""}
        if self._mounted:
            self._apply_card(card_id, card)
        else:
            # switch_screen 后新 screen 尚未 mount，先暂存
            self._pending = card

    def _apply_card(self, card_id: str, card: dict) -> None:
        self.card_id = card_id
        self.card = card
        body = self.query_one("#reader-body", Markdown)
        content = card.get("content") or ""
        if card.get("layer") == "L1" and len(content) > L1_TEXT_LIMIT:
            body.update("（L1 原文过长，已转纯文本）")
        else:
            body.update(content)
        body.scroll_home(animate=False)
        # 目录缓存：上下文未变只移动光标（避免每次翻卡 clear 重建 192 项导致闪烁）；
        # 关联面板内容随卡变，每次重建
        if self._toc_key != self.app.context_key:
            self.run_worker(self._build_toc())
            self._toc_key = self.app.context_key
        else:
            self._sync_toc()
        self.run_worker(self._build_links())
        self.refresh_header()
        # 仅书上下文记录阅读进度（L3/L4 不记书进度）
        if self.app.context_type == "book":
            q.mark_card_read(f"{self.app.ns}:{self.app.book}", card_id)
            # 新读的卡立即标 dim（目录已缓存，不重建）
            if card_id not in self._read_ids:
                self._read_ids.add(card_id)
                self._mark_toc_dim(card_id)

    def _mark_toc_dim(self, card_id: str) -> None:
        """把目录中指定卡标为已读（dim），局部更新样式不重建列表。"""
        toc = self.query_one("#reader-toc", ListView)
        idx = next((i for i, (cid, _) in enumerate(self._toc_items) if cid == card_id), None)
        if idx is None or idx >= len(toc.children):
            return
        item = toc.children[idx]
        label = next((w for w in item.children if isinstance(w, Label)), None)
        if label is None:
            return
        # 重建 Label 文本：序号 + 灰色标题（dim 效果）
        c = self._toc_items[idx][1]
        color = "dim"
        num = f"{idx + 1:>3} " if self._toc_items[idx][0].count(":") >= 2 else "    "
        text = Text(num, style="grey37")
        text.append(f"[{c.get('layer','')}] {c.get('title','')}", style=color)
        text.append(f"  {c['id']}", style="grey37")
        label.update(text)

    # ------------------------------------------------------------------
    # 左：书目录
    # ------------------------------------------------------------------
    async def _build_toc(self) -> None:
        """左目录：当前翻卡上下文全部卡（书内 L2 / 域 L3 / 全部 L4），占满列高。

        已读的卡用 dim（暗淡）标记，对应网页的半透明已读效果。
        """
        lv = self.query_one("#reader-toc", ListView)
        await lv.clear()
        self._toc_items = []
        app = self.app
        # 已读集合：书上下文用阅读进度；L3/L4 无进度
        self._read_ids = set()
        if app.context_type == "book":
            prog = q.get_reading_progress(f"{app.ns}:{app.book}")
            if prog and prog.get("read_ids"):
                import json as _json
                try:
                    self._read_ids = set(_json.loads(prog["read_ids"]))
                except Exception:
                    pass
        if app.context_type == "book":
            # 书上下文：L2 卡 + 补当前卡的来源 L1 原文入口
            src = self.card.get("source") or ""
            extra_l1: list[tuple[str, dict]] = []
            if self.card.get("layer") == "L2" and src:
                sc = q.get_card(src)
                if sc and sc.get("layer") == "L1":
                    extra_l1.append((src, sc))
            for cid, c in extra_l1:
                text = Text("    ", style="grey37")
                text.append(f"[L1] {c.get('title','')}", style=LAYER_COLORS["L1"])
                text.append(f"  {cid}", style="grey37")
                lv.append(ListItem(Label(text)))
                self._toc_items.append((cid, c))
            for i, c in enumerate(app.context_cards):
                self._add_toc_item(lv, c, index=i)
        else:
            # L3/L4 上下文：直接列集合内卡（带序号）
            for i, c in enumerate(app.context_cards):
                self._add_toc_item(lv, c, index=i)
        if lv.children:
            lv.index = 0
        # 高亮当前卡并滚动到它（翻卡时目录跟随）
        self._sync_toc()

    def _add_toc_item(self, lv: ListView, c: dict, index: int | None = None) -> None:
        color = LAYER_COLORS.get(c.get("layer", ""), "white")
        if c["id"] in self._read_ids:
            color = "dim"  # 已读：暗淡（对应 web 半透明）
        num = f"{index + 1:>3} " if index is not None else ""
        text = Text(num, style="grey37")
        text.append(f"[{c.get('layer','')}] {c.get('title','')}", style=color)
        text.append(f"  {c['id']}", style="grey37")
        lv.append(ListItem(Label(text)))
        self._toc_items.append((c["id"], c))

    # ------------------------------------------------------------------
    # 右上：关联横条（折行）
    # ------------------------------------------------------------------
    async def _build_links(self) -> None:
        """关联横条：来源 L1（L2 卡）→ 子卡 → link 邻居，折行显示。"""
        lv = self.query_one("#reader-links", ListView)
        await lv.clear()
        self._link_items: list[str | None] = []
        card = self.card

        seen: set[str] = set()
        src = card.get("source") or ""
        if card.get("layer") == "L2" and src:
            src_card = q.get_card(src)
            if src_card and src_card.get("layer") == "L1":
                self._add_link_group(lv, "来源 L1 原文", [(src, src_card)])
                seen.add(src)
        child_cards = []
        for cid in q.children(self.card_id):
            if cid in seen:
                continue
            c = q.get_card(cid)
            if c:
                child_cards.append((cid, c))
                seen.add(cid)
        if child_cards:
            self._add_link_group(lv, "子卡", child_cards)
        neighbor_cards = []
        for nid in q.neighbors(self.card_id, 1):
            if nid == self.card_id or nid in seen:
                continue
            c = q.get_card(nid)
            if c:
                neighbor_cards.append((nid, c))
                seen.add(nid)
        if neighbor_cards:
            self._add_link_group(lv, "关联", neighbor_cards)

        if not self._link_items:
            lv.append(ListItem(Label(Text("（无关联卡）", style="grey37"))))
        if lv.children:
            lv.index = 0

    def _add_link_group(self, lv: ListView, group_name: str, cards: list[tuple[str, dict]]) -> None:
        lv.append(ListItem(Label(Text(f"── {group_name} ──", style="bold grey62"))))
        self._link_items.append(None)
        start = len([x for x in self._link_items if x])
        for n, (cid, card) in enumerate(cards):
            color = LAYER_COLORS.get(card.get("layer", ""), "white")
            text = Text(f"{start + n + 1:>3} ", style="grey37")
            text.append(f"[{card.get('layer','')}] {card.get('title','')}", style=color)
            text.append(f"  {cid}", style="grey37")
            lv.append(ListItem(Label(text)))
            self._link_items.append(cid)

    # ------------------------------------------------------------------
    # 头部
    # ------------------------------------------------------------------
    def refresh_header(self) -> None:
        header = self.query_one("#reader-header", Static)
        card = self.card
        layer = card.get("layer", "")
        color = LAYER_COLORS.get(layer, "white")
        parts = [Text(f"[{layer}] ", style=color), Text(card.get("title", ""), style="bold")]
        meta = card.get("type", "")
        tags = card.get("tags") or []
        if tags:
            meta += " · " + " ".join(f"#{t}" for t in tags)
        ann = q.get_annotation(self.card_id)
        if ann and ann.get("note"):
            meta += " · [注]"
        if meta:
            parts.append(Text(f"  {meta}", style="grey62"))
        src = card.get("source") or ""
        if card.get("layer") == "L2" and src and q.get_card(src):
            parts.append(Text(f"  ↖ {src}", style="cyan"))
        idx = self.app.index_of(self.card_id)
        total = len(self.app.context_cards)
        if idx >= 0 and total:
            parts.append(Text(f"  {idx + 1}/{total}", style="grey62"))
        parts.append(Text(f"  {self.card_id}", style="grey37"))
        header.update(Text.assemble(*parts))

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def action_next(self) -> None:
        """↓ / n：翻到下一张，目录高亮与滚动同步。"""
        app = self.app
        cards = app.context_cards
        ids = app.context_ids
        if not cards:
            return
        idx = app.index_of(self.card_id)
        if idx >= 0:
            if idx >= len(cards) - 1:
                return
            target = cards[idx + 1]["id"]
        else:
            i = bisect.bisect_right(ids, self.card_id)
            if i >= len(ids):
                return
            target = ids[i]
        app.open_card(target)

    def action_prev(self) -> None:
        """↑ / p：翻到上一张，目录高亮与滚动同步。"""
        app = self.app
        cards = app.context_cards
        ids = app.context_ids
        if not cards:
            return
        idx = app.index_of(self.card_id)
        if idx >= 0:
            if idx <= 0:
                return
            target = cards[idx - 1]["id"]
        else:
            i = bisect.bisect_left(ids, self.card_id)
            if i <= 0:
                return
            target = ids[i - 1]
        app.open_card(target)

    def _sync_toc(self) -> None:
        """目录高亮与滚动跟随当前卡（局部更新，不重建列表）。"""
        toc = self.query_one("#reader-toc", ListView)
        idx = next((i for i, (cid, _) in enumerate(self._toc_items) if cid == self.card_id), None)
        if idx is not None and toc.children:
            toc.index = idx
            toc.scroll_to_widget(toc.children[idx], animate=False)

    def action_page_down(self) -> None:
        self.query_one("#reader-body", Markdown).scroll_page_down(animate=False)

    def action_toggle_panel(self) -> None:
        """Tab：目录 → 关联 → 无焦点 循环（内容区不可聚焦，滚轮直落其上）。"""
        toc = self.query_one("#reader-toc", ListView)
        links = self.query_one("#reader-links", ListView)
        if self.focused is toc:
            links.focus()
        elif self.focused is links:
            self.set_focus(None)  # 释放焦点，↑↓ 回到翻卡
        else:
            toc.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """目录/关联面板 Enter：跳转到选中卡（历史可回）。"""
        if event.item is None:
            return
        index = event.list_view.index
        if index is None:
            return
        if event.list_view.id == "reader-toc":
            if 0 <= index < len(self._toc_items):
                self.app.open_card(self._toc_items[index][0])
        elif event.list_view.id == "reader-links":
            if 0 <= index < len(self._link_items):
                cid = self._link_items[index]
                if cid:
                    self.app.open_card(cid)

    def action_expand(self) -> None:
        self.app.push_screen(ExpandPop(self.card_id))

    def action_note(self) -> None:
        self.app.push_screen(NotePop(self.card_id))

    def action_back(self) -> None:
        self.app.go_back()

    def action_forward(self) -> None:
        self.app.go_forward()

    def action_tree(self) -> None:
        self.app.push_screen(TreeScreen())

    def action_notes(self) -> None:
        self.app.push_screen(NotesScreen())

    def action_books(self) -> None:
        self.app.push_screen(BooksScreen())
