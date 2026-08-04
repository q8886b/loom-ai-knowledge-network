"""弹层：搜索 / 扩展 / 批注编辑 / 帮助。

搜索（/）与扩展（e）：输入或选择后打开卡并自动关闭。
批注编辑（a）：标签复用 cards.tags，笔记写入 card_annotations。
帮助（?）：全量快捷键表。
"""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, Static, TextArea

from . import queries as q

LAYER_COLORS = {"L1": "grey62", "L2": "#6366f1", "L3": "#10b981", "L4": "#f59e0b"}

_MODAL_CSS = """
ModalScreen {
    align: center middle;
}
.popup {
    width: 70%;
    height: 60%;
    border: round $primary;
    background: $surface;
    padding: 1;
}
.popup-wide {
    width: 80%;
    height: 70%;
}
"""


class SearchPop(ModalScreen):
    """输入即搜，方向键选结果，Enter 打开卡。"""

    BINDINGS = [Binding("escape", "dismiss", "关闭")]

    def __init__(self) -> None:
        super().__init__()
        self._results: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="popup popup-wide"):
            yield Static("搜索", id="pop-title", classes="pop-title")
            yield Input(placeholder="输入 ≥2 字，自动搜索", id="search-input")
            yield ListView(id="search-results")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        lv = self.query_one("#search-results", ListView)
        lv.clear()
        if len(query) < 2:
            self._results = []
            return
        self._results = q.search(query, top=30)
        for i, r in enumerate(self._results):
            color = LAYER_COLORS.get(r["layer"], "white")
            title = Text(f"{i + 1:>3} ", style="grey37")
            title.append(f"[{r['layer']}] {r['title']}", style=color)
            title.append(f"  {r['id']}", style="grey37")
            lv.append(ListItem(Label(title)))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # 输入框内回车：直接打开第一个结果（键盘流最短路径）
        if self._results:
            self.app.open_card(self._results[0]["id"])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item is None:
            return
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._results):
            # open_card 的栈归一化会先 pop 掉本弹层
            self.app.open_card(self._results[index]["id"])


class ExpandPop(ModalScreen):
    """当前卡的扩展：子卡（卢曼）+ 关联（link 邻居，含跨域）。"""

    BINDINGS = [Binding("escape", "dismiss", "关闭")]

    def __init__(self, card_id: str) -> None:
        super().__init__()
        self.card_id = card_id
        self._items: list[tuple[str, str]] = []  # (card_id, 显示标题)

    def compose(self) -> ComposeResult:
        with Vertical(classes="popup"):
            yield Static("", id="expand-title", classes="pop-title")
            yield ListView(id="expand-list")

    def on_mount(self) -> None:
        title = self.query_one("#expand-title", Static)
        title.update(Text(f"扩展 · {self.card_id}", style="bold cyan"))
        lv = self.query_one("#expand-list", ListView)
        child_ids = q.children(self.card_id)
        neighbor_ids = [n for n in q.neighbors(self.card_id, 1) if n != self.card_id]
        seen: set[str] = set()
        groups: list[tuple[str, list[str]]] = []
        if child_ids:
            groups.append(("子卡", child_ids))
        if neighbor_ids:
            groups.append(("关联", neighbor_ids))
        for group_name, ids in groups:
            lv.append(ListItem(Label(Text(f"── {group_name} ──", style="bold grey62"))))
            for cid in ids:
                if cid in seen:
                    continue
                seen.add(cid)
                card = q.get_card(cid)
                color = LAYER_COLORS.get(card["layer"], "white") if card else "white"
                text = Text(
                    f"[{card['layer']}] {card['title']}" if card else cid,
                    style=color,
                )
                if card and card["id"].split(":", 1)[0] != self.card_id.split(":", 1)[0]:
                    text.append(f"  ({card['id'].split(':', 1)[0]})", style="grey37")
                lv.append(ListItem(Label(text), id=f"exp-{cid.replace(':', '_')}"))
                self._items.append((cid, ""))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item is None:
            return
        item_id = event.item.id or ""
        if item_id.startswith("exp-"):
            # open_card 的栈归一化会先 pop 掉本弹层
            self.app.open_card(item_id[4:].replace("_", ":"))


class NotePop(ModalScreen):
    """批注编辑：标签（逗号分隔，复用 cards.tags）+ 笔记（可空）。"""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("ctrl+s", "save", "保存"),
        Binding("ctrl+enter", "save", "保存"),
    ]

    def __init__(self, card_id: str) -> None:
        super().__init__()
        self.card_id = card_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="popup"):
            yield Static("", id="note-title", classes="pop-title")
            yield Static("标签（逗号分隔）", classes="field-label")
            yield Input(id="note-tags")
            yield Static("笔记（可空，Ctrl+S 保存）", classes="field-label")
            yield TextArea(id="note-text")

    def on_mount(self) -> None:
        title = self.query_one("#note-title", Static)
        title.update(Text(f"批注 · {self.card_id}", style="bold cyan"))
        card = q.get_card(self.card_id)
        tags = ",".join(card.get("tags") or []) if card else ""
        self.query_one("#note-tags", Input).value = tags
        ann = q.get_annotation(self.card_id)
        if ann:
            self.query_one("#note-text", TextArea).value = ann.get("note") or ""
        self.query_one("#note-tags", Input).focus()

    def action_save(self) -> None:
        new_tags = [
            t.strip() for t in self.query_one("#note-tags", Input).value.split(",") if t.strip()
        ]
        card = q.get_card(self.card_id)
        old_tags = set(card.get("tags") or []) if card else set()
        add = [t for t in new_tags if t not in old_tags]
        remove = list(old_tags - set(new_tags))
        if add or remove:
            q.update_card_tags(self.card_id, add=add, remove=remove)
        note = self.query_one("#note-text", TextArea).value.strip()
        q.set_annotation(self.card_id, note)
        self.app.refresh_reader()
        self.app.notify("批注已保存")
        self.dismiss()

    def action_cancel(self) -> None:
        self.dismiss()


class HelpPop(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "关闭"), Binding("q", "dismiss", "关闭")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="popup"):
            yield Static("Loom Workbench 快捷键", id="help-title", classes="pop-title")
            lines = [
                ("阅读器", ""),
                ("n / p", "下一张 / 上一张（书内顺序，跳过 L1）"),
                ("Space", "内容翻页"),
                ("e", "扩展当前卡（子卡 / 关联，含跨域）"),
                ("a", "批注编辑（标签 + 笔记）"),
                ("[ / ]", "浏览历史 后退 / 前进"),
                ("b", "书选择（切换书）"),
                ("⌃C", "复制选中文本（鼠标划中即自动复制）"),
                ("", ""),
                ("模式", ""),
                ("t", "树浏览（x 切换 书内/全量）"),
                ("m", "批注列表（事后集中处理，可标签过滤）"),
                ("/", "搜索（全库）"),
                ("?", "本帮助"),
                ("Esc", "关闭弹层 / 返回阅读器"),
                ("q", "退出"),
            ]
            yield Static("\n".join(f"{k:<8} {v}" for k, v in lines), id="help-body")
