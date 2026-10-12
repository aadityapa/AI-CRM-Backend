"""Skill-wise question bank (10 Oct 2026): template config, skill pick, the
question plan, the candidate payload, pseudo-code marking, per-skill scores,
reminders and the template archive."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.interview import skill_bank as sb


def _bank(**over):
    cfg = {
        "enabled": True,
        "questions": [
            {"skill": "Python", "kind": "normal", "text": "What is a decorator?", "expected": "Wraps a function."},
            {"skill": "Python", "kind": "normal", "text": "What is a generator?", "expected": "Lazy iterator."},
            {"skill": "Python", "kind": "normal", "text": "Explain the GIL.", "expected": "One thread runs bytecode."},
            {"skill": "SQL", "kind": "normal", "text": "What is an index?", "expected": "Speeds lookups."},
            {"skill": "SQL", "kind": "normal", "text": "INNER vs LEFT JOIN?", "expected": "Left keeps all rows."},
            {"skill": "Linux", "kind": "normal", "text": "What does chmod do?", "expected": "Changes permissions."},
            {"skill": "Python", "kind": "pseudo", "text": "What does this print?", "code": "x = 2\nprint(x * 3)",
             "options": ["5", "6", "23"], "correct": 1},
            {"skill": "SQL", "kind": "pseudo", "text": "What does this print?", "code": "SELECT 1 + 1",
             "options": ["2", "11"], "correct": 0},
            {"skill": "Python", "kind": "coding", "text": "Reverse a string.", "expected": "return s[::-1]",
             "language": "python"},
        ],
    }
    cfg.update(over)
    return cfg


def test_clean_config_validates_each_kind():
    cfg = sb.clean_config(_bank())
    assert cfg["coding_count"] == 1 and cfg["pseudo_count"] == 2 and cfg["pick_min"] == 2
    assert all(q["id"] for q in cfg["questions"])
    with pytest.raises(sb.SkillBankError, match="correct option"):
        sb.clean_config({"enabled": True, "questions": [
            {"skill": "SQL", "kind": "pseudo", "text": "?", "code": "x", "options": ["a", "b"]}]})
    with pytest.raises(sb.SkillBankError, match="at least 2 options"):
        sb.clean_config({"enabled": True, "questions": [
            {"skill": "SQL", "kind": "pseudo", "text": "?", "code": "x", "options": ["a"], "correct": 0}]})
    with pytest.raises(sb.SkillBankError, match="Add at least one question"):
        sb.clean_config({"enabled": True, "questions": []})
    # Switched off, an empty bank is fine (the template keeps its other settings).
    assert sb.clean_config({"enabled": False})["questions"] == []
    assert sb.config_of({"skillBank": {"enabled": False, "questions": _bank()["questions"]}}) is None


def test_pick_offers_mandatory_locked_and_validates_limits():
    cfg = sb.clean_config(_bank())
    opts = sb.pick_options(cfg, ["python", "Kubernetes"])  # Kubernetes has no questions
    assert opts["mandatory"] == ["Python"]
    assert opts["offered"][0] == "Python" and set(opts["offered"]) == {"Python", "SQL", "Linux"}
    assert opts["min"] == 2 and opts["max"] == 3
    assert sb.default_pick(opts) == ["Python", "Linux"]
    # Mandatory skills are always in, whatever the browser sends.
    assert sb.validate_pick(["sql"], opts) == ["Python", "SQL"]
    # HR's own per-skill "mandatory" marks win over the template's required skills.
    cfg2 = sb.clean_config(_bank(mandatory_skills=["SQL"]))
    assert sb.pick_options(cfg2, ["Python"])["mandatory"] == ["SQL"]
    with pytest.raises(sb.SkillBankError, match="at least 2"):
        sb.validate_pick([], opts)
    with pytest.raises(sb.SkillBankError, match="not one of"):
        sb.validate_pick(["Cobol"], opts)


def test_plan_asks_only_picked_skills_and_orders_by_kind():
    cfg = sb.clean_config(_bank())
    plan = sb.build_plan(cfg, ["Python", "SQL"], seed="s1")
    skills = {p["skill"] for p in plan}
    assert skills <= {"Python", "SQL"}
    kinds = [p["kind"] for p in plan]
    assert kinds.count("coding") == 1 and kinds.count("pseudo") == 2 and kinds.count("normal") == 4
    # theory first, the typed questions IN BETWEEN, coding in the middle (11 Oct 2026)
    assert kinds == ["normal", "pseudo", "normal", "coding", "normal", "pseudo", "normal"]
    assert next(p for p in plan if p["kind"] == "coding")["time_limit_sec"] == 600
    # The same seed gives the same interview (a restart cannot reshuffle it).
    assert [p["qid"] for p in sb.build_plan(cfg, ["Python", "SQL"], seed="s1")] == [p["qid"] for p in plan]
    # Repeated wording gets a suffix so the session strings stay distinct.
    texts = sb.session_questions(plan)
    assert len(set(texts)) == len(texts)


def test_public_spec_never_leaks_the_answer():
    cfg = sb.clean_config(_bank())
    pseudo = next(s for s in sb.build_plan(cfg, ["Python", "SQL"], "x") if s["kind"] == "pseudo")
    pub = sb.public_spec(pseudo)
    assert pub["kind"] == "pseudo" and pub["speak"] is False and pub["options"]
    assert "correct" not in pub and "expected" not in pub


def test_insert_followup_shifts_specs():
    from services.interview import conversation

    session = {"questions": ["warm", "q1", "q2"], "meta": {"question_specs": {"1": {"kind": "normal"},
                                                                                "2": {"kind": "coding"}}}}
    conversation.insert_followup(session, 2, "Tell me more about that?")
    assert session["questions"][2] == "Tell me more about that?"
    assert sb.kind_for(session["meta"], 3) == "coding"
    assert sb.spec_for(session["meta"], 2) is None


def test_pseudo_choice_is_marked_without_a_model():
    spec = {"kind": "pseudo", "skill": "Python", "options": ["5", "6"], "correct": 1}
    idx, text = sb.choice_answer(spec, "1")
    assert idx == 1 and text == "Option B: 6"
    with pytest.raises(sb.SkillBankError):
        sb.choice_answer(spec, "7")
    right = sb.pseudo_row(1, spec, text, {"choice": 1})
    wrong = sb.pseudo_row(2, spec, "Option A: 5", None)
    assert right["score"] == 10.0 and wrong["score"] == 0.0
    assert "Option B" in wrong["expected_answer"]


def test_batch_scores_pseudo_without_openai_and_keeps_code(monkeypatch):
    import ai

    monkeypatch.setattr("openai_client.openai_key_configured", lambda *_a, **_k: False)
    specs = [
        {"kind": "pseudo", "skill": "Python", "options": ["5", "6"], "correct": 1},
        {"kind": "coding", "skill": "Python", "expected": "return s[::-1]", "language": "python"},
    ]
    rows = ai.evaluate_per_question_interview_batch(
        ["What does this print?", "Reverse a string."],
        ["Option B: 6", "def rev(s):\n    return s[::-1]"],
        question_specs=specs, choices=[{"choice": 1}, None],
    )
    assert rows[0]["score"] == 10.0 and rows[0]["question_kind"] == "pseudo"
    assert rows[0]["selected_option"] == 1 and rows[0]["correct_option"] == 1
    assert rows[1]["question_kind"] == "coding" and rows[1]["language"] == "python"


def test_skill_scores_come_from_each_skills_own_questions():
    specs = [{"skill": "Python", "kind": "normal"}, {"skill": "Python", "kind": "pseudo"},
             {"skill": "SQL", "kind": "normal"}]
    rows = [{"score": 8.0, "evidence": "wraps a function"}, {"score": 10.0}, {"score": 4.0}]
    out = {r["skill"]: r for r in sb.skill_scores_from_rows(rows, specs, ["Python", "SQL", "Linux"])}
    assert out["Python"]["score"] == 9.0 and out["Python"]["questions"] == 2
    assert out["SQL"]["score"] == 4.0 and out["Linux"]["score"] == 0.0
    # A skipped turn does not count, the same rule as the overall score.
    out2 = {r["skill"]: r for r in sb.skill_scores_from_rows(rows, specs, ["Python"], [False, True, True])}
    assert out2["Python"]["score"] == 10.0
    summary = sb.kind_summary(rows, specs)
    assert summary["pseudo"]["correct"] == 1 and summary["normal"]["questions"] == 2


def test_specs_align_with_the_evaluation_lists():
    meta = {"warmup_indices": [0], "question_specs": {"1": {"kind": "normal"}, "2": {"kind": "coding"}},
            "choices": {"2": {"choice": 0}}}
    assert [s["kind"] for s in sb.specs_for_evaluation(meta, 3, 2)] == ["normal", "coding"]
    assert sb.choices_for_evaluation(meta, 3, 2) == [None, {"choice": 0}]


def test_next_payload_carries_the_screen_but_not_the_answer():
    from candidate.service import next_question_payload

    session = {"questions": ["Please introduce yourself.", "What does this print?"], "answers": ["hi"],
               "current": 1, "meta": {"question_source": "skill_bank", "warmup_indices": [0], "num_q": 1,
                                      "question_specs": {"1": {"kind": "pseudo", "skill": "SQL", "code": "SELECT 1",
                                                               "options": ["1", "2"], "correct": 0,
                                                               "text": "What does this print?",
                                                               "time_limit_sec": 120}}}}
    out = next_question_payload(session)
    assert out["kind"] == "pseudo" and out["options"] == ["1", "2"] and out["speak"] is False
    assert out["answer_time_limit_sec"] == 120 and "correct" not in out


def test_reminder_windows():
    from services.interview_reminders import KIND_DAY, KIND_HOUR, reminder_kind, reminder_message

    assert reminder_kind(50, 3 * 24 * 60) == KIND_HOUR
    assert reminder_kind(50, 60) is None           # booked an hour ahead — the invite is the reminder
    assert reminder_kind(20 * 60, 3 * 24 * 60) == KIND_DAY
    assert reminder_kind(20 * 60, 8 * 60) is None   # booked the same day — no "tomorrow" mail
    assert reminder_kind(-5, 3 * 24 * 60) is None   # the slot has passed
    msg = reminder_message(candidate_name="Asha", position="Embedded Engineer",
                           when_text="Monday, 12 October 2026 at 4:00 PM IST",
                           invite_url="https://x.test/?invite=abc", kind=KIND_HOUR)
    assert "1 hour" in msg["subject"] and "https://x.test/?invite=abc" in msg["text"]


def test_template_archive_follows_the_opportunity(monkeypatch):
    from services import template_archive as ta

    monkeypatch.setattr(ta, "opportunity_stages", lambda codes: {"C-1": "Closed_Lost", "C-2": "On_Hold"})
    jobs = ta.annotate([{"jobId": "a", "opportunityId": "C-1"}, {"jobId": "b", "opportunityId": "C-2"},
                        {"jobId": "c", "opportunityId": ""}])
    assert [j["archived"] for j in jobs] == [True, False, False]
    assert "Closed Lost" in jobs[0]["archived_reason"]
    assert ta.is_archiving_stage("Archived") and not ta.is_archiving_stage("Active")


def test_l1_status_refuses_an_archived_template(monkeypatch):
    from types import SimpleNamespace

    from services import ai_interview_bridge as bridge

    monkeypatch.setattr(bridge, "resolve_l1_template_job_id", lambda *a, **k: "job-1")
    monkeypatch.setattr(bridge, "_open_template_request", lambda *a, **k: None)
    opp = SimpleNamespace(id=7, opp_id="C-2026-00099", pipeline_stage="Closed_Won")
    status = bridge.l1_template_status(None, opp, None)
    assert status["ready"] is False and status["state"] == "archived"
    assert status["code"] == bridge.TEMPLATE_ARCHIVED_CODE and "Reopen" in status["reason"]


# --------------------------------------------------------------- main.py integration

import asyncio
import importlib
from types import SimpleNamespace


@pytest.fixture()
def main_mod(monkeypatch):
    main = importlib.import_module("main")
    sessions = {}
    monkeypatch.setattr(main, "_require_user", lambda r, roles: ({"sub": "c@t", "role": "candidate"}, None))
    monkeypatch.setattr(main, "_session_key_from_payload", lambda p: "sk-bank")
    monkeypatch.setattr(main, "_session_key_from_session", lambda s: "sk-bank")
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_interview_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "remember_asked_question", lambda *a, **k: None)
    monkeypatch.setattr(main, "sessions", sessions)
    import services.tts_prewarm as tp
    monkeypatch.setattr(tp, "prewarm_tts", lambda *a, **k: False)
    return main, sessions


def _job():
    return {"jobId": "job-bank", "jobTitle": "Embedded Engineer", "requiredSkills": ["Python"],
            "optionalSkills": ["SQL"], "questionType": "dynamic", "weights": {"skillBank": _bank()},
            "jdText": "Python and SQL"}


def test_bootstrap_builds_the_bank_plan_without_any_model(main_mod, monkeypatch):
    main, sessions = main_mod
    monkeypatch.setattr(main, "get_job_template", lambda target, jid: _job())
    monkeypatch.setattr(main, "get_interview_progress_by_invite", lambda *a, **k: None)
    monkeypatch.setattr(main, "_try_restore_invite_session", lambda *a, **k: None)
    monkeypatch.setattr(main, "_generate_interview_questions", lambda **k: pytest.fail("no generation"))
    notes = main._pack_invite_config_into_notes("", {"job_id": "job-bank", "timing_mode": "count"}) \
        if hasattr(main, "_pack_invite_config_into_notes") else ""
    sched = {"notes": notes, "candidate_name": "Asha", "candidate_email": "a@x.in",
             "scheduled_at_local": "2026-10-12 16:00"}
    out = main._bootstrap_invite_interview_session("tok-bank-1", sched)
    assert out.get("status") == "ok", out
    s = sessions["inv:tok-bank-1"]
    meta = s["meta"]
    assert meta["question_source"] == "skill_bank" and meta["adaptive_next_question"] is False
    assert meta["skill_bank"]["mandatory"] == ["Python"] and meta["skill_bank"]["pick_confirmed"] is False
    assert s["questions"][0] == "Please introduce yourself."
    pick = main._skill_pick_payload(s)
    assert pick and pick["min"] == 2 and "Python" in pick["offered"]
    assert main._practice_payload(s)["kinds"] == ["normal", "pseudo", "coding"]


class _Req(SimpleNamespace):
    def __init__(self, body):
        super().__init__()
        self._body = body

    async def json(self):
        return self._body


def test_skill_selection_rebuilds_the_plan_and_answer_marks_the_choice(main_mod, monkeypatch):
    main, sessions = main_mod
    monkeypatch.setattr(main, "get_job_template", lambda target, jid: _job())
    cfg = sb.clean_config(_bank())
    pick = sb.pick_options(cfg, ["Python"])
    specs = sb.build_plan(cfg, ["Python", "Linux"], "seed")
    s = {"questions": ["Please introduce yourself.", *sb.session_questions(specs)], "answers": [], "current": 0,
         "meta": {"question_source": "skill_bank", "warmup_indices": [0], "job_id": "job-bank",
                  "question_seed": "seed", "interview_id": "iv-1"}}
    main._stamp_skill_bank(s["meta"], {**pick, "picked": ["Python", "Linux"]}, specs, offset=1)
    sessions["sk-bank"] = s

    bad = asyncio.run(main.candidate_skill_selection(_Req({"skills": ["Cobol"]})))
    assert bad.status_code == 400
    out = asyncio.run(main.candidate_skill_selection(_Req({"skills": ["SQL"]})))
    assert out["skills"] == ["Python", "SQL"] and out["plan"]["pseudo"] == 2
    assert s["meta"]["jd_skills"] == ["Python", "SQL"] and s["meta"]["skill_bank"]["pick_confirmed"]
    assert {sp["skill"] for sp in s["meta"]["question_specs"].values()} <= {"Python", "SQL"}
    assert main._skill_pick_payload(s) is None

    # Walk to the first pseudo-code question and answer it by option index.
    s["current"] = next(int(k) for k, v in s["meta"]["question_specs"].items() if v["kind"] == "pseudo")
    s["answers"] = ["x"] * s["current"]
    spec = s["meta"]["question_specs"][str(s["current"])]
    turn = s["current"]
    calls = []
    monkeypatch.setattr(main, "_schedule_turn_evaluation", lambda *a, **k: calls.append(1))
    res = main.answer(SimpleNamespace(), ans="ignored", action="send", choice=str(spec["correct"]))
    assert s["answers"][turn].startswith(f"Option {'ABCDEF'[spec['correct']]}:")
    assert s["meta"]["choices"][str(turn)]["correct"] is True
    assert calls == [], "a skill-bank interview makes no per-answer model call"
    assert res["status"] == "ok"
    refused = main.answer(SimpleNamespace(), ans="", action="send", choice="9")
    # the next question may be pseudo again or another kind; an out-of-range option is refused for pseudo only
    nxt_spec = s["meta"]["question_specs"].get(str(s["current"])) or {}
    if nxt_spec.get("kind") == "pseudo":
        assert refused.status_code == 400


# ------------------------------------------------------- 11 Oct 2026 follow-ups

def test_interleave_never_opens_or_closes_on_a_typed_question():
    N = [{"k": "n"}] * 6
    T = [{"k": "t1"}, {"k": "t2"}, {"k": "t3"}]
    out = [x["k"] for x in sb.interleave(N, T)]
    assert out[0] == "n" and out[-1] == "n" and out.count("n") == 6
    assert [x for x in out if x != "n"] == ["t1", "t2", "t3"]
    assert "n,t1" in ",".join(out) and "t1,t2" not in ",".join(out)
    assert [x["k"] for x in sb.interleave([], T)] == ["t1", "t2", "t3"]
    assert [x["k"] for x in sb.interleave(N[:2], T[:1])] == ["n", "t1", "n"]


def test_pseudo_partly_right_option_earns_partial_credit():
    cfg = sb.clean_config(_bank(questions=[
        {"skill": "SQL", "kind": "pseudo", "text": "Rows?", "code": "SELECT ...", "options": ["3", "4", "0"],
         "correct": 1, "partial": [0, 1, 9], "partial_credit": 40}]))
    q = cfg["questions"][0]
    assert q["partial"] == [0] and q["partial_credit"] == 40  # the correct one and junk dropped
    spec = sb.to_spec(q, cfg)
    assert sb.choice_credit(spec, 1) == 1.0 and sb.choice_credit(spec, 0) == 0.4 and sb.choice_credit(spec, 2) == 0
    row = sb.pseudo_row(0, spec, "Option A: 3", {"choice": 0})
    assert row["score"] == 4.0 and row["summary"].startswith("Partly right")
    meta = {"question_specs": {"3": spec}}
    assert sb.record_choice(meta, 3, 0) is False and meta["choices"]["3"]["credit"] == 0.4
    assert "partial" not in sb.public_spec(spec) and "partial_credit" not in sb.public_spec(spec)
    deco = sb.decorate_row(dict(row), spec, {"choice": 0})
    assert deco["partial_options"] == [0] and deco["partial_credit"] == 40
    summary = sb.kind_summary([row], [spec])
    assert summary["pseudo"]["partial"] == 1 and summary["pseudo"]["correct"] == 0


def test_coding_verdict_prefers_the_model_then_the_score():
    assert sb.coding_verdict({"verdict": "Partially Correct", "score": 9}) == "partially_correct"
    assert sb.coding_verdict({"verdict": "wrong"}) == "incorrect"
    assert sb.coding_verdict({"score": 8}) == "correct"
    assert sb.coding_verdict({"score": 5}) == "partially_correct"
    assert sb.coding_verdict({"score": 1}) == "incorrect"
    spec = {"kind": "coding", "skill": "Python", "code": "", "language": "python"}
    assert sb.decorate_row({"score": 7.5}, spec)["verdict"] == "correct"


def test_missing_normals_and_ai_fill_parse():
    from services.interview import skill_bank_fill as fill
    cfg = sb.clean_config(_bank())
    assert sb.missing_normals(cfg, ["Python", "Linux"]) == [("Linux", 1)]
    assert sb.missing_normals(sb.clean_config(_bank(ai_fill=False)), ["Linux"]) == []
    data = {"questions": [
        {"skill": "linux", "question": "How do you find which process holds a port", "expected_answer": "ss/lsof"},
        {"skill": "Linux", "question": "Second one?"},           # over the number asked
        {"skill": "Cobol", "question": "Not asked?"},            # skill not asked for
        {"skill": "Linux", "question": "What does chmod do?"},   # repeats the bank
    ]}
    out = fill.parse_fill(data, [("Linux", 1)], avoid=["What does chmod do?"])
    assert list(out) == ["linux"] and len(out["linux"]) == 1
    q = out["linux"][0]
    assert q["text"].endswith("?") and q["source"] == "ai" and q["difficulty"] == "medium"
    assert q["kind"] == "normal" and q["expected"] == "ss/lsof"
    plan = sb.build_plan(cfg, ["Python", "Linux"], "s", extra_normals=out)
    assert sum(1 for p in plan if p.get("source") == "ai") == 1
    assert fill.parse_fill(None, [("Linux", 1)], []) == {}


def test_skill_selection_tops_up_a_short_skill_with_ai_questions(main_mod, monkeypatch):
    main, sessions = main_mod
    monkeypatch.setattr(main, "get_job_template", lambda target, jid: _job())
    cfg = sb.clean_config(_bank())
    pick = sb.pick_options(cfg, ["Python"])
    specs = sb.build_plan(cfg, ["Python", "SQL"], "seed")
    s = {"questions": ["Please introduce yourself.", *sb.session_questions(specs)], "answers": [], "current": 0,
         "meta": {"question_source": "skill_bank", "warmup_indices": [0], "job_id": "job-bank",
                  "question_seed": "seed", "interview_id": "iv-2", "job_title": "Embedded Engineer"}}
    main._stamp_skill_bank(s["meta"], {**pick, "picked": ["Python", "SQL"]}, specs, offset=1)
    sessions["sk-bank"] = s
    asked = []

    def fake_fill(meta, needs, avoid):
        asked.append(list(needs))
        return {"linux": [{"id": "ai-1", "skill": "Linux", "kind": "normal", "text": "How do you read syslog?",
                           "expected": "journalctl", "difficulty": "medium", "source": "ai"}]}

    monkeypatch.setattr(main.interview_skill_bank_fill, "generate_fill", fake_fill)
    out = asyncio.run(main.candidate_skill_selection(_Req({"skills": ["Linux"]})))
    assert asked == [[("Linux", 1)]]
    assert out["ai_filled"] == 1
    texts = [sp["text"] for sp in s["meta"]["question_specs"].values()]
    assert "How do you read syslog?" in texts
    assert s["meta"]["skill_bank_fill"]["linux"][0]["id"] == "ai-1"
    # A second pick of the same skill re-uses the cached top-up — no second model call.
    s["meta"]["skill_bank"]["pick_confirmed"] = False
    asyncio.run(main.candidate_skill_selection(_Req({"skills": ["Linux"]})))
    assert len(asked) == 1


def test_only_rmg_or_gm_author_templates():
    import inspect
    import main
    src = inspect.getsource(main)
    assert src.count("_enforce_template_author(request)") == 5
    assert '_enforce_crm_roles(request, "RMG", tab="iv:templates", mode="edit")  # Template' not in src
    helper = inspect.getsource(main._enforce_template_author)
    assert "_screens_as_rmg_for_request" in helper and "HR" in helper


# ------------------------------------------- 12 Oct 2026: languages + progress strip

def test_coding_question_keeps_allowed_languages_and_starters():
    q = sb.clean_question({"skill": "Embedded C", "kind": "coding", "text": "Toggle bit 3 of PORTA.",
                           "language": "embedded_c", "languages": ["c", "CAPL", "cobol", "c"],
                           "starters": {"c": "void f(void) {}", "python": "def f(): pass", "capl": " "}}, 1)
    # The default language joins the allowed list; unknown and repeated ones are dropped;
    # a starter for a language the question does not allow (python) or an empty one is dropped.
    assert q["languages"] == ["embedded_c", "c", "capl"]
    assert q["starters"] == {"c": "void f(void) {}"}
    anyq = sb.clean_question({"skill": "Java", "kind": "coding", "text": "FizzBuzz."}, 2)
    assert anyq["language"] == "any" and "languages" not in anyq
    spec = sb.to_spec(q, sb.clean_config({"enabled": False}) | {"coding_minutes": 10, "pseudo_minutes": 2})
    pub = sb.public_spec(spec)
    assert pub["languages"] == ["embedded_c", "c", "capl"] and pub["starters"] == {"c": "void f(void) {}"}
    assert "expected" not in pub
    # A question that allows any language offers every supported one.
    assert sb.public_spec(sb.to_spec(anyq, {"coding_minutes": 10, "pseudo_minutes": 2}))["languages"] == list(sb.CODE_LANGUAGES)
    for lang in ("embedded_c", "cpp", "java", "kotlin", "python", "capl", "matlab"):
        assert lang in sb.CODE_LANGUAGES and lang in sb.LANGUAGE_LABELS


def test_candidate_language_reaches_the_scoring_prompt_and_report():
    spec = {"kind": "coding", "skill": "Java", "text": "Reverse.", "language": "any",
            "languages": ["java", "kotlin"], "starters": {"kotlin": "fun rev(s: String) = s"}, "expected": "x"}
    assert sb.chosen_language(spec, "KOTLIN") == "kotlin"
    assert sb.chosen_language(spec, "python") is None  # not allowed by this question
    meta = {"question_source": "skill_bank", "question_specs": {"3": spec}}
    sb.record_language(meta, 3, "kotlin")
    extra = sb.evaluation_extra(meta["question_specs"]["3"])
    assert extra["language"].startswith("Kotlin") and extra["starter_code"] == "fun rev(s: String) = s"
    row = sb.decorate_row({"score": 8}, meta["question_specs"]["3"])
    assert row["language"] == "kotlin" and row["verdict"] == "correct"


def test_question_map_lists_kinds_without_the_warmup_or_answers():
    cfg = sb.clean_config(_bank())
    specs = sb.build_plan(cfg, ["Python", "SQL"], "seed")
    meta = {"question_source": "skill_bank", "warmup_indices": [0]}
    sb.stamp_specs(meta, specs, offset=1)
    qmap = sb.question_map(meta, len(specs) + 1)
    assert [m["kind"] for m in qmap] == [s["kind"] for s in specs]
    assert all(set(m) == {"kind", "skill"} for m in qmap)
    assert sb.question_map({"question_source": "dynamic"}, 5) == []

    from candidate.service import next_question_payload
    session = {"questions": ["Please introduce yourself.", *sb.session_questions(specs)], "answers": [],
               "current": 0, "meta": dict(meta, num_q=len(specs))}
    out = next_question_payload(session)
    assert out["question_map"] == qmap


def test_answer_records_the_coding_language():
    import main as main_mod_
    src = open(main_mod_.__file__, encoding="utf-8").read()
    assert "code_language: str = Form(\"\")" in src
    assert "record_language(" in src and "chosen_language(typed_spec, code_language)" in src
