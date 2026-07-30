"""Computational checks for Loom.

Two categories per 005 §5.3 and §5.4:

  1. Per-card checks (run inside write-draft, single card)
  2. Batch checks (run in stop-check, across all drafts of a task)

All checks are pure functions returning a CheckResult. No LLM, no side effects.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    check_id: str
    passed: bool
    reason: str = ""


@dataclass
class CardDraft:
    """A parsed draft card (from drafts/<id>.md frontmatter + body)."""
    id: str
    title: str
    type: str
    content: str
    source: str | None = None
    layer: str = "L2"          # from plan
    origin: str = "ai"
    links: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------

def load_plan(task_id: str) -> dict[str, Any] | None:
    import json
    p = Path(f"/tmp/loom_task/{task_id}/plan.json")
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _link_targets_exist(conn: sqlite3.Connection,
                        link_ids: list[str], drafts: list[CardDraft]) -> dict[str, bool]:
    """Check existence considering both DB and current drafts (intra-task refs)."""
    draft_ids = {d.id for d in drafts}
    out: dict[str, bool] = {}
    for lid in link_ids:
        if lid in draft_ids:
            out[lid] = True
            continue
        row = conn.execute("SELECT 1 FROM cards WHERE id=?", (lid,)).fetchone()
        out[lid] = row is not None
    return out


# ---------------------------------------------------------------------------
# Per-card checks — run inside write-draft
# ---------------------------------------------------------------------------

ENUMERATION_PATTERN = re.compile(r"第\d+条")


def check_type_valid(d: CardDraft) -> CheckResult:
    ok = d.type in store.VALID_TYPES
    return CheckResult(
        "type_valid",
        ok,
        "" if ok else f"type '{d.type}' 不在合法集 {sorted(store.VALID_TYPES)}",
    )


def check_namespace_format(d: CardDraft) -> CheckResult:
    """card_id namespace 格式校验（005 §2.1）。

    L1=<领域>:<书>:src:<单元ID>
    L2=<领域>:<书>:<卢曼ID>
    L3=<领域>:<卢曼ID>
    L4=gen:<卢曼ID>

    卢曼 ID：数字开头，字母数字交替（01 / 12a / 12a1）。
    """
    if d.layer not in store.CARD_ID_PATTERNS:
        return CheckResult("namespace_format", True)
    if not store.card_id_matches_layer(d.id, d.layer):
        return CheckResult(
            "namespace_format", False,
            f"card_id '{d.id}' 不符合 layer={d.layer} 的 namespace 格式（005 §2.1）。"
            f"期望：L1=<域>:<书>:src:<单元> / L2=<域>:<书>:<卢曼ID> / "
            f"L3=<域>:<卢曼ID> / L4=gen:<卢曼ID>；卢曼 ID 完整匹配"
            f"数字段(字母段数字段)*字母段?（01/12a/12a1）",
        )
    return CheckResult("namespace_format", True)


def check_luhmann_parent_exists(
    d: CardDraft,
    conn: sqlite3.Connection,
    drafts: list[CardDraft],
) -> CheckResult:
    """卢曼父级存在校验（005 §2.1 / §5.3）。

    L2/L3/L4 新卡的父级 ID 必须先存在（库中或同 task drafts）。
    根卡（无父级）不校验。
    """
    if d.layer not in ("L2", "L3", "L4"):
        return CheckResult("luhmann_parent_exists", True)
    parent_id = store._parent_id(d.id)
    if parent_id is None:
        # 顶层根卡，无需父级
        return CheckResult("luhmann_parent_exists", True)
    draft_ids = {x.id for x in drafts}
    if parent_id in draft_ids:
        return CheckResult("luhmann_parent_exists", True)
    row = conn.execute("SELECT 1 FROM cards WHERE id=?", (parent_id,)).fetchone()
    if row is not None:
        return CheckResult("luhmann_parent_exists", True)
    return CheckResult(
        "luhmann_parent_exists",
        False,
        f"card_id '{d.id}' 的父级 '{parent_id}' 不存在。"
        f"请先建父级卡，再建子节点（005 §2.1 卢曼树规则）。",
    )


def check_layer_type_matrix(d: CardDraft) -> CheckResult:
    allowed = store.LAYER_TYPE_MATRIX.get(d.layer, set())
    ok = d.type in allowed
    return CheckResult(
        "layer_type_matrix",
        ok,
        "" if ok else f"type '{d.type}' 不允许在 layer={d.layer}（允许: {sorted(allowed)}）",
    )


def check_min_length(d: CardDraft) -> CheckResult:
    ok = len(d.content.strip()) >= store.MIN_CONTENT_LEN
    return CheckResult(
        "min_length",
        ok,
        "" if ok else f"content 仅 {len(d.content.strip())} 字，< {store.MIN_CONTENT_LEN}（疑似目录型）",
    )


def check_no_redundant_title_heading(d: CardDraft) -> CheckResult:
    """Card mirrors add their own H1; body must not repeat the exact title."""
    first_line = next(
        (line.strip() for line in d.content.splitlines() if line.strip()),
        "",
    )
    repeated = first_line in {f"# {d.title.strip()}", d.title.strip()}
    return CheckResult(
        "no_redundant_title_heading",
        not repeated,
        (
            ""
            if not repeated
            else "content 首行重复 card title；卡片镜像会自动生成 H1，请从正文命题开始"
        ),
    )


def check_links_exist(
    d: CardDraft,
    conn: sqlite3.Connection,
    drafts: list[CardDraft],
) -> CheckResult:
    """All links must resolve to committed cards or current task drafts.

    Link is the explicit graph truth in Loom; unresolved targets create dangling
    edges that search/browse/neighbors cannot faithfully interpret.
    """
    if not d.links:
        return CheckResult("links_exist", True)
    exists = _link_targets_exist(conn, d.links, drafts)
    missing = [lid for lid, ok in exists.items() if not ok]
    return CheckResult(
        "links_exist",
        not missing,
        "" if not missing else f"links 包含未入库、也不在当前 task drafts 中的目标: {missing}",
    )


def check_l3_links_lower(d: CardDraft, conn: sqlite3.Connection,
                          drafts: list[CardDraft]) -> CheckResult:
    if d.layer != "L3":
        return CheckResult("l3_links_lower", True)
    if not d.links:
        return CheckResult("l3_links_lower", False, "L3 卡没有任何 link（必须至少 link 一张 L2）")
    draft_map = {x.id: x for x in drafts}
    has_l2 = False
    for lid in d.links:
        if lid in draft_map:
            if draft_map[lid].layer == "L2":
                has_l2 = True
                break
        else:
            row = conn.execute(
                "SELECT layer FROM cards WHERE id=?", (lid,)).fetchone()
            if row and row["layer"] == "L2":
                has_l2 = True
                break
    return CheckResult(
        "l3_links_lower",
        has_l2,
        "" if has_l2 else "L3 卡的 link 目标没有 L2 卡",
    )


def _card_domain_ns(card_id: str) -> str | None:
    """从 card_id 提取领域 namespace（第一段）。
    gen 视为元层不算领域（返回 None），其他返回首段。
    fin:kahneman:12a → fin ; fin:3a → fin ; gen:1a → None
    """
    if ":" not in card_id:
        return None
    ns = card_id.split(":", 1)[0]
    return None if ns == "gen" else ns


def _card_material_ns(card_id: str) -> str | None:
    """从 card_id 提取材料 namespace（前两段）。
    L2：domain:book:<id> → domain:book
    L3：domain:<id>      → domain（无 book 概念，同领域同材料前缀不可判）
    L1：domain:book:src:<unit> → domain:book
    gen 视为元层不算材料（返回 None）。
    """
    if ":" not in card_id:
        return None
    parts = card_id.split(":")
    if parts[0] == "gen":
        return None
    if len(parts) >= 2:
        return ":".join(parts[:2])
    return parts[0]


def check_l4_links_lower(d: CardDraft, conn: sqlite3.Connection,
                          drafts: list[CardDraft]) -> CheckResult:
    """L4 必须至少 link 2 个不同领域 namespace 的 L2/L3 卡。

    L4 是跨域元层抽象——单领域涌现的应该是 L3 不是 L4。
    判据：跨材料 ≠ 跨领域。
    """
    if d.layer != "L4":
        return CheckResult("l4_links_lower", True)
    if not d.links:
        return CheckResult("l4_links_lower", False,
            "L4 卡没有任何 link（必须 link ≥2 个不同领域 namespace 的 L2/L3）")
    draft_map = {x.id: x for x in drafts}
    domains_with_lower: set[str] = set()
    has_lower = False
    for lid in d.links:
        layer = None
        if lid in draft_map:
            layer = draft_map[lid].layer
        else:
            row = conn.execute(
                "SELECT layer FROM cards WHERE id=?", (lid,)).fetchone()
            if row:
                layer = row["layer"]
        if layer in ("L2", "L3"):
            has_lower = True
            ns = _card_domain_ns(lid)
            if ns:
                domains_with_lower.add(ns)
    if not has_lower:
        return CheckResult("l4_links_lower", False,
            "L4 卡的 link 目标没有 L2 或 L3 卡（L4 必须锚到具体层级）")
    if len(domains_with_lower) < 2:
        return CheckResult("l4_links_lower", False,
            f"L4 卡的 link 只覆盖 {len(domains_with_lower)} 个领域"
            f"（{sorted(domains_with_lower)}），必须 ≥2 个不同领域才算跨域"
            f"（单领域涌现应归 L3）")
    return CheckResult("l4_links_lower", True)


def check_reflection_anchored(d: CardDraft, conn: sqlite3.Connection,
                               drafts: list[CardDraft]) -> CheckResult:
    if d.type != "反思":
        return CheckResult("reflection_anchored", True)
    if not d.links:
        return CheckResult("reflection_anchored", False,
                           "反思卡没有任何 link（必须锚定 判断/模式 卡）")
    draft_map = {x.id: x for x in drafts}
    has_anchor = False
    for lid in d.links:
        if lid in draft_map:
            if draft_map[lid].type in ("判断", "模式"):
                has_anchor = True
                break
        else:
            row = conn.execute(
                "SELECT type FROM cards WHERE id=?", (lid,)).fetchone()
            if row and row["type"] in ("判断", "模式"):
                has_anchor = True
                break
    return CheckResult(
        "reflection_anchored",
        has_anchor,
        "" if has_anchor else "反思卡没有 link 到 判断/模式 卡",
    )


def check_l4_index_format(d: CardDraft) -> CheckResult:
    if d.layer != "L4":
        return CheckResult("l4_index_format", True)
    first_line = d.content.lstrip().split("\n", 1)[0]
    ok = bool(store.MATURITY_PATTERN.match(first_line))
    return CheckResult(
        "l4_index_format",
        ok,
        "" if ok else "L4 卡 content 第一段必须以 [探索期] 或 [熟练期] 开头",
    )


def check_source_real(d: CardDraft, conn: sqlite3.Connection) -> CheckResult:
    if d.layer == "L1":
        if not d.source:
            return CheckResult("source_real", False, "L1.source 为空（必须保留原始 markdown 路径）")
        ok = Path(d.source).exists() or (store.PROJECT_ROOT / d.source).exists()
        return CheckResult(
            "source_real", ok,
            "" if ok else f"L1 source 文件不存在: {d.source}",
        )
    if d.layer == "L2":
        if not d.source:
            return CheckResult("source_real", False, "L2.source 为空（必须指向 L1 source card）")
        row = conn.execute(
            "SELECT layer, type FROM cards WHERE id=?", (d.source,)
        ).fetchone()
        ok = row is not None and row["layer"] == "L1" and row["type"] == "source"
        return CheckResult(
            "source_real", ok,
            "" if ok else f"L2.source 必须指向 L1 source card: {d.source}",
        )
    if not d.source:
        return CheckResult("source_real", True)
    return CheckResult("source_real", True)


def check_card_id_unique(d: CardDraft, conn: sqlite3.Connection) -> CheckResult:
    """单卡层：draft id 不能与库里已有 id 冲突。

    前置拦截——agent 写第一张卡时就知道 ID 冲突，不用等写完整批再被 stop-check 拒。
    batch 层的 check_id_unique 保留作为兜底（同 task 内多张卡同时撞库的情况）。
    """
    row = conn.execute("SELECT 1 FROM cards WHERE id=?", (d.id,)).fetchone()
    if row:
        return CheckResult(
            "card_id_unique", False,
            f"card_id {d.id} 已存在于库中（并行子 agent 命名撞车，请改用不同后缀）",
        )
    return CheckResult("card_id_unique", True)


def check_l2_no_cross_domain_links(
    d: CardDraft, conn: sqlite3.Connection, drafts: list[CardDraft]
) -> CheckResult:
    """L2/DIGEST 阶段 link 规则（008 §1）：

    必选：非主题 L2 卡必须 link 对应主题卡（由 batch 层 check_l2_links_topic 强制）。
    允许：link 同一材料（同 domain:book 前缀）内的其他 L2 / L1 source card；
          反思卡必须 link 一张 判断/模式 卡（由 check_reflection_anchored 强制）。
    禁止：跨材料、跨领域、L4——放到 L3 THINK 阶段。
    """
    if d.layer != "L2":
        return CheckResult("l2_no_cross_domain", True)
    own_mat = _card_material_ns(d.id)
    if not own_mat:
        return CheckResult("l2_no_cross_domain", True)
    own_domain = _card_domain_ns(d.id)
    draft_map = {x.id: x for x in drafts}
    bad: list[str] = []
    for lid in d.links:
        if lid.endswith(".md") or "/" in lid:
            continue
        # 库里已有卡
        row = conn.execute(
            "SELECT layer FROM cards WHERE id=?", (lid,)).fetchone()
        layer = row["layer"] if row else None
        in_draft = lid in draft_map
        target_layer = layer or (draft_map[lid].layer if in_draft else None)
        # L4：L2 一律不 link
        if target_layer == "L4":
            bad.append(lid)
            continue
        # 同材料内：放行（L1 source card、主题卡、本材料 L2 都属于这一档）
        target_mat = _card_material_ns(lid)
        if target_mat and target_mat == own_mat:
            continue
        # 同领域但不同材料：L2 阶段不允许
        target_domain = _card_domain_ns(lid)
        if target_domain and target_domain != own_domain:
            bad.append(lid)
            continue
        if target_domain and target_domain == own_domain and target_mat != own_mat:
            bad.append(lid)
            continue
    if bad:
        return CheckResult(
            "l2_no_cross_domain", False,
            f"L2 卡 {d.id}（材料 {own_mat}）link 了跨材料/跨领域/L4 卡 {bad}——"
            f"L2/DIGEST 阶段只允许本材料内 link；跨材料/跨领域/L4 放到 L3 THINK 阶段",
        )
    return CheckResult("l2_no_cross_domain", True)


PER_CARD_CHECKS = [
    check_type_valid,
    check_namespace_format,
    check_luhmann_parent_exists,
    check_layer_type_matrix,
    check_min_length,
    check_no_redundant_title_heading,
    check_links_exist,
    check_l3_links_lower,
    check_l4_links_lower,
    check_reflection_anchored,
    check_l4_index_format,
    check_source_real,
    check_card_id_unique,
    check_l2_no_cross_domain_links,
]


def run_per_card_checks(d: CardDraft, conn: sqlite3.Connection,
                         drafts: list[CardDraft]) -> list[CheckResult]:
    """Run all 14 per-card checks. Returns list of results (all of them).

    注：source_real 对 L4 提案（无 source）跳过；propose-l4 调用方需自行排除。
    """
    results = [
        check_type_valid(d),
        check_namespace_format(d),
        check_luhmann_parent_exists(d, conn, drafts),
        check_layer_type_matrix(d),
        check_min_length(d),
        check_no_redundant_title_heading(d),
        check_links_exist(d, conn, drafts),
        check_l3_links_lower(d, conn, drafts),
        check_l4_links_lower(d, conn, drafts),
        check_reflection_anchored(d, conn, drafts),
        check_l4_index_format(d),
        check_source_real(d, conn),
        check_card_id_unique(d, conn),
        check_l2_no_cross_domain_links(d, conn, drafts),
    ]
    return results


# ---------------------------------------------------------------------------
# Batch checks (4) — run in stop-check
# ---------------------------------------------------------------------------

def check_l2_has_topic(drafts: list[CardDraft], plan: dict[str, Any]) -> CheckResult:
    """L2 任务主题卡数量约束。

    按 phase 区分（005 §5.4）：
    - scout: ≥ 1 张主题卡（每章一张）
    - deep: == 0 张主题卡（主题卡由 Scout 建，Deep 不建）

    注：write-draft 已强制 phase ∈ {scout, deep}，else 分支是防御性兜底，正常不可达。
    """
    layer = plan.get("layer", "")
    if layer not in ("L2", "L2_light"):
        return CheckResult("l2_has_topic", True, "（非 L2 任务，跳过）")

    phase = plan.get("phase", "")
    topics = [d for d in drafts if d.type == "主题"]
    n = len(topics)

    if phase == "scout":
        ok = n >= 1
        return CheckResult(
            "l2_has_topic", ok,
            "" if ok else f"Scout 任务需要 ≥1 张 type=主题 的卡（当前 {n} 张）",
        )
    elif phase == "deep":
        ok = n == 0
        return CheckResult(
            "l2_has_topic", ok,
            "" if ok else f"Deep 任务不应建主题卡（应由 Scout 建，当前 drafts 里有 {n} 张）",
        )
    else:
        # 防御性兜底：write-draft 已拒绝无 phase 的 L2 任务，此分支正常不可达
        ok = n == 1
        return CheckResult(
            "l2_has_topic", ok,
            "" if ok else f"[兜底] L2 任务需要恰好 1 张主题卡（当前 {n} 张；正常应走 scout/deep）",
        )


def check_l2_links_topic(
    drafts: list[CardDraft],
    plan: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> CheckResult:
    """L2 卡 link 主题卡约束（整批校验，§5.4）。

    按 phase 区分：
    - scout: 不检查（scout 的 drafts 全是主题卡）
    - deep: 非主题卡必须 link plan.topic_card（已 commit 的主题卡）

    注：write-draft 已强制 phase ∈ {scout, deep}，else 分支是防御性兜底。
    """
    layer = plan.get("layer", "")
    if layer not in ("L2", "L2_light"):
        return CheckResult("l2_links_topic", True, "（非 L2 任务，跳过）")

    phase = plan.get("phase", "")

    if phase == "scout":
        return CheckResult("l2_links_topic", True, "（Scout 阶段，跳过）")

    if phase == "deep":
        topic_card_id = plan.get("topic_card")
        if not topic_card_id:
            return CheckResult(
                "l2_links_topic", False,
                "Deep 任务 plan 缺 topic_card 字段（应指定已 commit 的主题卡 id）",
            )
        if conn is not None:
            row = conn.execute(
                "SELECT type, layer FROM cards WHERE id=?", (topic_card_id,)
            ).fetchone()
            if row is None:
                return CheckResult(
                    "l2_links_topic", False,
                    f"Deep 任务的 topic_card {topic_card_id} 未在库中"
                    f"（Scout 是否 commit 成功？）",
                )
            if row["type"] != "主题":
                return CheckResult(
                    "l2_links_topic", False,
                    f"Deep 任务的 topic_card {topic_card_id} 不是主题卡"
                    f"（type={row['type']}）",
                )
        bad = [d.id for d in drafts
               if d.type != "主题" and topic_card_id not in d.links]
        return CheckResult(
            "l2_links_topic", not bad,
            "" if not bad else f"以下 L2 卡未 link 主题卡 {topic_card_id}: {bad}",
        )

    # 防御性兜底：write-draft 已拒绝无 phase 的 L2 任务，此分支正常不可达
    topics = [d for d in drafts if d.type == "主题"]
    if not topics:
        return CheckResult("l2_links_topic", False, "[兜底] 无主题卡，无法检查 link")
    topic_id = topics[0].id
    bad = [d.id for d in drafts
           if d.type != "主题" and topic_id not in d.links]
    return CheckResult(
        "l2_links_topic", not bad,
        "" if not bad else f"[兜底] 以下 L2 卡未 link 主题卡 {topic_id}: {bad}",
    )


def check_id_unique(drafts: list[CardDraft], conn: sqlite3.Connection) -> CheckResult:
    """ID 唯一性检测（整批层，需 conn）。

    防止 draft card_id 与 cards 表中已有 ID 冲突——常见于并行 THINK
    子 agent 独立工作、各自用了同名 ID，commit 时撞 UNIQUE。
    """
    if not drafts:
        return CheckResult("id_unique", True)
    conflicts = []
    for d in drafts:
        row = conn.execute("SELECT 1 FROM cards WHERE id=?", (d.id,)).fetchone()
        if row:
            conflicts.append(d.id)
    if conflicts:
        return CheckResult(
            "id_unique", False,
            f"{len(conflicts)} 个 card_id 与库中已有卡冲突（并行子 agent 独立用了同名 ID）"
            f"，冲突 ID: {conflicts[:10]}",
        )
    return CheckResult("id_unique", True)


def check_no_duplication(drafts: list[CardDraft]) -> CheckResult:
    """跨卡重复检测（整批层）。

    防止同 task 内多张卡高度相似（切碎同一单元 / 重复表达）。
    用 difflib.SequenceMatcher 计算两两 content 相似度，> 0.7 拒。
    """
    import difflib

    if len(drafts) < 2:
        return CheckResult("no_duplication", True)

    DUPLICATE_THRESHOLD = 0.7
    duplicates: list[dict[str, Any]] = []
    for i, a in enumerate(drafts):
        for j in range(i + 1, len(drafts)):
            b = drafts[j]
            sim = difflib.SequenceMatcher(None, a.content, b.content).ratio()
            if sim > DUPLICATE_THRESHOLD:
                duplicates.append({
                    "a": a.id, "b": b.id, "similarity": round(sim, 2),
                })

    if duplicates:
        return CheckResult(
            "no_duplication", False,
            f"检测到 {len(duplicates)} 对高度相似卡（>{DUPLICATE_THRESHOLD}），疑似切碎或重复: {duplicates}",
        )
    return CheckResult("no_duplication", True)


BATCH_CHECKS = [check_l2_has_topic, check_l2_links_topic, check_no_duplication, check_id_unique]


# ---------------------------------------------------------------------------
# THINK 覆盖校验（005 §4.1/§5.4）——仅当任务存在 think_state.json 时生效
# ---------------------------------------------------------------------------

def check_think_coverage(drafts: list[CardDraft], plan: dict[str, Any]) -> CheckResult:
    """每个主题要么被 ≥1 张产出 --from-topics 承接，要么有 think-skip 理由。"""
    from . import think_state

    task_id = plan.get("task_id", "")
    if not task_id or think_state.load(task_id) is None:
        return CheckResult("think_coverage", True)
    cov = think_state.coverage(task_id)
    if not cov["uncovered"]:
        return CheckResult("think_coverage", True)
    lines = [f"  - {r['topic']}（{r['title']}）" for r in cov["uncovered"]]
    return CheckResult(
        "think_coverage", False,
        f"{len(cov['uncovered'])}/{cov['total']} 个主题既无产出承接也无 skip 理由，"
        f"不允许静默跳过：\n" + "\n".join(lines) +
        "\n对明确不深挖的主题用 loom think-skip 记理由；否则写产出并 --from-topics 承接。",
    )


def check_think_read_trace(
    drafts: list[CardDraft],
    plan: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> CheckResult:
    """THINK 任务中 L3 draft link 的每张 L2 必须在本任务 read trace 里出现过。"""
    from . import think_state

    task_id = plan.get("task_id", "")
    if not task_id or think_state.load(task_id) is None or conn is None:
        return CheckResult("think_read_trace", True)

    trace_file = Path(f"/tmp/loom_task/{task_id}/.read_trace.jsonl")
    read_ids: set[str] = set()
    if trace_file.exists():
        for line in trace_file.read_text(encoding="utf-8").splitlines():
            try:
                read_ids.update(json.loads(line).get("card_ids", []))
            except (json.JSONDecodeError, AttributeError):
                continue

    unread: list[str] = []
    for d in drafts:
        if d.layer != "L3":
            continue
        for target in d.links:
            row = conn.execute("SELECT layer FROM cards WHERE id=?", (target,)).fetchone()
            # 同批 draft 里的 L2 也算已读（作者自己写的）；库中 L2 必须有 read trace
            if row is None or row["layer"] != "L2":
                continue
            if target not in read_ids:
                unread.append(f"  - {d.id} link 了 {target}，但本任务没有 read-cards 过它")
    if unread:
        return CheckResult(
            "think_read_trace", False,
            "L3 卡 link 了未深读的 L2（凭标题 link 视为未读）：\n" + "\n".join(unread) +
            f"\n先 loom read-cards <id> --task-id {task_id} 深读，再决定 link 与否。",
        )
    return CheckResult("think_read_trace", True)


def run_batch_checks(
    drafts: list[CardDraft],
    plan: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> list[CheckResult]:
    results = [
        check_l2_has_topic(drafts, plan),
        check_l2_links_topic(drafts, plan, conn),
        check_no_duplication(drafts),
        check_think_coverage(drafts, plan),
    ]
    if conn is not None:
        results.append(check_id_unique(drafts, conn))
        results.append(check_think_read_trace(drafts, plan, conn))
    return results


# ---------------------------------------------------------------------------
# Draft file I/O
# ---------------------------------------------------------------------------

DRAFT_HEADER_PATTERN = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL
)


def parse_draft_file(path: Path) -> CardDraft | None:
    """Parse a drafts/<id>.md file (frontmatter + body).

    Frontmatter fields: title, type, source, layer, origin, links
    Body: the content.

    Returns None if the file has no frontmatter (treated as a stray
    non-draft file, e.g. a temp content file the agent accidentally
    left in drafts/). Such files are skipped by list_drafts.
    """
    text = path.read_text(encoding="utf-8")
    m = DRAFT_HEADER_PATTERN.match(text)
    if not m:
        return None
    fm_text, body = m.group(1), m.group(2).strip()
    fields: dict[str, str] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    links_raw = fields.get("links", "")
    if links_raw:
        links_raw = links_raw.strip()
        if links_raw.startswith("[") and links_raw.endswith("]"):
            links_raw = links_raw[1:-1]
    links = (
        [s.strip().strip('"').strip("'") for s in links_raw.split(",") if s.strip()]
        if links_raw
        else []
    )
    return CardDraft(
        id=path.stem,
        title=fields.get("title", path.stem),
        type=fields.get("type", ""),
        content=body,
        source=fields.get("source") or None,
        layer=fields.get("layer", "L2"),
        origin=store.normalize_origin(fields.get("origin")),
        links=links,
    )


def list_drafts(task_id: str) -> list[CardDraft]:
    """Load all drafts for a task. Sorted by filename.

    Skips files without frontmatter (stray non-draft files like temp
    content files the agent may have left in drafts/).
    """
    drafts_dir = Path(f"/tmp/loom_task/{task_id}/drafts")
    if not drafts_dir.exists():
        return []
    drafts = []
    for p in sorted(drafts_dir.glob("*.md")):
        d = parse_draft_file(p)
        if d is not None:
            drafts.append(d)
    return drafts
