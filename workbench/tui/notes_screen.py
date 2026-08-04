"""批注列表模式（m）——事后集中处理被标记的卡。

全部有标签或有笔记的卡，按最近变化倒序；顶部输入框按标签过滤。
Enter 打开卡（回到阅读器）。
"""
from __future__ import annotations

import time

from rich.text import Text
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static

from . import queries as q


class NotesScreen(Screen):
    BINDINGS = [
        Binding("escape", "close", "返回"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []

    def compose(self):
        yield Static("", id="notes-header")
        yield Input(placeholder="过滤标签（如：值得深挖），留空显示全部", id="note-filter")
        yield DataTable(id="note-table")

    def on_mount(self) -> None:
        table = self.query_one("#note-table", DataTable)
        table.add_columns("序号", "卡", "层", "标签", "笔记")
        table.cursor_type = "row"
        self._load("")

    def _load(self, tag_filter: str) -> None:
        items = q.list_annotations()
        if tag_filter:
            items = [i for i in items if tag_filter in i["tags"]]
        table = self.query_one("#note-table", DataTable)
        table.clear()
        self._rows = []
        for i, it in enumerate(items):
            note = (it.get("note") or "").replace("\n", " ")[:40]
            tags = " ".join(f"#{t}" for t in it.get("tags", []))
            table.add_row(str(i + 1), it["id"], it["layer"], tags, note)
            self._rows.append(it)
        header = self.query_one("#notes-header", Static)
        header.update(
            Text.assemble(
                Text(f"批注列表 · {len(items)} 张", style="bold cyan"),
                Text("   Enter 打开 · Esc 返回", style="grey37"),
            )
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        self._load(event.value.strip())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row is None or event.cursor_row >= len(self._rows):
            return
        self.app.open_card(self._rows[event.cursor_row]["id"])

    def action_close(self) -> None:
        self.app.pop_screen()
