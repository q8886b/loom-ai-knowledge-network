from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"
DESIGN_005 = REPO_ROOT / "docs" / "design" / "005-layered-redesign-harness.md"


def test_digest_phase_contract_is_enforced_by_write_draft_and_stop_check(loom, loom_helpers):
    source_id = "llm:contract:src:01"
    topic_id = "llm:contract:01"
    loom_helpers.import_source(source_id, "sources/07-LLM/contract/ch01.md")

    missing_phase = loom["task_id"]("missing_phase")
    loom["write_plan"](missing_phase, task="L2 without phase", source=source_id, layer="L2", skill="DIGEST")
    loom["run"]([
        "write-draft",
        missing_phase,
        "llm:contract:01x",
        "--type=主题",
        "--title=缺 phase",
        f"--source={source_id}",
        "--content=这张卡故意放在缺少 phase 的 L2 计划下，应该被两阶段管线的前置合约拒绝。",
    ], expected=1)

    scout_bad = loom["task_id"]("scout_bad_type")
    loom["write_plan"](
        scout_bad,
        task="Scout bad type",
        source=source_id,
        layer="L2",
        phase="scout",
        skill="DIGEST",
    )
    loom["run"]([
        "write-draft",
        scout_bad,
        "llm:contract:01a",
        "--type=判断",
        "--title=Scout 非主题",
        f"--source={source_id}",
        "--content=Scout 阶段只负责建立主题卡，这张判断卡应该被 write-draft 前置拦截。",
    ], expected=1)

    loom_helpers.write_committed_topic(source_id, topic_id)

    deep_topic = loom["task_id"]("deep_topic")
    loom["write_plan"](
        deep_topic,
        task="Deep writes topic",
        source=source_id,
        layer="L2",
        phase="deep",
        topic_card=topic_id,
        skill="DIGEST",
    )
    loom["run"]([
        "write-draft",
        deep_topic,
        "llm:contract:01b",
        "--type=主题",
        "--title=Deep 主题",
        f"--source={source_id}",
        f"--links={topic_id}",
        "--content=Deep 阶段不应该再建立主题卡，因为主题卡必须由 Scout 先行建立并提交。",
    ], expected=1)

    deep_missing_topic_link = loom["task_id"]("deep_missing_topic_link")
    loom["write_plan"](
        deep_missing_topic_link,
        task="Deep missing topic link",
        source=source_id,
        layer="L2",
        phase="deep",
        topic_card=topic_id,
        skill="DIGEST",
    )
    loom["run"]([
        "write-draft",
        deep_missing_topic_link,
        "llm:contract:01c",
        "--type=判断",
        "--title=未链接主题的判断",
        f"--source={source_id}",
        "--content=这张判断卡没有链接主题卡，单卡可以写入 draft，但 stop-check 整批校验必须拒绝。",
    ])
    loom["run"](["mark-ready", deep_missing_topic_link])
    loom["run"](["stop-check", deep_missing_topic_link], admin=True, expected=2)
    rejected = json.loads(
        (Path("/tmp/loom_task") / deep_missing_topic_link / ".rejected.json").read_text(encoding="utf-8")
    )
    assert rejected["status"] == "rejected"
    assert any("topic" in f["check"] or "主题" in f["reason"] for f in rejected["failures"])


def test_commit_ready_requires_semantic_gate_and_rejects_modified_drafts(loom, loom_helpers):
    source_id = "llm:gate:src:01"
    topic_id = "llm:gate:01"
    loom_helpers.import_source(source_id, "sources/07-LLM/gate/ch01.md")

    task = loom["task_id"]("commit_gate")
    loom["write_plan"](
        task,
        task="Commit gate",
        source=source_id,
        layer="L2",
        phase="scout",
        skill="DIGEST",
    )
    loom["run"]([
        "write-draft",
        task,
        topic_id,
        "--type=主题",
        "--title=提交闸门主题",
        f"--source={source_id}",
        "--content=这张主题卡用于验证 commit-ready 必须等待计算层通过和语义自检声明，且不能接受通过后被修改的 draft。",
    ])

    loom["run"](["commit-ready", task, "--semantic-passed"], expected=1)
    loom["run"](["mark-ready", task])
    loom["run"](["stop-check", task], admin=True, expected=2)
    loom["run"](["commit-ready", task], expected=1)

    draft_path = Path("/tmp/loom_task") / task / "drafts" / f"{topic_id}.md"
    draft_path.write_text(
        draft_path.read_text(encoding="utf-8") + "\n计算层通过后追加内容，应该触发 mtime 防篡改。\n",
        encoding="utf-8",
    )
    loom["run"](["commit-ready", task, "--semantic-passed"], expected=1)
    assert loom["store"].get_card(topic_id) is None


def test_think_stop_check_records_l4_warning_without_blocking_computed_state(loom, loom_helpers):
    source_id = "llm:thinkpipe:src:01"
    topic_id = "llm:thinkpipe:01"
    l2_id = "llm:thinkpipe:01a"
    loom_helpers.import_source(source_id, "sources/07-LLM/thinkpipe/ch01.md")
    loom_helpers.write_committed_topic(source_id, topic_id)

    deep = loom["task_id"]("thinkpipe_deep")
    loom["write_plan"](
        deep,
        task="Deep support card",
        source=source_id,
        layer="L2",
        phase="deep",
        topic_card=topic_id,
        skill="DIGEST",
    )
    loom["run"]([
        "write-draft",
        deep,
        l2_id,
        "--type=判断",
        "--title=支撑 THINK 的 L2 判断",
        f"--source={source_id}",
        f"--links={topic_id}",
        "--content=这张 L2 判断为 THINK 管线提供材料锚点，生成层必须从已消化的材料结论出发。",
    ])
    loom_helpers.commit_semantic_ready(deep)

    think = loom["task_id"]("think_l4_warn")
    loom["write_plan"](think, task="THINK without L4 refs", layer="L3", skill="THINK")
    loom["run"]([
        "write-draft",
        think,
        "llm:1",
        "--layer=L3",
        "--type=判断",
        "--title=生成层判断",
        f"--links={l2_id}",
        "--content=这张 L3 判断从已入库 L2 卡出发形成新结论，用来验证 THINK 任务零 L4 引用时只发出 WARN 而不阻断计算层通过。",
    ])
    loom["run"](["mark-ready", think])
    loom["run"](["stop-check", think], admin=True, expected=2)

    computed = json.loads((Path("/tmp/loom_task") / think / ".computed_passed.json").read_text(encoding="utf-8"))
    rejected = json.loads((Path("/tmp/loom_task") / think / ".rejected.json").read_text(encoding="utf-8"))
    assert computed["status"] == "computed_passed"
    assert rejected["status"] == "semantic_required"
    reason = rejected["failures"][0]["reason"]
    assert "[WARN] THINK 任务全程零 L4 引用" in reason
    assert "commit-ready" in reason


def test_skill_text_keeps_core_pipeline_contracts_in_sync_with_005():
    design = DESIGN_005.read_text(encoding="utf-8")
    core = (SKILLS_DIR / "_loom_core.md").read_text(encoding="utf-8")
    digest = (SKILLS_DIR / "loom-digest" / "SKILL.md").read_text(encoding="utf-8")
    think = (SKILLS_DIR / "loom-think" / "SKILL.md").read_text(encoding="utf-8")
    use = (SKILLS_DIR / "loom-use" / "SKILL.md").read_text(encoding="utf-8")

    for required in [
        "phase=scout",
        "phase=deep",
        "topic_card",
        "DIGEST 完全 L4-blind",
        "loom orient",
        "bin/loom read-cards <id> [<id>...]",
        "commit-ready --semantic-passed",
        ".semantic_sample.json",
    ]:
        assert required in design

    for mode_skill in (digest, think, use):
        assert "skills/_loom_core.md" in mode_skill
        assert "type_match" in mode_skill
        assert "single_unit" in mode_skill
        assert "genuine_digest" in mode_skill
        assert "self_contained" in mode_skill
        assert "loom commit-ready" in mode_skill
        assert "--semantic-passed" in mode_skill

    assert "DIGEST 完全 L4-blind" in digest
    assert "不调 read-l4-index" in digest
    assert "不 link L4" in digest
    assert "topic_card" in digest

    for thinking_skill in (think, use):
        assert "loom orient" in thinking_skill
        assert "--task-id $TASK_ID" in thinking_skill
        assert "标题对上 ≠ 模式适用" in thinking_skill
        assert "探索铁律（必须）" in thinking_skill
        assert "地图 → 问题" in thinking_skill
        assert "问题 → 网络" in thinking_skill
        assert "语义饱和" in thinking_skill
        assert "彼此真正不同的多种结构假设" in thinking_skill
        assert "检索命中只产生候选" in thinking_skill
        assert "--mode=vector" in thinking_skill

    assert "未经明确触发，不写 draft、不 mark-ready、不 commit-ready" in use
    assert "入库只能走 `loom commit-ready <task_id> --semantic-passed`" in core
    assert "禁止为满足 ID 或门禁创建空目录父卡" in core
