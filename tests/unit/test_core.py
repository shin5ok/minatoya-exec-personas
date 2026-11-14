"""ユニットテスト（LLM を呼ばない部分）。"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from app import config, conversation
from app.knowledge import get_bundle
from app.preparation import (
    PrepRequest,
    _fallback_library,
    chair_for,
    default_state,
    scene_for,
)

ROOT = config.ROOT


def test_bundle_loads_personas_and_concepts() -> None:
    bundle = get_bundle()
    assert list(bundle.personas) == ["president", "cto", "cio"]
    assert len(bundle.open_issues) == 8
    # OKF: index.md / log.md はコンセプトとして読まない
    assert all(not c.path.name.startswith(("index", "log")) for c in bundle.concepts.values())
    for pid, info in bundle.personas.items():
        assert info.avatar.endswith(f"{pid}.webp")
        assert (ROOT / "frontend" / info.avatar.removeprefix("/static/")).exists()


def test_always_documents_and_render() -> None:
    bundle = get_bundle()
    always = bundle.always_ids()
    assert "company/facts" in always and "company/06_open_issues" in always
    facts = bundle.render("company/facts")
    assert "```yaml" in facts and "open_issues" in facts  # Dataset は resource の YAML を添える
    capped = bundle.render_many(always, max_chars_total=5000)
    assert len(capped) <= 5500


def test_ids_for_issues_filters_speeches_by_persona() -> None:
    bundle = get_bundle()
    ids = bundle.ids_for_issues(["tide_budget"], ["cio"])
    assert "company/05_it_landscape" in ids
    assert not any(i.startswith("personas/cto/") for i in ids)


@pytest.mark.parametrize("pid", ["president", "cto", "cio"])
@pytest.mark.parametrize("scene", ["meeting", "one_on_one", "external"])
def test_persona_prompt_has_catchphrase_rules(pid: str, scene: str) -> None:
    prompt = get_bundle().persona_prompt(pid, scene)
    assert "## 口癖" in prompt
    assert "直前の自分の発言で使った口癖は、次の発言では使わない" in prompt
    profile = get_bundle().profiles[pid]
    # その場面で使わない口癖は、黙って除外されている
    for c in profile["catchphrases"]:
        if scene not in c["scenes"]:
            assert f"「{c['phrase']}」" not in prompt.split("## 口癖")[1].split("## 判断")[0]


def test_build_prompt_cli_runs() -> None:
    out = subprocess.run(
        [sys.executable, "scripts/build_prompt.py", "cto", "--scene", "one_on_one"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    assert out.startswith("# あなたは「真壁 玲奈」")


def test_validate_and_okf_lint_pass() -> None:
    for script in (["scripts/validate.py"], ["scripts/okf.py", "lint"]):
        res = subprocess.run([sys.executable, *script], cwd=ROOT, capture_output=True, text=True)
        assert res.returncode == 0, res.stdout + res.stderr


def test_mode_scene_and_chair() -> None:
    assert PrepRequest(persona_ids=["cto"]).mode == "dialog"
    assert PrepRequest(persona_ids=["cto", "cio"]).mode == "meeting"
    assert scene_for("dialog") == "one_on_one" and scene_for("meeting") == "meeting"
    assert chair_for(["cto", "president"]) == "president"
    assert chair_for(["cio", "cto"]) == "cio"


def test_fallback_library_matches_keywords() -> None:
    lib = _fallback_library(get_bundle(), PrepRequest(persona_ids=["cio"], theme="TIDEの38億円の追加予算"))
    assert lib.issues == ["tide_budget"]
    assert lib.doc_ids


def test_cue_kind() -> None:
    assert conversation.cue_kind("こんにちは") is None
    assert conversation.cue_kind(conversation.CUE_TEXT["wrapup"]) == "wrapup"
    assert conversation.cue_kind(conversation.CUE_TEXT["final"] + "追加") == "final"
    assert conversation.cue_kind(conversation.CUE_TEXT["idle"]) == "idle"
    assert conversation.cue_kind(conversation.CUE_PREFIX + "その他") == "other"


def test_format_remaining() -> None:
    now = time.time()
    assert conversation.format_remaining({"duration_sec": 0}) == "時間の制限なし"
    assert "挨拶から計測" in conversation.format_remaining({"duration_sec": 300})
    assert conversation.format_remaining({"duration_sec": 300, "deadline": now + 65}, now=now) == "残り 1分05秒"


def test_persona_instruction_orders_documents_first_and_includes_directive() -> None:
    state = default_state(["president", "cto"])
    state["user_name"] = "佐藤"
    text = conversation.persona_instruction(
        "cto", state, {"addressee": "president", "intent": "社長の質問に答える", "special": "結論を言う"}
    )
    # 共通の資料を先頭に置く（ペルソナ間で同じ接頭辞にして、暗黙のキャッシュを効かせる）
    assert text.startswith("# 参照資料")
    assert "あなたは「真壁 玲奈」" in text
    assert "望月さん" in text  # CTO は社長を「望月さん」と呼ぶ
    assert "社長の質問に答える" in text and "結論を言う" in text
    assert "佐藤さん" in text


def test_verdict_directive() -> None:
    v = conversation.Verdict(verdict="redo", reason="材料不足", conditions=["お客さんの声を3つ"])
    d = conversation.verdict_directive(v, decided_already=False)
    assert "やり直し" in d["special"] and "割り込み" in d["special"]
    d2 = conversation.verdict_directive(v, decided_already=True)
    assert "すでに出た結論" in d2["special"]


def test_transcript_lines_skips_cues_and_unknown_authors() -> None:
    from google.adk.events import Event
    from google.genai import types

    def ev(author: str, text: str) -> Event:
        return Event(invocation_id="i", author=author, content=types.Content(role="user", parts=[types.Part(text=text)]))

    lines = conversation.transcript_lines(
        [ev("user", "こんにちは"), ev("cto", "どうも"), ev("other", "x"), ev("user", conversation.CUE_TEXT["idle"])]
    )
    assert "ユーザー: こんにちは" in lines
    assert "真壁 玲奈[cto]: どうも" in lines
    assert "x" not in lines.split("\n")
    assert "（進行）" in lines


def test_finish_cue_and_early_directive() -> None:
    assert conversation.cue_kind(conversation.CUE_TEXT["finish"]) == "finish"
    v = conversation.Verdict(verdict="approved", reason="条件がそろった", conditions=["月次報告"])
    d = conversation.verdict_directive(v, decided_already=True, early=True)
    assert "締める" in d["special"] and "時間" not in d["special"] and "承認" in d["special"]
