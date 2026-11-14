# ruff: noqa
"""ミナトヤ・ホールディングス 幹部シミュレーター（ADK エージェント）。

root_agent は進行役（ExecutiveRoundtable）。ユーザーの発言や進行の合図を受けて、
進行役（director）が「誰が・誰に・何を」話すかを決め、ペルソナのエージェントを順に呼ぶ。

- 対話モード（1人）: 交互に話すだけにせず、ときどき続けて話したり話題を変えたりする
- 会議モード（複数）: 役員どうしの掛け合いを織り込む
- 時間制限: 残り1分でまとめを促し、10秒前に結論が出ていなければ議長が割り込んで結論を出す
"""

import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.events import Event, EventActions
from google.adk.models import Gemini, LlmRequest
from google.genai import types

from app import config, conversation, runtime
from app.knowledge import get_bundle
from app.preparation import default_state

logger = logging.getLogger(__name__)

MODEL = config.TEXT_MODEL


def _instruction_for(pid: str):
    def provider(ctx: ReadonlyContext) -> str:
        state = ctx.state.to_dict() if hasattr(ctx.state, "to_dict") else dict(ctx.state)
        if not state.get("prepared"):
            state = {**default_state([pid]), **state}
        directive = runtime.DIRECTIVES.get((ctx.invocation_id, pid))
        return conversation.persona_instruction(pid, state, directive)

    return provider


async def inject_attachments(callback_context: CallbackContext, llm_request: LlmRequest) -> None:
    """提出された添付ファイルを、毎回のモデル呼び出しの先頭に添える。"""
    attachments = callback_context.state.get("attachments") or []
    parts: list[types.Part] = []
    for a in attachments:
        try:
            part = await callback_context.load_artifact(filename=a["name"])
        except Exception:  # pylint: disable=broad-except
            logger.exception("failed to load attachment %s", a.get("name"))
            part = None
        if part is not None:
            parts += [types.Part(text=f"【提出された添付資料: {a['name']}】"), part]
    if parts:
        llm_request.contents.insert(0, types.Content(role="user", parts=parts))


def make_persona_agent(pid: str) -> LlmAgent:
    info = get_bundle().personas[pid]
    return LlmAgent(
        name=pid,
        description=f"{info.name}（{info.role_label}）。{info.tagline}",
        model=Gemini(model=MODEL, retry_options=types.HttpRetryOptions(attempts=3)),
        instruction=_instruction_for(pid),
        generate_content_config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=config.PERSONA_THINKING_LEVEL),
            temperature=1.0,
        ),
        before_model_callback=inject_attachments,
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )


def _merge_decision(current: dict[str, Any] | None, plan: conversation.Plan) -> dict[str, Any]:
    """一度出た結論は、進行役が「未決」と見ても消さない（覆ったときだけ置き換える）。"""
    current = current or {}
    if plan.decision_status == "undecided" and current.get("status", "undecided") != "undecided":
        return current
    return {"status": plan.decision_status, "summary": plan.decision_summary}


def _user_text(ctx: InvocationContext) -> str:
    content = ctx.user_content
    if not content or not content.parts:
        return ""
    return "".join(p.text or "" for p in content.parts if p.text).strip()


class ExecutiveRoundtable(BaseAgent):
    """進行役。ペルソナのエージェント（sub_agents）を、計画した順に呼ぶ。"""

    def _persona(self, pid: str) -> BaseAgent:
        agent = self.find_sub_agent(pid)
        if agent is None:
            raise ValueError(f"unknown persona: {pid}")
        return agent

    def _state_event(self, ctx: InvocationContext, delta: dict[str, Any]) -> Event:
        return Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            branch=ctx.branch,
            actions=EventActions(state_delta=delta),
        )

    async def _speak(
        self, ctx: InvocationContext, pid: str, directive: dict[str, Any]
    ) -> AsyncGenerator[Event, None]:
        key = (ctx.invocation_id, pid)
        runtime.DIRECTIVES[key] = directive
        try:
            async for event in self._persona(pid).run_async(ctx):
                yield event
        finally:
            runtime.DIRECTIVES.pop(key, None)

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        if not state.get("prepared"):
            yield self._state_event(ctx, default_state())
        if state.get("phase") in ("ended", "cancelled"):
            return

        text = _user_text(ctx)
        cue = conversation.cue_kind(text)
        now = time.time()
        if cue is None and state.get("duration_sec") and not state.get("deadline"):
            yield self._state_event(ctx, {"deadline": now + int(state["duration_sec"])})

        deadline = state.get("deadline")
        final_due = bool(deadline) and now >= float(deadline) - config.FINAL_CALL_SECONDS
        if cue == conversation.CUE_FINISH:
            decided = (state.get("decision") or {}).get("status", "undecided") != "undecided"
            if decided and not state.get("final_done"):
                async for event in self._final_call(ctx, early=True):
                    yield event
            return
        if cue == conversation.CUE_FINAL or (final_due and not state.get("final_done")):
            if not state.get("final_done"):
                async for event in self._final_call(ctx):
                    yield event
            return
        if cue == conversation.CUE_WRAPUP:
            if state.get("wrapup_done"):
                return
            yield self._state_event(ctx, {"wrapup_done": True})

        events = list(ctx.session.events)
        try:
            plan = await conversation.plan_turns(dict(state), events, cue)
        except Exception:  # pylint: disable=broad-except
            logger.exception("director failed; falling back to a single reply")
            speaker = state["persona_ids"][0] if state["mode"] == "dialog" else state.get("chair")
            plan = conversation.Plan(
                phase="smalltalk" if state.get("phase") == "waiting" else "discussion",
                turns=[conversation.Turn(speaker=speaker, addressee="user", intent="相手の発言に自然に応じる")],
                decision_status=(state.get("decision") or {}).get("status", "undecided"),
                decision_summary=(state.get("decision") or {}).get("summary", ""),
            )
        phase = plan.phase
        if cue == conversation.CUE_WRAPUP or (state.get("phase") == "wrapup" and phase in ("smalltalk", "discussion")):
            phase = "wrapup"
        yield self._state_event(
            ctx,
            {
                "phase": phase,
                "decision": _merge_decision(state.get("decision"), plan),
                "last_plan": [t.model_dump() for t in plan.turns],
            },
        )

        interrupt = runtime.interrupt_event(ctx.session.id)
        interrupt.clear()
        for i, turn in enumerate(plan.turns):
            if i > 0 and interrupt.is_set():
                break
            directive = turn.model_dump()
            if cue == conversation.CUE_WRAPUP:
                directive["special"] = (
                    "残り時間は約1分。あなたから、まとめに入るよう相手に話しかける。"
                    "結論を出すために必要な確認を一つだけに絞る。2文程度"
                )
            async for event in self._speak(ctx, turn.speaker, directive):
                yield event

        # 発言のあとで、判断者が結論を明言したかを確かめる（「結論を出して終了」ボタンの表示に使う）
        if state.get("chair") in {t.speaker for t in plan.turns} or state["mode"] == "dialog":
            try:
                check = await conversation.detect_decision(dict(state), list(ctx.session.events))
            except Exception:  # pylint: disable=broad-except
                logger.exception("decision check failed")
            else:
                current = state.get("decision") or {}
                if check.status != "undecided" and check.status != current.get("status"):
                    yield self._state_event(ctx, {"decision": {"status": check.status, "summary": check.summary}})

    async def _final_call(self, ctx: InvocationContext, early: bool = False) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        chair = state.get("chair") or state["persona_ids"][0]
        decision = state.get("decision") or {}
        decided = decision.get("status", "undecided") != "undecided"
        if decided:
            verdict = conversation.Verdict(
                verdict=decision["status"], reason=decision.get("summary", ""), conditions=[]
            )
        else:
            try:
                verdict = await conversation.decide_verdict(dict(state), list(ctx.session.events))
            except Exception:  # pylint: disable=broad-except
                logger.exception("verdict failed; defaulting to redo")
                verdict = conversation.Verdict(
                    verdict="redo",
                    reason="材料が足りず、この場では決めきれないため",
                    conditions=["論点を整理して出直す"],
                )
        yield self._state_event(ctx, {"final_done": True, "phase": "closing"})
        async for event in self._speak(ctx, chair, conversation.verdict_directive(verdict, decided, early=early)):
            yield event
        yield self._state_event(
            ctx,
            {
                "phase": "ended",
                "decision": {"status": verdict.verdict, "summary": verdict.reason},
                "verdict": {
                    "status": verdict.verdict,
                    "label": conversation.DECISION_LABELS[verdict.verdict],
                    "reason": verdict.reason,
                    "conditions": verdict.conditions,
                    "by": chair,
                    "interrupted": not decided,
                },
            },
        )


root_agent = ExecutiveRoundtable(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml.
    name="minatoya_exec_personas",
    description="ミナトヤ・ホールディングスの社長・CTO・CIOとの対話／会議シミュレーター",
    sub_agents=[make_persona_agent(pid) for pid in get_bundle().personas],
)

app = App(
    root_agent=root_agent,
    name="app",
)
