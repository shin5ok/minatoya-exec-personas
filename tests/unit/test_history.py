"""相談の一覧・サマリ・キャンセルのユニットテスト（LLM を呼ばない部分）。"""

from __future__ import annotations

from types import SimpleNamespace

from app.preparation import default_state
from app.server import history_item, summary_is_stale
from app.summary import has_conversation, is_closed


def test_is_closed_and_has_conversation() -> None:
    assert is_closed({"phase": "ended"}) and is_closed({"phase": "cancelled"})
    assert not is_closed({"phase": "discussion"})
    assert not has_conversation({"phase": "waiting"}) and not has_conversation({})
    assert has_conversation({"phase": "smalltalk"})


def test_summary_is_stale() -> None:
    assert summary_is_stale({}, 1000)
    assert not summary_is_stale({"summary": {"created_at": 990}}, 1000)  # 保存直後の更新は許容
    assert summary_is_stale({"summary": {"created_at": 800}}, 1000)  # その後も会話が続いた


def test_history_item_status() -> None:
    state = default_state(["president", "cto"])
    state["theme"] = "シオミの全店展開"

    def item(**kw):
        return history_item(SimpleNamespace(id="s1", last_update_time=1.0, state={**state, **kw}))

    assert item()["status"] == "未開始"
    assert item(phase="discussion")["status"] == "進行中"
    assert item(phase="cancelled")["status"] == "キャンセル"
    done = item(phase="ended", verdict={"status": "redo", "label": "やり直し（差し戻し）"})
    assert done["status"] == "やり直し（差し戻し）"
    assert done["persona_ids"] == ["president", "cto"] and done["theme"] == "シオミの全店展開"
