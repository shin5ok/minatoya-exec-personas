"""相談のサマリ（一覧表示用）を作る。"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from app import config, llm
from app.conversation import DECISION_LABELS, transcript_lines
from app.knowledge import get_bundle

logger = logging.getLogger(__name__)


class Summary(BaseModel):
    title: str = Field(description="相談の見出し（20字以内）")
    consulted: str = Field(description="何を相談したか（1〜2文）")
    key_points: list[str] = Field(description="役員から出た主な指摘・質問（2〜4件。誰の指摘かを先頭に書く）")
    outcome: str = Field(description="結論や、会話がどこまで進んだか（1文）")
    homework: list[str] = Field(description="宿題・次のアクション（0〜3件）")


def is_closed(state: dict[str, Any]) -> bool:
    return state.get("phase") in ("ended", "cancelled")


def has_conversation(state: dict[str, Any]) -> bool:
    return state.get("phase") not in (None, "waiting")


async def make_summary(state: dict[str, Any], events: list[Any]) -> dict[str, Any]:
    bundle = get_bundle()
    names = "、".join(f"{bundle.personas[p].name}（{bundle.personas[p].role_label}）" for p in state.get("persona_ids", []))
    verdict = state.get("verdict") or {}
    status = "キャンセル（途中で中断）" if state.get("phase") == "cancelled" else DECISION_LABELS.get(
        (state.get("decision") or {}).get("status", "undecided"), "未決"
    )
    prompt = f"""次の相談（社内のロールプレイ）を、あとで一覧で見返すためにまとめてください。

- 相手: {names}
- 相談者: {state.get('user_name') or '相談者'}（{state.get('user_role') or '社員'}）
- テーマ: {state.get('theme') or state.get('theme_summary') or '未設定'}
- 状態: {status}
- 結論: {verdict.get('label', '')} {verdict.get('reason', '')} 条件: {'、'.join(verdict.get('conditions', []))}

# 会話
{transcript_lines(events, limit=60) or '（会話なし）'}

「ユーザー」という言葉は使わず、相談者は名前（なければ「相談者」）で書く。会話にないことは書かない。
"""
    summary = await llm.generate_json(
        model=config.DIRECTOR_MODEL, prompt=prompt, schema=Summary, thinking_level=config.DIRECTOR_THINKING_LEVEL
    )
    return {**summary.model_dump(), "created_at": time.time()}
