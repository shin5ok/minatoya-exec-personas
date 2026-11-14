"""会話を始める前の準備（ローディング中に行う）。

1. 人物像を読み込む（性格・話し方・口癖・判断の軸。scripts/build_prompt.py で組み立てる）
2. テーマに合う資料を選ぶ（OKF の目次＝frontmatter だけを見て選ぶ。本文は読まない）
3. 選んだ資料を読み込む
4. ペルソナごとに、テーマについての「事前の考え」をまとめる（並列）
5. セッションの state を組み立てる

state には資料の本文を入れない（Agent Runtime のセッションを軽く保つため）。
資料の ID だけを保存し、実行時に knowledge/ から本文を組み立てる。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import config, llm
from app.knowledge import KnowledgeBundle, get_bundle

logger = logging.getLogger(__name__)

Progress = Callable[[str, str, dict[str, Any] | None], Awaitable[None]]

# 選んだ資料の本文の上限（文字）。多いほど詳しく答えるが、応答は遅くなる。
MAX_DOC_CHARS = config.MAX_DOC_CHARS
LARGE_DOC_CHARS = 15_000


class LibrarianResult(BaseModel):
    theme_summary: str = Field(description="相談テーマを1〜2文で言い換えたもの")
    decision_question: str = Field(description="この場で決めたいこと（承認・判断の対象）を1文で")
    issues: list[str] = Field(description="関係する経営の論点ID（open_issues の id）。0〜3件")
    doc_ids: list[str] = Field(description="読むべき資料の id（目次の id をそのまま）。重要な順に最大6件")
    scenario_hint: str = Field(description="近いロールプレイ・シナリオがあれば、その番号と名前。なければ空文字")
    user_role_guess: str = Field(description="ユーザー（提案者・相談者）の立場の推定。不明なら『社員』")


class Briefing(BaseModel):
    stance: str = Field(description="このテーマに対する、いまの立場（1〜2文）")
    first_impression: str = Field(description="テーマを聞いた第一印象。本人の口調で1文")
    key_questions: list[str] = Field(description="相手に必ず聞きたい質問。本人の口調で3つ")
    concerns: list[str] = Field(description="懸念やリスク。2〜3件")
    what_would_convince: str = Field(description="何があれば賛成・承認できるか")
    hidden_note: str = Field(description="表には出さない本音や個人的な事情（条件がそろったときだけ出す）")
    small_talk: str = Field(description="冒頭の雑談で触れそうな話題（趣味・最近の出来事など）。1文")


class PrepRequest(BaseModel):
    persona_ids: list[str]
    theme: str = ""
    duration_minutes: float = 0
    user_name: str = ""
    user_role: str = ""

    @property
    def mode(self) -> Literal["dialog", "meeting"]:
        return "dialog" if len(self.persona_ids) == 1 else "meeting"


async def _noop(_step: str, _label: str, _extra: dict[str, Any] | None = None) -> None:
    return None


def scene_for(mode: str) -> str:
    return "one_on_one" if mode == "dialog" else "meeting"


def chair_for(persona_ids: list[str]) -> str:
    return "president" if "president" in persona_ids else persona_ids[0]


async def choose_documents(bundle: KnowledgeBundle, req: PrepRequest) -> LibrarianResult:
    names = "、".join(f"{bundle.personas[p].name}（{bundle.personas[p].role_label}）" for p in req.persona_ids)
    issues = "\n".join(f"- {i['id']}: {i['title']}（期限 {i['decide_by']}）" for i in bundle.open_issues)
    prompt = f"""あなたは社内資料の司書です。これから「{names}」との{'1対1の対話' if req.mode == 'dialog' else '会議'}が始まります。
相談テーマに合う資料を、目次（frontmatter の要約）だけを見て選んでください。

# 相談テーマ（ユーザー入力）
{req.theme or '（未入力。雑談から始め、いまの経営の論点を相談する想定）'}

# ユーザーの立場（自己申告）
{req.user_role or '（未入力）'}

# 経営の論点
{issues}

# 資料の目次（OKF の frontmatter）
{bundle.catalog(req.persona_ids)}

# 選び方
- load=always の資料は自動で読むので、選ばなくてよい
- テーマに直接関係する資料を優先し、出席者の講演録（type=Speech）は話し方や考え方の手がかりになるものを選ぶ
- テーマが曖昧なら、論点の資料（company/06_open_issues は自動）と中間決算を中心にする
"""
    return await llm.generate_json(
        model=config.DIRECTOR_MODEL, prompt=prompt, schema=LibrarianResult, thinking_level="LOW"
    )


async def make_briefing(
    bundle: KnowledgeBundle, pid: str, req: PrepRequest, lib: LibrarianResult, docs: str
) -> Briefing:
    others = [bundle.personas[p].name for p in req.persona_ids if p != pid]
    persona_prompt = bundle.persona_prompt(pid, scene_for(req.mode))
    prompt = f"""{docs}

{persona_prompt}

# これから始まる場
- 形式: {'ユーザーとの1対1の対話' if req.mode == 'dialog' else '会議（出席: ' + '、'.join(others) + '、ユーザー）'}
- 相談テーマ: {req.theme or '（未定。挨拶と雑談から始まる）'}
- テーマの要約: {lib.theme_summary}
- 決めたいこと: {lib.decision_question}
- 関係する論点: {', '.join(bundle.issue_title(i) for i in lib.issues) or 'なし'}
- 近いシナリオ: {lib.scenario_hint or 'なし'}
- ユーザーの立場: {req.user_role or lib.user_role_guess}

始まる前に、あなた（{bundle.personas[pid].name}）としての事前の考えを整理してください。
数字は資料にあるものだけを使い、資料にない数字は作らないこと。
"""
    return await llm.generate_json(
        model=config.PREP_MODEL, prompt=prompt, schema=Briefing, thinking_level="LOW"
    )


def _fallback_library(bundle: KnowledgeBundle, req: PrepRequest) -> LibrarianResult:
    text = req.theme
    keywords = {
        "tide_budget": ["TIDE", "基幹", "38億", "延期", "MOSS"],
        "logi_cloud_carveout": ["Logi", "ロジクラウド", "分社", "外部資本"],
        "ai_ordering_rollout": ["シオミ", "AI発注", "発注"],
        "security_budget": ["セキュリティ", "ランサム", "3.9"],
        "driver_shortage": ["ドライバー", "物流効率化", "2024年問題", "運賃"],
        "rural_stores": ["中山間", "移動販売", "不採算", "ドローン"],
        "it_cost_cut": ["IT費", "削減", "コストカット"],
        "h1_recovery": ["上期", "未達", "挽回", "下期"],
    }
    issues = [k for k, words in keywords.items() if any(w.lower() in text.lower() for w in words)][:3]
    if not issues:
        issues = ["h1_recovery"]
    return LibrarianResult(
        theme_summary=req.theme or "いまの経営の論点についての相談",
        decision_question="提案・相談の方向性を承認できるか",
        issues=issues,
        doc_ids=bundle.ids_for_issues(issues, req.persona_ids)[:6],
        scenario_hint="",
        user_role_guess=req.user_role or "社員",
    )


async def prepare(req: PrepRequest, progress: Progress = _noop) -> dict[str, Any]:
    bundle = get_bundle()
    unknown = [p for p in req.persona_ids if p not in bundle.personas]
    if unknown or not req.persona_ids:
        raise ValueError(f"unknown personas: {unknown}")
    started = time.monotonic()

    names = "・".join(bundle.personas[p].name for p in req.persona_ids)
    await progress("persona", f"{names}の人物像を読み込んでいます（性格・口癖・判断の軸）", None)
    for pid in req.persona_ids:  # 組み立てに失敗しないことを先に確かめる
        bundle.persona_prompt(pid, scene_for(req.mode))

    await progress("library", "相談テーマに合う資料を探しています", None)
    try:
        lib = await choose_documents(bundle, req)
        lib.issues = [i for i in lib.issues if i in {x["id"] for x in bundle.open_issues}][:3]
        lib.doc_ids = [d for d in lib.doc_ids if d in bundle.concepts][:6]
        if not lib.doc_ids:
            lib.doc_ids = bundle.ids_for_issues(lib.issues, req.persona_ids)[:6]
    except Exception:  # 準備の失敗で会話を止めない
        logger.exception("librarian failed; falling back to keyword match")
        lib = _fallback_library(bundle, req)

    doc_ids = order_documents(bundle, bundle.always_ids(), lib.doc_ids, req.mode)
    titles = [bundle.concepts[d].title for d in doc_ids]
    await progress("reading", f"資料を{len(doc_ids)}件読み込んでいます", {"documents": titles})
    docs = bundle.render_many(doc_ids, max_chars_total=MAX_DOC_CHARS)

    await progress("briefing", "それぞれが事前に考えをまとめています", None)
    results = await asyncio.gather(
        *(make_briefing(bundle, pid, req, lib, docs) for pid in req.persona_ids),
        return_exceptions=True,
    )
    briefings: dict[str, Any] = {}
    for pid, res in zip(req.persona_ids, results, strict=True):
        if isinstance(res, BaseException):
            logger.error("briefing failed for %s: %s", pid, res)
            briefings[pid] = None
        else:
            briefings[pid] = res.model_dump()
            await progress("briefed", f"{bundle.personas[pid].name}の準備ができました", {"persona": pid})

    duration = max(0.0, min(float(req.duration_minutes or 0), float(config.MAX_DURATION_MINUTES)))
    state = {
        "prepared": True,
        "mode": req.mode,
        "persona_ids": req.persona_ids,
        "chair": chair_for(req.persona_ids),
        "theme": req.theme,
        "theme_summary": lib.theme_summary,
        "decision_question": lib.decision_question,
        "issues": lib.issues,
        "scenario_hint": lib.scenario_hint,
        "doc_ids": doc_ids,
        "briefings": briefings,
        "user_name": req.user_name.strip(),
        "user_role": req.user_role.strip() or lib.user_role_guess,
        "duration_sec": int(duration * 60),
        "deadline": None,
        "phase": "waiting",
        "decision": {"status": "undecided", "summary": ""},
        "wrapup_done": False,
        "final_done": False,
        "attachments": [],
        "prep_seconds": round(time.monotonic() - started, 1),
    }
    await progress("ready", "準備ができました。挨拶をすると始まります", {"seconds": state["prep_seconds"]})
    return state


def order_documents(bundle: KnowledgeBundle, always: list[str], selected: list[str], mode: str) -> list[str]:
    """上限で切られても困らない順に並べる: 小さい常備資料 → テーマの資料 → 大きい常備資料 → 手本。"""
    size = {cid: len(bundle.render(cid)) for cid in always}
    small = [c for c in always if size[c] <= LARGE_DOC_CHARS]
    large = [c for c in always if size[c] > LARGE_DOC_CHARS]
    ids = small + [c for c in selected if c not in always] + large
    if mode == "meeting":
        ids += bundle.few_shot_ids()
    return list(dict.fromkeys(ids))


def default_state(persona_ids: list[str] | None = None) -> dict[str, Any]:
    """準備を経ずに呼ばれたとき（agents-cli run / eval など）の最小の state。"""
    bundle = get_bundle()
    pids = persona_ids or list(bundle.personas)
    mode = "dialog" if len(pids) == 1 else "meeting"
    doc_ids = order_documents(bundle, bundle.always_ids(), [], mode)
    return {
        "prepared": True,
        "mode": mode,
        "persona_ids": pids,
        "chair": chair_for(pids),
        "theme": "",
        "theme_summary": "いまの経営の論点についての相談",
        "decision_question": "相談内容の方向性を承認できるか",
        "issues": [],
        "scenario_hint": "",
        "doc_ids": doc_ids,
        "briefings": dict.fromkeys(pids),
        "user_name": "",
        "user_role": "社員",
        "duration_sec": 0,
        "deadline": None,
        "phase": "waiting",
        "decision": {"status": "undecided", "summary": ""},
        "wrapup_done": False,
        "final_done": False,
        "attachments": [],
    }
