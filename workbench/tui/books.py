"""书选择——首屏与域视图（双容器 + display 切换，避免异步 mount 竞态）。

首屏（domains）：最近阅读段（跨域最近读的书）+ 全部域列表。
域视图（domain）：左 = 域卡列表（普通域 L3，gen 域 L4）+ 右 = 书列表，
Tab 切左右焦点，Enter 打开。
"""
from __future__ import annotations

import json

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Label, ListItem, ListView, Static

from . import queries as q

LAYER_COLORS = {"L1": "grey62", "L2": "#6366f1", "L3": "#10b981", "L4": "#f59e0b"}


class BooksScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "返回"),
        Binding("tab", "toggle_panel", "左右切换"),
    ]

    def __init__(self, initial_view: str = "domains", initial_ns: str = "") -> None:
        super().__init__()
        self.view = "domains"  # domains | domain
        self._initial_view = initial_view
        self._initial_ns = initial_ns
        self.ns_list: list[str] = []
        self.ns_idx = 0
        self._recent_ns = ""
        self._ns_counts: dict[str, int] = {}
        self._rows: list[tuple | None] = []
        self._last_domain_row = 0
        self._last_book_row: dict[str, int] = {}
        self._domain_cards: list[dict] = []
        self._domain_books: list[dict] = []

    def compose(self):
        yield Static("", id="books-header")
        yield ListView(id="books-list")
        with Horizontal(id="domain-view"):
            yield ListView(id="domain-cards")
            yield ListView(id="domain-books")

    async def on_mount(self) -> None:
        progress = q.list_reading_progress()
        if progress:
            recent = sorted(progress, key=lambda p: p["updated_at"], reverse=True)[0]
            self._recent_ns = recent["scope"].split(":", 1)[0]
        stats = q.stats()
        self._ns_counts = stats.get("namespaces", {})
        self.ns_list = sorted(self._ns_counts.keys())
        if self._initial_view == "domain" and self._initial_ns in self.ns_list:
            await self._load_domain(self._initial_ns)
        else:
            await self._load_domains()

    # ------------------------------------------------------------------
    # 首屏：最近阅读 + 全部域
    # ------------------------------------------------------------------
    async def _load_domains(self) -> None:
        self.view = "domains"
        lv = self.query_one("#books-list", ListView)
        self.query_one("#books-list").display = True
        self.query_one("#domain-view").display = False
        await lv.clear()
        self._rows = []
        recent = q.recent_books(5)
        if recent:
            lv.append(ListItem(Label(Text("── 最近阅读 ──", style="bold cyan"))))
            self._rows.append(None)
            for b in recent:
                pct = f"{b['read'] * 100 // b['total']}%" if b["total"] else "—"
                text = Text(f"📖 {b['name']}", style="bold")
                text.append(f"  {b['read']}/{b['total']} {pct}", style="grey62")
                text.append(f"  {b['ns']}:{b['book']}", style="grey37")
                lv.append(ListItem(Label(text)))
                self._rows.append(("recent", b["ns"], b["book"]))
            lv.append(ListItem(Label(Text("", style="grey37"))))
            self._rows.append(None)
        lv.append(ListItem(Label(Text("── 全部域 ──", style="bold cyan"))))
        self._rows.append(None)
        for ns in self.ns_list:
            books_data = q.books(ns)
            progress = q.list_reading_progress(ns)
            read = sum(len(json.loads(p["read_ids"])) for p in progress if p.get("read_ids"))
            total = self._ns_counts.get(ns, 0)
            star = " [最近读]" if ns == self._recent_ns else ""
            pct = f"{read * 100 // total}%" if total else "—"
            text = Text(f"{q.domain_display(ns)}{star}", style="bold")
            text.append(f"  {total} 卡", style="grey62")
            text.append(f"  {len(books_data)} 本 · {pct}", style="grey37")
            lv.append(ListItem(Label(text)))
            self._rows.append(("domain", ns))
        header = self.query_one("#books-header", Static)
        header.update(
            Text.assemble(
                Text("Loom 入口 ", style="bold cyan"),
                Text("Enter 进入 · [最近读] = 上次读的域 · q 退出", style="grey62"),
            )
        )
        # 恢复上次停留（默认最近读的域）
        row = self._last_domain_row
        if row < 0 or row >= len(self._rows) or not self._rows[row]:
            row = next((i for i, r in enumerate(self._rows) if r and r[0] == "domain" and r[1] == self._recent_ns), 1)
        if lv.children:
            lv.index = max(1, min(row, len(self._rows) - 1))
        lv.focus()

    # ------------------------------------------------------------------
    # 域视图：左卡列表（L3，gen=L4）+ 右书列表
    # ------------------------------------------------------------------
    async def _load_domain(self, ns: str) -> None:
        self.view = "domain"
        self.ns_idx = self.ns_list.index(ns) if ns in self.ns_list else 0
        self.query_one("#books-list").display = False
        view = self.query_one("#domain-view", Horizontal)
        view.display = True
        left = self.query_one("#domain-cards", ListView)
        right = self.query_one("#domain-books", ListView)
        await left.clear()
        await right.clear()
        if ns == "gen":
            self._domain_cards = q.l4_all()
        else:
            self._domain_cards = q.l3_by_ns(ns)
        self._domain_books = q.books(ns)
        for i, c in enumerate(self._domain_cards):
            color = LAYER_COLORS.get(c["layer"], "white")
            text = Text(f"{i + 1:>3} ", style="grey37")
            text.append(f"[{c['layer']}] {c['title']}", style=color)
            text.append(f"  {c['id']}", style="grey37")
            left.append(ListItem(Label(text)))
        progress = {p["scope"]: p for p in q.list_reading_progress(ns)}
        for i, b in enumerate(self._domain_books):
            scope = f"{ns}:{b['book']}"
            p = progress.get(scope)
            read = len(json.loads(p["read_ids"])) if p and p["read_ids"] else 0
            pct = f"{read * 100 // b['count']}%" if b["count"] else "—"
            text = Text(f"{i + 1:>3} ", style="grey37")
            text.append(f"📖 {b['name']}", style="bold")
            text.append(f"  {read}/{b['count']} {pct}", style="grey62")
            right.append(ListItem(Label(text)))
        # 恢复该域上次停留的书行
        book_row = self._last_book_row.get(ns, 0)
        if right.children and book_row < len(right.children):
            right.index = book_row
        if left.children:
            left.index = 0
        right.focus()
        kind = "L4" if ns == "gen" else "L3"
        header = self.query_one("#books-header", Static)
        header.update(
            Text.assemble(
                Text(f"域 {q.domain_display(ns)}  ", style="bold cyan"),
                Text(f"({self.ns_idx + 1}/{len(self.ns_list)})  ", style="grey37"),
                Text(f"左 {kind} 卡 {len(self._domain_cards)} · 右 书 {len(self._domain_books)}", style="grey62"),
                Text("  Tab 左右 · Enter 打开 · Esc 返回", style="grey37"),
            )
        )

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item is None:
            return
        index = event.list_view.index
        if index is None:
            return
        if self.view == "domains":
            if index >= len(self._rows) or not self._rows[index]:
                return
            kind = self._rows[index][0]
            if kind == "recent":
                _, ns, book = self._rows[index]
                self.app.open_book(ns, book, back_view="domains")
            elif kind == "domain":
                _, ns = self._rows[index]
                self._last_domain_row = index
                await self._load_domain(ns)
        elif self.view == "domain":
            ns = self.ns_list[self.ns_idx]
            if event.list_view.id == "domain-cards":
                if 0 <= index < len(self._domain_cards):
                    cid = self._domain_cards[index]["id"]
                    if ns == "gen":
                        self.app.enter_l4(cid, back_view="domain")
                    else:
                        self.app.enter_l3(ns, cid, back_view="domain")
            elif event.list_view.id == "domain-books":
                if 0 <= index < len(self._domain_books):
                    self._last_book_row[ns] = index
                    self.app.open_book(ns, self._domain_books[index]["book"], back_view="domain", back_ns=ns)

    def action_toggle_panel(self) -> None:
        if self.view != "domain":
            return
        right = self.query_one("#domain-books", ListView)
        left = self.query_one("#domain-cards", ListView)
        if self.focused is right:
            left.focus()
        else:
            right.focus()

    async def action_back(self) -> None:
        if self.view == "domain":
            await self._load_domains()
        # domains 是首屏，Esc 不退出（用 q）
