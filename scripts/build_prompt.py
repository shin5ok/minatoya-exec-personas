#!/usr/bin/env python3
"""ペルソナの YAML から、ロールプレイ用のシステムプロンプトを組み立てる。

使い方:
    python3 scripts/build_prompt.py president
    python3 scripts/build_prompt.py cio --scene external > cio_external.md

scene（場面）:
    meeting     経営会議（既定）
    one_on_one  1on1・雑談
    external    講演・社外の場

組み立てる順番（上ほど重要。トークンが足りなければ下から削る）:
    1. 人物カード（card）と肩書き
    2. 話し方（場面に応じた一人称、敬語、方言）
    3. 口癖のルール   ← render_catchphrase_rules()（TODO）
    4. 判断の軸と、よく聞く質問
    5. 地雷、譲歩のしかた、譲れない線、知らないこと
    6. 論点ごとの立場
    7. 他の役員との関係
    8. 発言例（few-shot）
    9. ロールプレイの共通ルール
会社の資料（company/*.md）と講演録（speeches/*.md）は長いので、ここには入れません。
検索（RAG）で渡す想定です。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent / "knowledge"
SCENES = {"meeting": "経営会議", "one_on_one": "1on1・雑談", "external": "講演・社外の場"}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def bullet(items) -> str:
    return "\n".join(f"- {x}" for x in items)


def flat(text) -> str:
    """YAML のブロック文字列の改行を取り除いて1行にする（行内の空白は残す）。"""
    return "".join(line.strip() for line in str(text).splitlines())


def render_identity(p: dict, scene: str) -> str:
    return (
        f"# あなたは「{p['display_name']}」（{p['kana']}）です\n"
        f"{' ／ '.join(p['current_titles'])}。{p['age']}歳。\n"
        f"いまの場面: {SCENES[scene]}\n\n"
        f"{p['card'].strip()}"
    )


def render_speech_style(p: dict, scene: str) -> str:
    s = p["speech_style"]
    lines = [
        "## 話し方",
        f"- 一人称: {s['first_person'][scene]}",
        f"- 敬語: {s['keigo_level'][scene]}",
        f"- 語尾の傾向: {'、'.join(s['sentence_endings'])}",
        f"- テンポ: {s['tempo']}",
    ]
    if s.get("english_mixing"):
        lines.append(f"- 英語の混ぜ方: {s['english_mixing']}")
    dialect = s.get("dialect")
    if dialect and scene != "external":
        lines.append(f"- 方言（{dialect['base']}）: {flat(dialect['usage_rule'])}")
        lines += [f"  - {w['word']}（＝{w['standard']}）例: {w['example']}" for w in dialect["lexicon"]]
    elif dialect:
        lines.append("- 方言: 原則として使わない（口癖のルールで認めたものを除く）")
    vocab = s["vocabulary"]
    lines.append(f"- よく使う言葉: {'、'.join(vocab['prefers'])}")
    lines.append(f"- 使わない言葉: {'、'.join(vocab['avoids'])}")
    if vocab.get("caution"):
        lines.append(f"- 注意: {vocab['caution']}")
    lines.append(f"- 比喩の引き出し: {'／'.join(s['metaphor_domains'])}")
    return "\n".join(lines)


def render_catchphrase_rules(catchphrases: list[dict], scene: str) -> str:
    """口癖を「どの場面で、どれくらいの頻度で使うか」というルールに変換する。

    TODO(あなた): ここを実装してください（5〜10行程度）。

    口癖は「らしさ」を出すのにいちばん効きます。一方で LLM は、渡された口癖を毎ターン
    使いがちで、すぐにものまね芸人のような演技になります。
    catchphrases の各要素は、次のキーを持ちます（profile.yaml の catchphrases）:
        phrase     口癖そのもの
        meaning    意味
        trigger    使う場面（例: 決断の瞬間）
        frequency  頻度の目安（例: 1会議で最大1回）
        scenes     使ってよい場面のリスト（meeting / one_on_one / external）
        example    使用例

    設計の選択肢（どれを選ぶかで、「らしさ」と自然さのバランスが変わります）:
      - scene に合わない口癖を、黙って除外するか、「この場面では使わない」と明示するか
        （明示すると、かえってその言葉を意識させてしまうこともある）
      - 全部渡すか、上位 N 個に絞るか（多いほど乱発しやすい）
      - frequency をそのまま書くか、「直前の発言で使った口癖は、次の発言では使わない」の
        ような、より強い禁止ルールに言い換えるか
      - example まで渡すか（文体はよく真似るが、同じ文をそのまま繰り返しやすい）

    戻り値は "## 口癖" で始まる Markdown の文字列にしてください。

    採用した設計:
      - scene に合わない口癖は黙って除外する（禁止語として書くと、かえって意識させるため）
      - 上位 MAX_CATCHPHRASES 個に絞る（YAML の並び順を重要度順とみなす）
      - frequency はそのまま書いたうえで、「直前の発言で使った口癖は使わない」
        「口癖なしの発言を基本にする」という強いルールを足す
      - example は渡さない（文をそのまま繰り返しやすいため。口調は発言例で伝える）
    """
    usable = [c for c in catchphrases if scene in c.get("scenes", [])][:MAX_CATCHPHRASES]
    lines = [
        "## 口癖",
        "口癖は「らしさ」のスパイスです。ほとんどの発言は口癖なしで話し、合う場面でだけ使います。",
        "- 直前の自分の発言で使った口癖は、次の発言では使わない",
        "- 1回の発言で使う口癖は1つまで。目安は3回の発言に1回以下",
        "- 口癖を言うためだけに発言を作らない。文脈に合うときだけ自然に混ぜる",
    ]
    lines += [f"- 「{c['phrase']}」（{c['meaning']}）: {c['trigger']}。頻度: {c['frequency']}" for c in usable]
    return "\n".join(lines)


MAX_CATCHPHRASES = 6


def render_decision(p: dict) -> str:
    d = p["decision_style"]
    return "\n".join([
        "## 判断のしかた",
        flat(d["summary"]),
        "### 判断の軸",
        bullet(d["criteria"]),
        "### よく聞く質問（1回の発言で1つまで）",
        bullet(d["typical_questions"]),
        "### 会議でのふるまい",
        bullet(d["meeting_behavior"]),
    ])


def render_boundaries(p: dict) -> str:
    per = p["personality"]
    hot = [f"{h['trigger']} → {h['reaction']}" for h in per["hot_buttons"]]
    return "\n".join([
        "## 人柄",
        flat(per["summary"]),
        "### 地雷（こう言われたら、こう反応する）",
        bullet(hot),
        "### 喜ぶもの",
        bullet(per["pleased_by"]),
        "### 譲歩のしかた",
        flat(per["how_they_concede"]),
        "### 譲れない線",
        bullet(per["red_lines"]),
        "### 知らないこと・苦手なこと（ここは断言せず、他の人に意見を求める）",
        bullet(per["blind_spots"]),
    ])


def render_stances(p: dict, titles: dict[str, str]) -> str:
    st = p["stances"]
    lines = [
        "## いまの論点への立場（2026年11月1日時点）",
        "### 上期の計画未達についての、あなたの見立て",
        flat(st["h1_shortfall_diagnosis"]),
    ]
    for i in st["issues"]:
        lines += [
            f"### {titles.get(i['id'], i['id'])}",
            f"- 立場: {i['position']}",
            f"- 理由: {flat(i['reasoning'])}",
            f"- 受け入れる条件: {i['will_accept_if']}",
            f"- 受け入れないもの: {i['will_reject']}",
        ]
    return "\n".join(lines)


def render_relationships(rel: dict, pid: str, names: dict[str, str]) -> str:
    lines = ["## 他の人との関係", "### 呼び方"]
    lines += [f"- {names.get(t, t)} → 「{v}」" for t, v in rel["address_matrix"][pid].items()]
    for pair in rel["pairs"]:
        if pid not in pair["between"]:
            continue
        other = next(x for x in pair["between"] if x != pid)
        lines += [
            f"### {names.get(other, other)}との関係",
            f"- 表の顔: {flat(pair['public'])}",
            f"- 本音: {flat(pair['private'])}",
            f"- 緊張点: {'／'.join(pair['tension_points'])}",
        ]
    return "\n".join(lines)


def render_examples(p: dict) -> str:
    lines = ["## 発言例（口調の参考。同じ文をそのまま繰り返さない）"]
    lines += [f"- {s['situation']}: 「{s['line']}」" for s in p["sample_lines"]]
    return "\n".join(lines)


def render_ground_rules() -> str:
    return "\n".join([
        "## ロールプレイの共通ルール",
        "- 会社の事実と数字は、渡された資料にあるものだけを使う。資料にない数字は作らない。"
        "聞かれたら「手元に数字がないので、確認して後日お答えします」と答える",
        "- 基準日は2026年11月1日。それより後の出来事は知らない",
        "- 1回の発言は2〜5文。講演の場面を除き、演説しない",
        "- 他の役員の発言にも反応してよい。ユーザーにだけ話しかけない",
        "- ロールプレイの最中に、架空の会社であることを自分から言わない",
    ])


def build(pid: str, scene: str) -> str:
    p = load(ROOT / "personas" / pid / "profile.yaml")
    rel = load(ROOT / "relationships.yaml")
    facts = load(ROOT / "company" / "facts.yaml")
    titles = {i["id"]: i["title"] for i in facts["open_issues"]}
    names = {e["id"]: e["name"] for e in facts["executives"]} | {"user": "提案者・報告者（ユーザー）"}
    parts = [
        render_identity(p, scene),
        render_speech_style(p, scene),
        render_catchphrase_rules(p["catchphrases"], scene),
        render_decision(p),
        render_boundaries(p),
        render_stances(p, titles),
        render_relationships(rel, pid, names),
        render_examples(p),
        render_ground_rules(),
    ]
    return "\n\n".join(parts) + "\n"


def main() -> None:
    personas = sorted(d.name for d in (ROOT / "personas").iterdir() if d.is_dir())
    parser = argparse.ArgumentParser(description="ペルソナのシステムプロンプトを組み立てる")
    parser.add_argument("persona", choices=personas)
    parser.add_argument("--scene", choices=sorted(SCENES), default="meeting")
    args = parser.parse_args()
    print(build(args.persona, args.scene), end="")


if __name__ == "__main__":
    main()
