from __future__ import annotations

import json
from pathlib import Path


def _first_proposal(task_id: str) -> Path:
    proposals = sorted((Path("/tmp/loom_task") / task_id / "staging").glob("*.json"))
    assert len(proposals) == 1
    return proposals[0]


def _commit_test_l4(loom, loom_helpers, card_id: str = "gen:1") -> tuple[str, str, str]:
    _llm_source, _llm_topic, llm_l2 = loom_helpers.write_committed_l2("llm", "l4obs")
    _med_source, _med_topic, med_l2 = loom_helpers.write_committed_l2("med", "l4obs")
    task = loom["task_id"]("commit_l4")
    loom["run"]([
        "propose-l4", task, card_id, "--title=跨域测试模式", "--type=模式",
        f"--related={llm_l2},{med_l2}",
        "--content=[探索期] 这个模式同时锚定 LLM 与医学两个领域，用来验证 L4 提案、引用 trace 和审核状态机。",
    ])
    proposal = _first_proposal(task)
    loom["run"](["proposal-decision", str(proposal), "--decision=approved"], admin=True)
    loom["run"](["commit-l4", str(proposal)], admin=True)
    return card_id, llm_l2, med_l2


def test_read_cards_records_trace_l4_refs_and_l1_lightweight_payload(loom, loom_helpers, capsys):
    source_id = "llm:observe:src:01"
    loom_helpers.import_source(
        source_id,
        "sources/07-LLM/observe/ch01.md",
        text="# 可观测材料\n\n这是一段很长的原文内容，用于验证 read-cards 对 L1 只返回 snippet 而不是完整 content。",
    )
    l4_id, _llm_l2, _med_l2 = _commit_test_l4(loom, loom_helpers)
    capsys.readouterr()

    task = loom["task_id"]("read_trace")
    loom["run"](["read-cards", source_id, l4_id, "--task-id", task])
    payload = json.loads(capsys.readouterr().out)

    by_id = {card["id"]: card for card in payload["cards"]}
    assert "snippet" in by_id[source_id]
    assert "content_size" in by_id[source_id]
    assert "content" not in by_id[source_id]
    assert by_id[l4_id]["content"].startswith("[探索期]")

    trace_file = Path("/tmp/loom_task") / task / ".read_trace.jsonl"
    trace_lines = trace_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(trace_lines) == 1
    trace = json.loads(trace_lines[0])
    assert trace["command"] == "read-cards"
    assert set(trace["card_ids"]) == {source_id, l4_id}

    l4_refs = json.loads((Path("/tmp/loom_task") / task / ".l4_refs").read_text(encoding="utf-8"))
    assert l4_refs == [l4_id]

    assert loom["store"].get_card(source_id)["use_count"] == 1
    loom["run"](["read-source", source_id])
    capsys.readouterr()
    assert loom["store"].get_card(source_id)["use_count"] == 2


def test_search_bumps_search_count_but_skim_does_not_bump_use_count(loom, loom_helpers, capsys):
    _source_id, _topic_id, l2_id = loom_helpers.write_committed_l2("llm", "searchobs")
    capsys.readouterr()
    before = loom["store"].get_card(l2_id)

    loom["run"](["skim", l2_id])
    capsys.readouterr()
    after_skim = loom["store"].get_card(l2_id)
    assert after_skim["use_count"] == before["use_count"]

    loom["run"](["search", "可复用认知单元", "--mode=fts", "--top=5"])
    capsys.readouterr()
    after_search = loom["store"].get_card(l2_id)
    assert after_search["search_count"] >= 1


def test_think_stop_check_reports_l4_refs_when_read_trace_contains_l4(loom, loom_helpers):
    l4_id, llm_l2, _med_l2 = _commit_test_l4(loom, loom_helpers)

    think = loom["task_id"]("think_with_l4_ref")
    loom["write_plan"](think, task="THINK with L4 refs", layer="L3", skill="THINK")
    loom["run"](["read-cards", l4_id, "--task-id", think])
    loom["run"]([
        "write-draft", think, "llm:7", "--layer=L3", "--type=判断",
        "--title=使用 L4 的生成判断", f"--links={llm_l2}",
        "--content=这张 L3 判断已经在任务 trace 中读取过 L4 模式，因此 stop-check 应该报告引用统计而不是零引用 WARN。",
    ])
    loom["run"](["mark-ready", think])
    loom["run"](["stop-check", think], admin=True, expected=2)

    reason = loom_helpers.rejected_payload(think)["failures"][0]["reason"]
    assert "[L4 引用统计]" in reason
    assert "[WARN]" not in reason
    assert l4_id in reason


def test_l4_proposal_requires_cross_domain_and_human_approval(loom, loom_helpers):
    _source_id, _topic_id, llm_l2 = loom_helpers.write_committed_l2("llm", "proposal")
    task = loom["task_id"]("proposal_negative")

    loom["run"]([
        "propose-l4", task, "gen:81", "--title=单领域提案", "--type=模式",
        f"--related={llm_l2}",
        "--content=[探索期] 这个提案只锚定单一领域，应该在 proposal 阶段就被机器校验拒绝。",
    ], expected=2)
    assert "l4_links_lower" in loom_helpers.reject_check_ids(task)

    l4_id, _llm_l2, _med_l2 = _commit_test_l4(loom, loom_helpers, "gen:82")
    pending_task = loom["task_id"]("pending_l4")
    _source2, _topic2, llm2 = loom_helpers.write_committed_l2("llm", "proposalb")
    _source3, _topic3, med2 = loom_helpers.write_committed_l2("med", "proposalb")
    loom["run"]([
        "propose-l4", pending_task, "gen:83", "--title=待审核提案", "--type=模式",
        f"--related={llm2},{med2}",
        "--content=[探索期] 这个提案机器校验通过但仍处于 pending，commit-l4 必须等待用户审核批准。",
    ])
    pending = _first_proposal(pending_task)
    loom["run"](["commit-l4", str(pending)], admin=True, expected=1)
    loom["run"](["proposal-decision", str(pending), "--decision=approved"], admin=True)
    loom["run"](["commit-l4", str(pending)], admin=True)
    assert loom["store"].get_card("gen:83")["layer"] == "L4"
    loom["run"](["proposal-decision", str(pending), "--decision=rejected"], admin=True, expected=1)

    assert loom["store"].get_card(l4_id)["layer"] == "L4"


def test_propose_card_edit_runs_machine_checks_before_human_review(loom, loom_helpers):
    l4_id, _llm_l2, _med_l2 = _commit_test_l4(loom, loom_helpers, "gen:9")
    task = loom["task_id"]("bad_edit")
    loom["run"]([
        "propose-card-edit", task, l4_id, "--type=修正",
        "--content=这个编辑故意移除 L4 第一段成熟度标记，因此应该在 staging 之前被机器校验拒绝。",
    ], expected=2)
    assert "l4_index_format" in loom_helpers.reject_check_ids(task)

    good = loom["task_id"]("good_edit")
    loom["run"]([
        "propose-card-edit", good, l4_id, "--type=补充",
        "--content=[探索期] 这个编辑保留 L4 成熟度标记，并补充模式的适用边界，应该能进入待审核 staging。",
    ])
    proposal = _first_proposal(good)
    loom["run"](["apply-card-edit", str(proposal)], admin=True, expected=1)
    loom["run"](["proposal-decision", str(proposal), "--decision=approved"], admin=True)
    loom["run"](["apply-card-edit", str(proposal)], admin=True)
    assert "适用边界" in loom["store"].get_card(l4_id)["content"]
