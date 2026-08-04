"""树浏览模式（t）——卢曼层级树。

默认显示当前书内树；x 切到全 namespace 主题树（只加载 主题/结构 卡作根，
子卡展开时懒加载）。选中节点打开卡并回到阅读器。
"""
from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Static, Tree
from textual.widgets._tree import TreeNode

from . import queries as q

LAYER_COLORS = {"L1": "grey62", "L2": "#6366f1", "L3": "#10b981", "L4": "#f59e0b"}


class TreeScreen(Screen):
    BINDINGS = [
        Binding("x", "toggle_scope", "书内/全量"),
        Binding("escape", "close", "返回"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.scope_book = True
        self._lazy = False
        self._loading: set[str] = set()

    def compose(self):
        yield Static("", id="tree-header")
        yield Tree("卡", id="card-tree")

    def on_mount(self) -> None:
        self._load()

    def _load(self) -> None:
        app = self.app
        tree = self.query_one("#card-tree", Tree)
        tree.clear()
        header = self.query_one("#tree-header", Static)
        if self.scope_book and app.book:
            # 书内树：完整嵌套（q.tree 已含全部 children），无需懒加载
            roots = q.tree(ns=f"{app.ns}:{app.book}")
            self._lazy = False
            header.update(Text(f"树 · {app.ns}:{app.book}", style="bold cyan"))
        else:
            # 全量树：只加载主题/结构根，子卡展开时懒加载（避免上万节点卡死）
            roots = q.theme_roots(app.ns)
            self._lazy = True
            header.update(
                Text(f"树 · {app.ns} 全部（主题/结构根节点）", style="bold cyan")
            )
        for r in roots:
            self._add_node(tree.root, r)
        if not self._lazy:
            tree.root.expand_all()
        if tree.root.children:
            # move_cursor 只移动高亮，不触发 NodeSelected（select_node 会）；
            # 延迟到布局完成后执行，避免节点 _line 未就绪
            first = tree.root.children[0]
            self.call_after_refresh(lambda: tree.move_cursor(first))

    def _add_node(self, parent: TreeNode, node: dict) -> None:
        label = self._label(node)
        child = parent.add(label, data=node["id"])
        for c in node.get("children", []):
            self._add_node(child, c)

    @staticmethod
    def _label(node: dict) -> Text:
        layer = node.get("layer", "")
        color = LAYER_COLORS.get(layer, "white")
        title = node.get("title", "")
        if layer == "L1":
            title = f"[原文] {title}"
        return Text(f"[{layer}] {title}", style=color)

    def _lazy_load_children(self, node_id: str, card_id: str) -> None:
        """懒加载子卡：LIKE 前缀查询直接子卡（不含 L1，全量树模式用）。"""
        if card_id in self._loading:
            return
        self._loading.add(card_id)
        try:
            tree = self.query_one("#card-tree", Tree)
            parent = tree.get_node_by_id(node_id)
            for child_id in q.children(card_id):
                card = q.get_card(child_id)
                if card:
                    label = Text(
                        f"[{card['layer']}] {card['title']}",
                        style=LAYER_COLORS.get(card["layer"], "white"),
                    )
                    parent.add(label, data=child_id)
        finally:
            self._loading.discard(card_id)

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        if self._lazy and node.data and not node.children:
            self._lazy_load_children(node.id, node.data)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data:
            self.app.open_card(event.node.data)

    def action_toggle_scope(self) -> None:
        self.scope_book = not self.scope_book
        self._load()

    def action_close(self) -> None:
        self.app.pop_screen()
