"""Web API のユニットテスト（LLM を呼ばない部分）。"""

from __future__ import annotations

from google.adk.events import Event
from google.genai import types

from app import conversation
from app.preparation import default_state
from app.server import history, public_state, user_id_from


def test_user_id_from_iap_header() -> None:
    assert user_id_from({"x-goog-authenticated-user-email": "accounts.google.com:alice@example.com"}) == "alice@example.com"
    assert user_id_from({}) == "local-user"


def test_public_state_has_documents_and_hides_briefings() -> None:
    state = default_state(["president"])
    state["briefings"] = {"president": {"hidden_note": "秘密"}}
    out = public_state(state)
    assert out["mode"] == "dialog"
    assert "briefings" not in out  # 本音（hidden）は UI に出さない
    assert out["documents"] and {"id", "title", "type"} <= set(out["documents"][0])
    assert "server_time" in out


def test_history_hides_cues_and_partials() -> None:
    def ev(author: str, text: str, partial: bool = False) -> Event:
        return Event(
            invocation_id="i", author=author, partial=partial,
            content=types.Content(role="user", parts=[types.Part(text=text)]),
        )

    msgs = history([
        ev("user", "こんにちは"),
        ev("user", conversation.CUE_TEXT["wrapup"]),
        ev("president", "おはよう", partial=True),
        ev("president", "おはよう、佐藤さん"),
        ev("minatoya_exec_personas", "x"),
    ])
    assert [(m["speaker"], m["text"]) for m in msgs] == [("user", "こんにちは"), ("president", "おはよう、佐藤さん")]
