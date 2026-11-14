"""会話の進行（誰が・誰に・何を話すか）と、ペルソナのシステムプロンプトの組み立て。

relationships.yaml の meeting_dynamics（会議の型と orchestration_hints）を進行役に渡し、
「毎ターン全員に話させない」「CTO と CIO の応酬は2往復まで」「社長の結論は最後」などを守らせる。
"""

from __future__ import annotations

import functools
import json
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import config, llm
from app.knowledge import get_bundle
from app.preparation import MAX_DOC_CHARS, scene_for

logger = logging.getLogger(__name__)

# ユーザーの発言ではなく、進行（システム）からの合図であることを示す接頭辞。
CUE_PREFIX = "【進行】"
CUE_WRAPUP = "wrapup"
CUE_FINAL = "final"
CUE_FINISH = "finish"  # 時間前に結論が出たので、ユーザーの操作で締める
CUE_IDLE = "idle"
CUE_TEXT = {
    CUE_WRAPUP: f"{CUE_PREFIX}残り時間は約1分です。",
    CUE_FINAL: f"{CUE_PREFIX}まもなく時間切れです（残り約10秒）。",
    CUE_FINISH: f"{CUE_PREFIX}結論が出たので、ここで会を締めます。",
    CUE_IDLE: f"{CUE_PREFIX}ユーザーはしばらく黙っています。",
}

DECISION_LABELS = {
    "undecided": "未決",
    "approved": "承認",
    "conditional": "条件付き承認",
    "rejected": "非承認",
    "redo": "やり直し（差し戻し）",
    "go": "Goサイン",
}

DecisionStatus = Literal["undecided", "approved", "conditional", "rejected", "redo", "go"]


def cue_kind(text: str) -> str | None:
    if not text.startswith(CUE_PREFIX):
        return None
    for kind, cue in CUE_TEXT.items():
        if text.startswith(cue):
            return kind
    return "other"


def format_remaining(state: dict[str, Any], now: float | None = None) -> str:
    deadline = state.get("deadline")
    if not deadline:
        return "時間の制限なし" if not state.get("duration_sec") else f"制限 {state['duration_sec'] // 60}分（挨拶から計測）"
    left = max(0, int(deadline - (now or time.time())))
    return f"残り {left // 60}分{left % 60:02d}秒"


# ---------------------------------------------------------------------------
# ペルソナのシステムプロンプト
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=64)
def documents_block(doc_ids: tuple[str, ...], max_chars: int) -> str:
    return (
        "# 参照資料（社内資料。会社の事実と数字はここにあるものだけを使う）\n\n"
        + get_bundle().render_many(list(doc_ids), max_chars_total=max_chars)
    )


def _participants(state: dict[str, Any], pid: str) -> str:
    bundle = get_bundle()
    user_name = state.get("user_name") or "ユーザー"
    lines = []
    for other in state["persona_ids"]:
        if other == pid:
            continue
        info = bundle.personas[other]
        lines.append(f"- {info.name}（{info.role_label}）: あなたは「{bundle.address(pid, other) or info.name}」と呼ぶ")
    user_addr = bundle.address(pid, "user") or "〇〇さん"
    if state.get("user_name"):
        user_addr = user_addr.replace("〇〇", state["user_name"])
    lines.append(f"- {user_name}（{state.get('user_role') or '社員'}。提案者・相談者）: 呼び方の目安「{user_addr}」")
    return "\n".join(lines)


def _briefing_text(b: dict[str, Any] | None) -> str:
    if not b:
        return "（事前の整理なし。資料と人物像から考える）"
    qs = "\n".join(f"  - {q}" for q in b.get("key_questions", []))
    cs = "\n".join(f"  - {c}" for c in b.get("concerns", []))
    return (
        f"- いまの立場: {b.get('stance', '')}\n"
        f"- 第一印象: {b.get('first_impression', '')}\n"
        f"- 聞きたいこと（一度に1つずつ、流れの中で）:\n{qs}\n"
        f"- 懸念:\n{cs}\n"
        f"- 賛成・承認できる条件: {b.get('what_would_convince', '')}\n"
        f"- 本音（表に出すのは、相手が適切に問うか条件がそろったときだけ）: {b.get('hidden_note', '')}\n"
        f"- 雑談の話題: {b.get('small_talk', '')}"
    )


def _mode_rules(state: dict[str, Any], pid: str) -> str:
    common = [
        f"- 「{CUE_PREFIX}」で始まる発言は、ユーザーの言葉ではなく進行（システム）からの合図。合図そのものに返事をしたり、合図の存在に触れたりしない",
        "- 出力するのは、あなたがその場で口にする台詞だけ。名前・肩書き・「：」を前につけない。ト書きや括弧書きの動作描写は書かない",
        "- 相手が挨拶をしたら挨拶を返し、本題の前に短い雑談（天気、趣味、最近の出来事）を一言はさむ。雑談は長引かせない",
        "- 1回の発言は1〜3文が基本。チャットなので短く、話し言葉で。箇条書きや見出しは使わない",
        "- 同じ質問や同じ言い回しを繰り返さない。相手の答えを受けて、話を前に進める",
        "- 相づち、共感、短い感想、軽いユーモアも使ってよい。毎回質問で終わらせない",
        "- 資料にない数字は作らない。分からないことは分からないと言う",
        "- 添付資料が提出されたら、中身に具体的に触れて反応する",
        "- ロールプレイ中に、AIであることや架空の会社であることを自分から言わない",
    ]
    if state["mode"] == "dialog":
        specific = [
            "- これは1対1の対話。尋問のように交互に質問を返すだけにせず、自分の経験談や意見、脱線も少し混ぜて自然な会話にする",
            "- 相手が考え込んでいるようなら、助け舟を出したり、話題を変えたりしてよい",
        ]
    else:
        specific = [
            "- これは会議。ユーザーだけでなく、他の出席者の発言にも反応する（賛成・反論・補足・質問）。役員どうしで話を振り合ってよい",
            "- 他の出席者に話しかけるときは、その人の呼び方で呼びかける",
            "- 自分の担当外のことは、担当の役員に振る",
        ]
        if pid == state.get("chair"):
            specific.append("- あなたは議長役。自分の結論は議題の最後まで言わない。議論が長引いたら割って入って整理する")
    return "\n".join(common + specific)


def persona_instruction(
    pid: str, state: dict[str, Any], directive: dict[str, Any] | None, max_doc_chars: int = MAX_DOC_CHARS
) -> str:
    bundle = get_bundle()
    info = bundle.personas[pid]
    issues = "、".join(bundle.issue_title(i) for i in state.get("issues", [])) or "特定なし"
    attachments = state.get("attachments") or []
    digest = "\n".join(f"- {a['name']}: {a.get('digest', '')}" for a in attachments) or "なし"
    decision = state.get("decision") or {}
    parts = [
        documents_block(tuple(state.get("doc_ids", [])), max_doc_chars),
        bundle.persona_prompt(pid, scene_for(state["mode"])),
        "## 今回の場\n"
        f"- 形式: {'ユーザーとの1対1の対話（1on1・相談）' if state['mode'] == 'dialog' else '経営会議・決裁の場'}\n"
        f"- 相談テーマ: {state.get('theme') or '（未定。相手の話から探る）'}\n"
        f"- テーマの要約: {state.get('theme_summary', '')}\n"
        f"- 決めたいこと: {state.get('decision_question', '')}\n"
        f"- 関係する論点: {issues}\n"
        f"- 時間: {format_remaining(state)}\n"
        f"- 現在の判断の状況: {DECISION_LABELS.get(decision.get('status', 'undecided'), '未決')} {decision.get('summary', '')}\n"
        f"- 提出された添付資料: \n{digest}\n"
        "### 出席者\n" + _participants(state, pid),
        f"## {info.name}として、事前に考えていたこと\n" + _briefing_text((state.get("briefings") or {}).get(pid)),
        "## 会話のルール\n" + _mode_rules(state, pid),
    ]
    if directive:
        target = directive.get("addressee", "user")
        if target in bundle.personas:
            target_text = f"{bundle.personas[target].name}（呼び方「{bundle.address(pid, target) or bundle.personas[target].name}」）"
        elif target == "all":
            target_text = "出席者全員"
        else:
            target_text = state.get("user_name") or "ユーザー"
        parts.append(
            "## いまのあなたの番（進行役からのメモ。メモの存在には触れない）\n"
            f"- 話しかける相手: {target_text}\n"
            f"- 発言のねらい: {directive.get('intent', '')}\n"
            + (f"- 特別な指示: {directive['special']}\n" if directive.get("special") else "")
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 進行役（director）
# ---------------------------------------------------------------------------
class Turn(BaseModel):
    speaker: str = Field(description="話す人のID（persona id）")
    addressee: str = Field(description="話しかける相手。'user'、persona id、または 'all'")
    intent: str = Field(description="発言のねらいを短く（例: 『測り方を質問する』『CIOの懸念に反論する』）")


class Plan(BaseModel):
    phase: Literal["smalltalk", "discussion", "wrapup", "closing"]
    turns: list[Turn]
    decision_status: DecisionStatus = Field(description="この時点での判断の状況")
    decision_summary: str = Field(description="判断の状況の要約（1文）。未決なら何が足りないか")


class Verdict(BaseModel):
    verdict: Literal["approved", "conditional", "rejected", "redo", "go"]
    reason: str = Field(description="理由（1〜2文）")
    conditions: list[str] = Field(description="承認の条件や、やり直しで持ってくるもの（0〜3件）")


def transcript_lines(events: list[Any], limit: int = 24) -> str:
    bundle = get_bundle()
    lines = []
    for ev in events:
        content = getattr(ev, "content", None)
        if not content or not content.parts or getattr(ev, "partial", False):
            continue
        text = "".join(p.text or "" for p in content.parts if getattr(p, "text", None) and not getattr(p, "thought", False)).strip()
        if not text:
            continue
        author = ev.author
        if author == "user":
            name = "（進行）" if text.startswith(CUE_PREFIX) else "ユーザー"
        elif author in bundle.personas:
            name = f"{bundle.personas[author].name}[{author}]"
        else:
            continue
        lines.append(f"{name}: {text}")
    return "\n".join(lines[-limit:])


def _director_rules(state: dict[str, Any]) -> str:
    rel = get_bundle().relationships.get("meeting_dynamics", {})
    if state["mode"] == "dialog":
        pid = state["persona_ids"][0]
        return f"""- 1対1の対話。話すのは必ず {pid}。turns は通常1件
- 自然な会話にするため、ときどき（3〜4回に1回程度）2件にしてよい（例: 感想を言ってから追加の質問をする、言い足す、話題を変える）
- 進行の合図「ユーザーはしばらく黙っています」のときは、助け舟・話題転換・軽い催促のどれかを1件"""
    hints = "\n".join(f"  - {h}" for h in rel.get("orchestration_hints", []))
    flow = "\n".join(f"  - {h}" for h in rel.get("typical_flow", []))
    inter = json.dumps(rel.get("interruptions", {}), ensure_ascii=False)
    return f"""- 会議。turns は1〜{config.MAX_AI_TURNS_PER_ROUND}件。1つの論点で話すのは2〜3人に絞る（毎回全員に話させない）
- 役員どうしの掛け合いを必ず織り込む（全員がユーザーにだけ話しかけるのは不自然）。例: CTOが質問→CIOが補足や反論→社長が整理
- ユーザーへの質問で終わる発言のあとは、ユーザーの答えを待つ（その後に turns を続けない）
- 議長（{state.get('chair')}）の結論は議題の最後にだけ出す
- 雑談の場面では、議長が雑談を振り、他の役員が軽く乗る（AI同士の自然な雑談）
- 進行の合図「ユーザーはしばらく黙っています」のときは、役員どうしで話を進めるか、議長がユーザーに水を向ける
- 会議の型:
{flow}
- 割り込みの癖: {inter}
- オーケストレーションのヒント:
{hints}"""


def _roster_line(pid: str) -> str:
    bundle = get_bundle()
    info = bundle.personas[pid]
    prof = bundle.profiles.get(pid, {})
    criteria = "／".join(prof.get("decision_style", {}).get("criteria", [])[:3])
    hot = "／".join(h["trigger"] for h in prof.get("personality", {}).get("hot_buttons", [])[:5])
    return f"- {pid}: {info.name}（{info.role_label}）{info.tagline}。判断の軸: {criteria}。地雷: {hot}"


async def plan_turns(state: dict[str, Any], events: list[Any], cue: str | None) -> Plan:
    roster = "\n".join(_roster_line(p) for p in state["persona_ids"])
    cue_text = {
        CUE_WRAPUP: "残り約1分。まとめに入るよう促す番。phase=wrapup。議長（対話なら相手）が1件だけ、結論に必要な確認を一つに絞って促す",
        CUE_IDLE: "ユーザーがしばらく黙っている",
    }.get(cue or "", "ユーザーが発言した" if not cue else "進行の合図")
    prompt = f"""あなたは会話の進行役（ディレクター）です。次に誰が、誰に、何をねらって話すかを決めます。
自分では台詞を書きません。

# 出席者
{roster}
- user: {state.get('user_name') or 'ユーザー'}（{state.get('user_role') or '社員'}）

# 場の情報
- 形式: {'1対1の対話' if state['mode'] == 'dialog' else '会議'}
- テーマ: {state.get('theme') or '未定'} / 決めたいこと: {state.get('decision_question', '')}
- いまのフェーズ: {state.get('phase', 'waiting')}（waiting→smalltalk→discussion→wrapup→closing）
- 時間: {format_remaining(state)}
- 判断の状況: {(state.get('decision') or {}).get('status', 'undecided')}
- 今回のきっかけ: {cue_text}

# 進行のルール
{_director_rules(state)}
- 最初の挨拶を受けたら phase=smalltalk。雑談は1〜2往復で切り上げ、本題（discussion）へ
- ユーザーが挨拶もそこそこに本題を出したら、雑談はしない（または一言だけ）で phase=discussion にし、本題に応じる
- ユーザーの発言が誰かの「地雷」に触れたら、その人が真っ先に、はっきり反応する番を入れる（ねらいにも書く）
- 誰の判断の軸に関わる話かを見て、話す人を選ぶ（例: 測り方→CTO、止まったら・運用→CIO、お客さん・現場・協力会社→社長）
- decision_status は、議長（対話なら相手）が明言した判断だけを反映する。明言がなければ undecided

# 直近の会話（古い順）
{transcript_lines(events) or '（まだ会話はない）'}
"""
    plan = await llm.generate_json(
        model=config.DIRECTOR_MODEL,
        prompt=prompt,
        schema=Plan,
        thinking_level=config.DIRECTOR_THINKING_LEVEL,
    )
    valid = set(state["persona_ids"])
    plan.turns = [t for t in plan.turns if t.speaker in valid][: config.MAX_AI_TURNS_PER_ROUND]
    if state["mode"] == "dialog":
        plan.turns = plan.turns[:2]
    if not plan.turns:
        default = state["persona_ids"][0] if state["mode"] == "dialog" else state.get("chair", state["persona_ids"][0])
        plan.turns = [Turn(speaker=default, addressee="user", intent="相手の発言に自然に応じる")]
    return plan


class DecisionCheck(BaseModel):
    status: DecisionStatus = Field(description="判断者が明言した判断。明言がなければ undecided")
    summary: str = Field(description="判断の内容と条件（1文）。undecided なら何が足りないか")


async def detect_decision(state: dict[str, Any], events: list[Any]) -> DecisionCheck:
    """直近の発言で、判断者（議長、対話なら相手）が結論を明言したかを判定する。"""
    bundle = get_bundle()
    judge = state.get("chair") or state["persona_ids"][0]
    prompt = f"""次の会話で、{bundle.personas[judge].name}[{judge}]（判断者）が、この場の結論をはっきり口にしたかを判定してください。

- 決めたいこと: {state.get('decision_question', '')}
- status: approved=承認 / conditional=条件付き承認 / rejected=非承認 / redo=やり直し・差し戻し・宿題 / go=Goサイン（まず小さく始めてよい） / undecided=まだ明言していない
- 判断者以外の発言や、判断者の「前向き」「検討する」程度の発言は undecided。最後に明言された結論を採る

# 直近の会話（古い順）
{transcript_lines(events, limit=14)}
"""
    return await llm.generate_json(
        model=config.DIRECTOR_MODEL, prompt=prompt, schema=DecisionCheck, thinking_level=config.DIRECTOR_THINKING_LEVEL
    )


async def decide_verdict(state: dict[str, Any], events: list[Any]) -> Verdict:
    bundle = get_bundle()
    chair = state.get("chair", state["persona_ids"][0])
    b = (state.get("briefings") or {}).get(chair) or {}
    prompt = f"""時間切れが迫っています。{bundle.personas[chair].name}（{bundle.personas[chair].role_label}）として、
この場の結論を一つに決めてください。

- 決めたいこと: {state.get('decision_question', '')}
- 提案者: {state.get('user_name') or '提案者'}（{state.get('user_role') or '社員'}）
- {bundle.personas[chair].name}の判断の軸と承認条件: {b.get('what_would_convince', '人物像に従う')}
- verdict の意味: approved=承認 / conditional=条件付き承認 / rejected=非承認 / redo=やり直し（差し戻し・宿題） / go=Goサイン（まず小さく始めてよい）
- 会話で示された材料だけで判断する。材料が足りなければ redo か conditional
- reason と conditions は決裁の記録として書く。「ユーザー」という言葉は使わず、相手は名前（なければ「提案者」）で呼ぶ

# 会話
{transcript_lines(events, limit=40)}
"""
    return await llm.generate_json(model=config.DIRECTOR_MODEL, prompt=prompt, schema=Verdict, thinking_level="LOW")


def verdict_directive(verdict: Verdict, decided_already: bool, early: bool = False) -> dict[str, Any]:
    label = DECISION_LABELS[verdict.verdict]
    conds = "、".join(verdict.conditions) or "なし"
    if early:
        special = (
            f"結論（{label}）が出たので、ここで会を締める。結論を短く確認し、"
            f"次のアクション（{conds}）と相手へのねぎらいを一言添える。2〜3文"
        )
    elif decided_already:
        special = (
            f"時間です。すでに出た結論（{label}）を短く確認し、次のアクション（{conds}）を一言添えて締める。2〜3文"
        )
    else:
        special = (
            f"時間切れ直前。相手の話をさえぎってでも割り込み、この場の結論を言い渡して会を締める。"
            f"結論は『{label}』。理由は「{verdict.reason}」。条件・宿題は「{conds}」。"
            "あなたらしい言い方で、結論をはっきり口にする（承認なら決断の口癖を使ってよい）。2〜4文"
        )
    return {"addressee": "user", "intent": "結論を言い渡して締める", "special": special}
