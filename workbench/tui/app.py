"""Loom Workbench TUI — 终端内的卡片阅读器。

默认界面是阅读器（连续翻阅一本书的 L2 卡），其余能力按需进入：
  /  搜索弹层      e  扩展弹层（子卡/关联）      a  批注编辑弹层
  t  树浏览模式    m  批注列表模式              b  书选择
  [  ]  浏览历史前进/后退                        q  退出
"""
from __future__ import annotations

import bisect

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer

from . import queries as q
from .books import BooksScreen
from .pops import HelpPop, SearchPop
from .reader import ReaderScreen

HISTORY_CAP = 50


class WorkbenchApp(App):
    TITLE = "Loom Workbench"
    SUB_TITLE = "终端卡片阅读器"
    # textual 鼠标文本选择（选中高亮）。注意：选中后 ⌘C 走终端原生复制，
    # 终端对 textual 的 SGR 高亮选区不一定识别——若复制空是终端协议问题。
    ALLOW_SELECT = True

    CSS = """
    Screen { layout: vertical; }
    Footer { height: 1; }

    /* 低调朴实：低对比、柔和的边框与背景 */
    #reader-main { height: 1fr; }
    #reader-toc {
        width: 32%;
        max-width: 42%;
        border-right: solid #404860;
    }
    #reader-right { width: 1fr; }
    #reader-links {
        height: auto;
        max-height: 40%;
        border-bottom: solid #404860;
    }
    #reader-body {
        height: 1fr;
        overflow-y: auto;   /* Markdown 默认禁滚，显式开启以支持鼠标滚轮 */
        scrollbar-gutter: stable;
        border: none;
        background: $surface;
    }

    /* ListView 选中项：柔和的暗蓝底 + 亮字，低对比不刺眼 */
    ListView > ListItem.selected {
        background: #3a4a6b;
        text-style: bold;
    }
    ListView > ListItem.selected > Label {
        color: #e6e6f0;
    }

    /* 弹层：柔和边框 */
    .popup {
        border: round #404860;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("?", "help", "帮助"),
        Binding("/", "search", "搜索"),
        Binding("escape", "back", "返回"),
        # ctrl+c = 复制选中文本（textual 默认 ctrl+c 是 help_quit 退出提示，
        # 覆盖为复制；走 OSC52 剪贴板协议，不依赖终端选区）
        Binding("ctrl+c", "copy_text", "复制选中", show=False),
    ]

    def action_copy_text(self) -> None:
        selection = self.screen.get_selected_text() if hasattr(self.screen, "get_selected_text") else None
        if selection:
            self.copy_to_clipboard(selection)
            self.notify("已复制选中")

    def on_text_selected(self, event) -> None:
        """鼠标划中文字（选择完成）→ 自动放进剪贴板，无提示。"""
        selection = self.screen.get_selected_text() if hasattr(self.screen, "get_selected_text") else None
        if selection:
            self.copy_to_clipboard(selection)

    def action_back(self) -> None:
        """全局 Esc 兜底：从阅读器/模式屏逐层返回。"""
        # 弹层/模式屏各自有 Esc 绑定；这里处理阅读器（栈里只有它或它+default）
        if isinstance(self.screen, ReaderScreen):
            self.go_back_to_books()
        elif len(self.screen_stack) > 1:
            self.pop_screen()

    def go_back_to_books(self) -> None:
        """从阅读器返回书选择（回到进来时的域视图/首屏）。"""
        # open_card 会把 BooksScreen 替换成 ReaderScreen，栈底只剩 default——
        # 直接弹到 default，再新建 BooksScreen 并恢复视图
        while len(self.screen_stack) > 1 and not isinstance(self.screen, BooksScreen):
            self.pop_screen()
        if not isinstance(self.screen, BooksScreen):
            bs = BooksScreen(
                initial_view=self.back_view,
                initial_ns=self.back_ns if self.back_view == "domain" else "",
            )
            self.push_screen(bs)

    def __init__(self) -> None:
        super().__init__()
        self.ns = ""
        self.book = ""
        self.context_type = "book"  # book | l3 | l4（决定 ↓/↑ 翻卡序列）
        self.context_label = ""     # 显示：如 "llm:harness" / "llm L3" / "gen L4"
        self.context_cards: list[dict] = []
        self.context_ids: list[str] = []
        self.context_key = ""
        self.back_view = "domains"  # Esc 返回时的书选择视图（domains 或 domain+ns）
        self.back_ns = ""
        self.history: list[str] = []
        self.history_index = -1

    def on_mount(self) -> None:
        self.push_screen(BooksScreen())

    # ------------------------------------------------------------------
    # 翻卡上下文：book（书内非 L1）/ l3（某域 L3）/ l4（全部 L4）
    # ------------------------------------------------------------------
    def _set_context(self, ctype: str, label: str, cards: list[dict], key: str) -> None:
        self.context_type = ctype
        self.context_label = label
        self.context_cards = cards
        self.context_ids = [c["id"] for c in cards]
        self.context_key = key

    def load_context_cards(self, ctype: str, label: str, key: str) -> list[dict]:
        if self.context_key == key:
            return self.context_cards
        if ctype == "book":
            ns, book = label.split(":", 1)
            data = q.cards_by_ns(ns, book=book)
            cards = [c for c in data["cards"] if c["layer"] != "L1"]
        elif ctype == "l3":
            ns = label.split(" ", 1)[0]
            cards = q.l3_by_ns(ns)
        elif ctype == "l4":
            cards = q.l4_all()
        else:
            cards = []
        self._set_context(ctype, label, cards, key)
        return cards

    def index_of(self, card_id: str) -> int:
        """卡在翻卡序列的位置；不在序列（如 L1 原文）返回 -1。"""
        i = bisect.bisect_left(self.context_ids, card_id)
        return i if i < len(self.context_ids) and self.context_ids[i] == card_id else -1

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------
    def open_book(self, ns: str, book: str, back_view: str | None = None, back_ns: str = "") -> None:
        self.ns, self.book = ns, book
        if back_view:
            self.back_view, self.back_ns = back_view, back_ns
        key = f"{ns}:{book}"
        cards = self.load_context_cards("book", key, key)
        scope = f"{ns}:{book}"
        prog = q.get_reading_progress(scope)
        target = ""
        if prog and prog["last_card_id"] and prog["last_card_id"] in self.context_ids:
            target = prog["last_card_id"]
        if not target and cards:
            target = cards[0]["id"]
        if target:
            self.open_card(target)

    def enter_l3(self, ns: str, card_id: str | None = None, back_view: str | None = None) -> None:
        """进入某域的 L3 卡列表（翻卡上下文 = 该域 L3）。"""
        self.ns = ns
        self.book = ""
        if back_view:
            self.back_view, self.back_ns = back_view, ns
        label = f"{ns} L3"
        cards = self.load_context_cards("l3", label, label)
        target = card_id if card_id and card_id in self.context_ids else (cards[0]["id"] if cards else "")
        if target:
            self.open_card(target)

    def enter_l4(self, card_id: str | None = None, back_view: str | None = None) -> None:
        """进入全部 L4 卡（翻卡上下文 = gen L4）。"""
        self.ns = "gen"
        self.book = ""
        if back_view:
            self.back_view, self.back_ns = back_view, "gen"
        cards = self.load_context_cards("l4", "gen L4", "gen L4")
        target = card_id if card_id and card_id in self.context_ids else (cards[0]["id"] if cards else "")
        if target:
            self.open_card(target)

    def open_card(self, card_id: str, from_history: bool = False) -> None:
        if not from_history:
            self.push_history(card_id)
        ns = card_id.split(":", 1)[0]
        if ns and ns != self.ns:
            self.ns = ns
            self.book = ""
            self.context_cards, self.context_ids = [], []
            self.context_key = ""
            self.context_type = "book"
            self.context_label = ""
        # 归一化屏幕栈：弹层/模式屏全部退掉，栈顶回到阅读器
        while len(self.screen_stack) > 1 and not isinstance(self.screen, ReaderScreen):
            self.pop_screen()
        if isinstance(self.screen, ReaderScreen):
            self.screen.set_card(card_id)
        elif isinstance(self.screen, BooksScreen):
            # 首屏开书：books 替换成 reader
            self.switch_screen(ReaderScreen())
            self.screen.set_card(card_id)
        else:
            # 栈底 default（on_mount 尚未完成时防御性兜底）
            self.push_screen(ReaderScreen())
            self.screen.set_card(card_id)

    def push_history(self, card_id: str) -> None:
        if self.history and self.history[self.history_index] == card_id:
            return
        self.history = self.history[: self.history_index + 1] + [card_id]
        if len(self.history) > HISTORY_CAP:
            self.history = self.history[-HISTORY_CAP:]
        self.history_index = len(self.history) - 1

    def go_back(self) -> None:
        if self.history_index <= 0:
            return
        self.history_index -= 1
        self.open_card(self.history[self.history_index], from_history=True)

    def go_forward(self) -> None:
        if self.history_index >= len(self.history) - 1:
            return
        self.history_index += 1
        self.open_card(self.history[self.history_index], from_history=True)

    # ------------------------------------------------------------------
    # 全局动作
    # ------------------------------------------------------------------
    def action_help(self) -> None:
        self.push_screen(HelpPop())

    def action_search(self) -> None:
        self.push_screen(SearchPop())

    def refresh_reader(self) -> None:
        """批注/标签变化后刷新当前阅读器的头部显示。"""
        if isinstance(self.screen, ReaderScreen):
            self.screen.refresh_header()
