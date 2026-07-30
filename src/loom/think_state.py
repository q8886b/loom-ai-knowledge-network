"""单书 THINK 覆盖账（005 §4.1）。

think_state.json 是一本账，不是状态机：记录这本书有哪些主题、每个主题
被哪张产出承接、或为什么明确不深挖。探索过程完全自由（004 双路线螺旋），
账本只在收尾时给 stop-check 提供覆盖判据：

- 每个主题要么被 ≥1 张产出 --from-topics 承接，要么有 think-skip 理由；
- drafts 中 L3 卡 link 的每张 L2 必须出现在本任务 .read_trace.jsonl。

单书 THINK 是单 agent 场景：没有 worker 分配、session 绑定、并发锁。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class ThinkStateError(ValueError):
    """覆盖账操作不合法（未 init、主题不存在、重复 skip 等）。"""


def _task_dir(task_id: str) -> Path:
    return Path(f"/tmp/loom_task/{task_id}")


def state_path(task_id: str) -> Path:
    return _task_dir(task_id) / "think_state.json"


def load(task_id: str) -> dict[str, Any] | None:
    """读覆盖账；不存在（非 THINK 任务）返回 None。"""
    p = state_path(task_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _save(state: dict[str, Any]) -> None:
    p = state_path(state["task_id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def init(task_id: str, goal: str, materials: list[str],
         conn: sqlite3.Connection) -> dict[str, Any]:
    """从材料的全部已入库主题卡生成覆盖清单。materials 形如 ["fin:tianwei"]。"""
    if state_path(task_id).exists():
        raise ThinkStateError(f"task {task_id} 已有 think_state.json，不能重复 init")

    topics: dict[str, dict[str, Any]] = {}
    for material in materials:
        material = material.strip()
        rows = conn.execute(
            "SELECT id, title FROM cards"
            " WHERE layer='L2' AND type='主题' AND substr(id, 1, length(?)+1) = ? || ':'"
            " ORDER BY id",
            (material, material),
        ).fetchall()
        if not rows:
            raise ThinkStateError(
                f"材料 {material} 没有已入库的主题卡——先完成 DIGEST（Scout 建主题卡）再 THINK"
            )
        for row in rows:
            topics[row["id"]] = {"title": row["title"], "skip_reason": None, "outputs": []}

    state = {
        "task_id": task_id,
        "goal": goal,
        "materials": materials,
        "created_at": time.time(),
        "topics": topics,
    }
    _task_dir(task_id).mkdir(parents=True, exist_ok=True)
    _save(state)
    return state


def _require_state(task_id: str) -> dict[str, Any]:
    state = load(task_id)
    if state is None:
        raise ThinkStateError(f"task {task_id} 没有 think_state.json——先 loom think-init")
    return state


def _require_topic(state: dict[str, Any], topic_id: str) -> dict[str, Any]:
    topic = state["topics"].get(topic_id)
    if topic is None:
        raise ThinkStateError(
            f"主题 {topic_id} 不在覆盖清单内（共 {len(state['topics'])} 个主题，"
            f"用 loom think-coverage {state['task_id']} 查看）"
        )
    return topic


def skip(task_id: str, topic_id: str, reason: str) -> dict[str, Any]:
    """对明确不深挖的主题记一句具体理由（理由是否成立由语义层判）。"""
    if not reason or not reason.strip():
        raise ThinkStateError("skip 必须给具体理由（--reason）")
    state = _require_state(task_id)
    topic = _require_topic(state, topic_id)
    if topic["outputs"]:
        raise ThinkStateError(f"主题 {topic_id} 已被产出 {topic['outputs']} 承接，不能再 skip")
    topic["skip_reason"] = reason.strip()
    _save(state)
    return state


def record_output(task_id: str, output_id: str, from_topics: list[str]) -> None:
    """产出落账：声明它承接了哪些主题。由 write-draft/propose-* 调用。"""
    state = load(task_id)
    if state is None:
        return  # 非 THINK 任务，from-topics 无账可记（调用方负责警告）
    for topic_id in from_topics:
        topic = _require_topic(state, topic_id.strip())
        # skip 后又深挖是自然路径：产出落账即撤销 skip，无需单独 unskip 命令
        topic["skip_reason"] = None
        if output_id not in topic["outputs"]:
            topic["outputs"].append(output_id)
    _save(state)


def existing_output_ids(task_id: str) -> set[str]:
    """当前仍存在的产出 id（drafts + staging），用于对账时剔除已删除产出。

    drafts 文件名即 card_id；staging 提案文件名是 prop_xxx，需读 JSON 里的
    target_id 才是它承接的卡片 id。
    """
    ids: set[str] = set()
    drafts_dir = _task_dir(task_id) / "drafts"
    if drafts_dir.is_dir():
        ids.update(p.stem for p in drafts_dir.iterdir() if p.suffix == ".md")
    staging_dir = _task_dir(task_id) / "staging"
    if staging_dir.is_dir():
        for p in staging_dir.iterdir():
            if p.suffix != ".json":
                continue
            try:
                record = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            target = record.get("target_id")
            if target and record.get("status") != "rejected":
                ids.add(target)
    return ids


def coverage(task_id: str) -> dict[str, Any]:
    """覆盖账视图：每个主题 → 承接产出（仅现存）/ skip 理由 / 未结。"""
    state = _require_state(task_id)
    existing = existing_output_ids(task_id)
    rows = []
    uncovered = []
    for topic_id, topic in sorted(state["topics"].items()):
        outputs = [o for o in topic["outputs"] if o in existing]
        status = "produced" if outputs else ("skipped" if topic["skip_reason"] else "open")
        row = {
            "topic": topic_id,
            "title": topic["title"],
            "status": status,
            "outputs": outputs,
            "skip_reason": topic["skip_reason"],
        }
        rows.append(row)
        if status == "open":
            uncovered.append(row)
    return {
        "task_id": task_id,
        "goal": state["goal"],
        "materials": state["materials"],
        "total": len(rows),
        "produced": sum(1 for r in rows if r["status"] == "produced"),
        "skipped": sum(1 for r in rows if r["status"] == "skipped"),
        "open": len(uncovered),
        "topics": rows,
        "uncovered": uncovered,
    }
