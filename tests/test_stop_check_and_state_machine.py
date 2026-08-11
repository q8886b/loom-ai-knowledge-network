from __future__ import annotations

import json
from pathlib import Path


def _write_raw_draft(
    task_id: str,
    card_id: str,
    *,
    title: str,
    type_: str,
    layer: str,
    source: str = "",
    links: list[str] | None = None,
    content: str,
) -> Path:
    drafts_dir = Path("/tmp/loom_task") / task_id / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    path = drafts_dir / f"{card_id}.md"
    path.write_text(
        "\n".join([
            "---",
            f"id: {card_id}",
            f"title: {title}",
            f"type: {type_}",
            f"layer: {layer}",
            f"source: {source}",
            f"links: {','.join(links or [])}",
            "---",
            "",
            content,
            "",
        ]),
        encoding="utf-8",
    )
    return path


def test_stop_check_rejects_scout_without_topic_even_if_draft_bypasses_write_draft(loom, loom_helpers):
    source_id = "llm:batch:src:01"
    loom_helpers.import_source(source_id, "sources/07-LLM/batch/ch01.md")
    task = loom["task_id"]("batch_no_topic")
    loom["write_plan"](
        task,
        task="Scout with no topic",
        source=source_id,
        layer="L2",
        phase="scout",
        skill="DIGEST",
    )
    _write_raw_draft(
        task,
        "llm:batch:01a",
        title="绕过写入的判断",
        type_="判断",
        layer="L2",
        source=source_id,
        content="这张卡模拟坏 agent 绕过 write-draft 写入非主题卡，stop-check 应该仍然拒绝 Scout 缺主题卡。",
    )
    loom["run"](["mark-ready", task])
    loom["run"](["stop-check", task], admin=True, expected=2)

    payload = loom_helpers.rejected_payload(task)
    assert payload["status"] == "rejected"
    assert "l2_has_topic" in [failure["check"] for failure in payload["failures"]]
    assert "l2_has_topic" in loom_helpers.reject_check_ids(task)


def test_stop_check_rejects_duplicate_drafts_in_same_task(loom, loom_helpers):
    _source_id, _topic_id, l2_id = loom_helpers.write_committed_l2("llm", "dupcheck")
    task = loom["task_id"]("batch_dup")
    loom["write_plan"](task, task="Duplicate L3 cards", layer="L3", skill="THINK")
    content = "这两张生成卡表达几乎完全相同的认知单元，用于验证整批重复检测会拒绝切碎或重复表达。"
    for card_id in ("llm:31", "llm:32"):
        loom["run"]([
            "write-draft", task, card_id, "--layer=L3", "--type=判断",
            f"--title={card_id}", f"--links={l2_id}", f"--content={content}",
        ])
    loom["run"](["mark-ready", task])
    loom["run"](["stop-check", task], admin=True, expected=2)

    payload = loom_helpers.rejected_payload(task)
    assert "no_duplication" in [failure["check"] for failure in payload["failures"]]
    assert "no_duplication" in loom_helpers.reject_check_ids(task)


def test_stop_check_rejects_batch_id_collision_if_draft_bypasses_write_draft(loom, loom_helpers):
    source_id, topic_id, _l2_id = loom_helpers.write_committed_l2("llm", "idbatch")
    task = loom["task_id"]("batch_id_collision")
    loom["write_plan"](
        task,
        task="Raw duplicate id",
        source=source_id,
        layer="L2",
        phase="scout",
        skill="DIGEST",
    )
    _write_raw_draft(
        task,
        topic_id,
        title="重复主题 ID",
        type_="主题",
        layer="L2",
        source=source_id,
        content="这张 raw draft 复用已入库主题卡 ID，用来验证 stop-check 的整批唯一性兜底。",
    )
    loom["run"](["mark-ready", task])
    loom["run"](["stop-check", task], admin=True, expected=2)

    checks = [failure["check"] for failure in loom_helpers.rejected_payload(task)["failures"]]
    assert "card_id_unique" in checks
    assert "id_unique" in checks


def test_mark_ready_and_stop_check_reject_empty_tasks(loom, loom_helpers):
    task = loom["task_id"]("empty_ready")
    loom["write_plan"](task, task="Empty task", layer="L3", skill="THINK")
    loom["run"](["mark-ready", task], expected=1)
    loom["run"](["stop-check", task], admin=True, expected=2)

    payload = loom_helpers.rejected_payload(task)
    assert payload["status"] == "rejected"
    assert payload["failures"][0]["check"] == "no_drafts"
    with loom["store"].connect() as conn:
        row = conn.execute(
            "SELECT status, drafts_count FROM task_trace WHERE task_id=?",
            (task,),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["drafts_count"] == 0


def test_commit_ready_rejects_added_draft_after_computed_passed(loom, loom_helpers):
    source_id = "llm:mutate:src:01"
    topic_id = "llm:mutate:01"
    loom_helpers.import_source(source_id, "sources/07-LLM/mutate/ch01.md")
    task = loom["task_id"]("added_after_compute")
    loom["write_plan"](
        task,
        task="Draft set mutation",
        source=source_id,
        layer="L2",
        phase="scout",
        skill="DIGEST",
    )
    loom["run"]([
        "write-draft", task, topic_id, "--type=主题", "--title=原主题",
        f"--source={source_id}",
        "--content=这张主题卡先通过计算层，随后测试会追加一个新的 draft 来触发 draft 集合防篡改。",
    ])
    loom["run"](["mark-ready", task])
    loom["run"](["stop-check", task], admin=True, expected=2)
    _write_raw_draft(
        task,
        "llm:mutate:02",
        title="新增主题",
        type_="主题",
        layer="L2",
        source=source_id,
        content="这张 draft 是计算层通过之后才新增的，commit-ready 应该拒绝整个集合。",
    )

    loom["run"](["commit-ready", task, "--semantic-passed"], expected=1)
    assert loom["store"].get_card(topic_id) is None


def test_stop_check_pending_only_processes_current_session_by_default(loom, loom_helpers, monkeypatch, tmp_path):
    task_root = tmp_path / "loom_task"
    real_path = Path

    def redirected_path(value="", *parts):
        path = real_path(value, *parts)
        marker = "/tmp/loom_task"
        text = str(path)
        if text == marker:
            return task_root
        if text.startswith(marker + "/"):
            return task_root / text[len(marker) + 1:]
        return path

    monkeypatch.setattr(loom["cli"], "Path", redirected_path)
    monkeypatch.setattr(loom["cli"].checks, "Path", redirected_path)

    source_id = "llm:session:src:01"
    loom_helpers.import_source(source_id, "sources/07-LLM/session/ch01.md")

    current = loom["task_id"]("current_session")
    other = loom["task_id"]("other_session")
    for task, card_id in ((current, "llm:session:01"), (other, "llm:session:02")):
        task_dir = task_root / task
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "plan.json").write_text(
            json.dumps({
                "task_id": task,
                "task": f"Scout {card_id}",
                "source": source_id,
                "layer": "L2",
                "phase": "scout",
                "skill": "DIGEST",
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        loom["run"]([
            "write-draft", task, card_id, "--type=主题", f"--title={card_id}",
            f"--source={source_id}",
            "--content=这张主题卡用于验证 stop-check-pending 默认只处理当前 session 的 ready task。",
        ])

    monkeypatch.setenv("LOOM_SESSION_ID", "pytest-current-session")
    (task_root / current / ".session_id").write_text("pytest-current-session", encoding="utf-8")
    (task_root / other / ".session_id").write_text("pytest-other-session", encoding="utf-8")
    for task in (current, other):
        (task_root / task / ".ready").write_text("1", encoding="utf-8")

    loom["run"]([
        "stop-check-pending",
        "--current-session=pytest-current-session",
    ], admin=True)

    assert (task_root / current / ".computed_passed.json").exists()
    assert not (task_root / other / ".computed_passed.json").exists()
    aggregate = task_root / ".semantic_sample.json"
    assert aggregate.exists()
    payload = json.loads(aggregate.read_text(encoding="utf-8"))
    assert payload["sample_ids"] == ["llm:session:01"]
    assert [card["card_id"] for card in payload["cards"]] == ["llm:session:01"]
