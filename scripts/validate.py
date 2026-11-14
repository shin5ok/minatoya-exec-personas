#!/usr/bin/env python3
"""ペルソナデータセットの整合性チェック。

使い方:
    python3 scripts/validate.py

チェック内容:
  1. すべての YAML と Markdown の front matter が読めること
  2. facts.yaml の算術（セグメント合計、計画差異、店舗数、EPS・配当性向、ROE など）
  3. 利益率の妥当性（スーパー 1.5〜4%、3PL 3〜7%）
  4. 年齢と生年月日の一致（as_of 基準）、経歴の時系列
  5. 講演日が基準日以前であること、講演時の肩書きが経歴と一致すること
  6. 講演が引用した数字（cited_facts）が facts.yaml と一致し、本文に現れること
  7. open_issues の論点 ID を全ペルソナの stances がカバーしていること
  8. 「YYYY年M月D日（曜）」の曜日が正しいこと
  9. 決算期の表記に「FY」が混ざっていないこと（「2026年2月期」で統一）
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent / "knowledge"
WEEKDAYS = "月火水木金土日"
DATE_WITH_WEEKDAY = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日[（(]([月火水木金土日])[）)]")
FY_LABEL = re.compile(r"\bFY\s?\d{2,4}")
OKF_RESERVED = {"index.md", "log.md"}  # OKF の予約ファイル（目次と更新履歴）。講演録としては扱わない

errors: list[str] = []
warnings: list[str] = []


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT))


def load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        errors.append(f"{rel(path)}: YAML を読めない: {e}")
        return None


def split_front_matter(path: Path) -> tuple[dict | None, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        errors.append(f"{rel(path)}: front matter が閉じていない")
        return None, text
    try:
        return yaml.safe_load(text[4:end]), text[end + 5:]
    except yaml.YAMLError as e:
        errors.append(f"{rel(path)}: front matter を読めない: {e}")
        return None, text


def get_path(data, dotted: str):
    cur = data
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError(dotted)
        cur = cur[key]
    return cur


def approx(a, b, tol: float = 0.051) -> bool:
    return abs(float(a) - float(b)) <= tol


def to_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def ym(value) -> str:
    """date / 'YYYY-MM' / 'YYYY-MM-DD' を 'YYYY-MM' にそろえる。"""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.strftime("%Y-%m")
    return str(value)[:7]


def expect(cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


# ---------------------------------------------------------------------
# facts.yaml
# ---------------------------------------------------------------------
def check_segments(label: str, revenue: int, op: int, segments: dict, adjustment: float) -> None:
    seg_rev = sum(s["external_revenue"] for s in segments.values())
    expect(seg_rev == revenue, f"facts {label}: セグメント外部売上の合計 {seg_rev} ≠ 連結 {revenue}")
    seg_op = sum(s["profit"] for s in segments.values()) + adjustment
    expect(approx(seg_op, op), f"facts {label}: セグメント利益＋調整額 {seg_op} ≠ 営業利益 {op}")

    retail = segments["retail"]
    d = retail.get("detail")
    if d:
        expect(d["stores_revenue"] + d["ec_revenue"] == retail["external_revenue"],
               f"facts {label}: リテールの店舗＋EC売上が合わない")
        expect(d["stores_profit"] + d["ec_profit"] == retail["profit"],
               f"facts {label}: リテールの店舗＋EC利益が合わない")
    margin = retail["profit"] / retail["external_revenue"] * 100
    if not 1.5 <= margin <= 4.0:
        warnings.append(f"facts {label}: リテール利益率 {margin:.1f}% がスーパーとして不自然")

    lg = segments["logistics"]
    lg_total = lg["external_revenue"] + lg.get("intersegment_revenue", 0)
    margin = lg["profit"] / lg_total * 100
    if not 3.0 <= margin <= 7.0:
        warnings.append(f"facts {label}: ロジ利益率 {margin:.1f}% が3PLとして不自然")

    dd = segments["digital_other"].get("detail")
    if dd:
        expect(sum(dd.values()) == segments["digital_other"]["profit"],
               f"facts {label}: デジタル・その他の利益内訳が合わない")


def check_facts(f: dict) -> None:
    fin = f["financials"]
    periods = fin["periods"]
    shares = f["stock"]["shares_outstanding_ex_treasury"] / 1e8  # 億株

    keys = sorted(periods)
    for prev_key, key in zip(keys, keys[1:]):
        p = periods[key]
        if "revenue" in p:
            expect(approx(p["operating_income"] / p["revenue"] * 100, p["operating_margin_pct"]),
                   f"facts {key}: 営業利益率が合わない")
        if "roe_pct" in p and "equity" in periods[prev_key]:
            roe = p["net_income"] / ((periods[prev_key]["equity"] + p["equity"]) / 2) * 100
            expect(approx(roe, p["roe_pct"]), f"facts {key}: ROE {roe:.2f}% ≠ {p['roe_pct']}%")
        if "eps_yen" in p:
            expect(approx(p["net_income"] / shares, p["eps_yen"], 0.06), f"facts {key}: EPS が合わない")

    p = periods["2026-02"]
    check_segments("2026年2月期", p["revenue"], p["operating_income"], p["segments"], p["adjustment"])
    expect(approx(p["dividend_yen"] / p["eps_yen"] * 100, p["payout_ratio_pct"]), "facts 2026-02: 配当性向が合わない")
    expect(approx(p["equity"] / p["total_assets"] * 100, p["equity_ratio_pct"]), "facts 2026-02: 自己資本比率が合わない")
    expect(p["operating_cf"] + p["investing_cf"] == p["free_cf"], "facts 2026-02: FCF が合わない")
    retail = p["segments"]["retail"]
    expect(approx(retail["detail"]["ec_revenue"] / retail["external_revenue"] * 100, p["kpi"]["ec_ratio_pct"]),
           "facts 2026-02: EC化率が合わない")

    h = fin["h1"]["2027-02"]
    check_segments("中間期実績", h["actual"]["revenue"], h["actual"]["operating_income"],
                   h["segments_actual"], h["adjustment_actual"])
    check_segments("前年中間期", h["prior_year"]["revenue"], h["prior_year"]["operating_income"],
                   h["segments_prior_year"], h["adjustment_prior_year"])
    plan_op = sum(s["profit"] for s in h["segments_plan"].values()) + h["adjustment_plan"]
    expect(plan_op == h["plan"]["operating_income"], f"facts 中間期計画: セグメント計 {plan_op} ≠ {h['plan']['operating_income']}")
    expect(approx((h["actual"]["revenue"] / h["prior_year"]["revenue"] - 1) * 100, h["revenue_yoy_pct"]), "facts 中間期: 売上前年比")
    expect(approx((h["actual"]["operating_income"] / h["prior_year"]["operating_income"] - 1) * 100,
                  h["operating_income_yoy_pct"]), "facts 中間期: 営業利益前年比")
    expect(approx(h["actual"]["revenue"] / h["plan"]["revenue"] * 100, h["revenue_vs_plan_pct"]), "facts 中間期: 売上計画比")
    expect(approx(h["actual"]["operating_income"] / h["plan"]["operating_income"] * 100,
                  h["operating_income_vs_plan_pct"]), "facts 中間期: 営業利益計画比")

    var = h["variance_vs_plan"]
    for seg in ("retail", "logistics", "digital_other"):
        diff = h["segments_actual"][seg]["profit"] - h["segments_plan"][seg]["profit"]
        expect(approx(sum(var[seg].values()), diff), f"facts 計画差異 {seg}: 要因合計 {sum(var[seg].values()):.1f} ≠ {diff}")
    corp = h["adjustment_actual"] - h["adjustment_plan"]
    expect(approx(sum(var["corporate"].values()), corp), "facts 計画差異 corporate が合わない")

    k = h["kpi"]
    est = ((1 + k["existing_store_customers_yoy_pct"] / 100) * (1 + k["existing_store_ticket_yoy_pct"] / 100) - 1) * 100
    expect(approx(est, k["existing_store_sales_yoy_pct"], 0.1), "facts 中間期: 既存店売上 ≠ 客数×客単価")

    fc = fin["forecast"]["2027-02"]
    expect(approx(fc["operating_income"] / fc["revenue"] * 100, fc["operating_margin_pct"]), "facts 予想: 営業利益率")
    expect(approx(fc["net_income"] / shares, fc["eps_yen"], 0.06), "facts 予想: EPS")
    expect(approx(fc["dividend_yen"] / fc["eps_yen"] * 100, fc["payout_ratio_pct"]), "facts 予想: 配当性向")
    ih = fc["implied_h2"]
    expect(ih["revenue"] == fc["revenue"] - h["actual"]["revenue"], "facts 予想: 下期売上の逆算が合わない")
    expect(ih["operating_income"] == fc["operating_income"] - h["actual"]["operating_income"], "facts 予想: 下期営業利益の逆算")
    prior_h2 = p["operating_income"] - h["prior_year"]["operating_income"]
    expect(approx((ih["operating_income"] / prior_h2 - 1) * 100, ih["operating_income_yoy_pct"]), "facts 予想: 下期の前年比")

    t = f["mid_term_plan"]["targets_2028_02"]
    sp = t["segment_profit"]
    expect(sum(sp.values()) == t["operating_income"], "facts 中計: セグメント利益目標の合計")
    expect(approx(t["operating_income"] / t["revenue"] * 100, t["operating_margin_pct"]), "facts 中計: 営業利益率")
    inv = f["mid_term_plan"]["investment_3y"]
    expect(inv["stores"] + inv["logistics"] + inv["it_digital"] + inv["human_capital_other"] == inv["total"], "facts 中計: 投資総額")
    expect(sum(inv["it_digital_breakdown"].values()) == inv["it_digital"], "facts 中計: IT・デジタル投資の内訳")

    for key, s in f["stores"].items():
        if "standard" in s:
            expect(s["standard"] + s["quick"] == s["total"], f"facts 店舗 {key}: 業態別合計")
        if "by_prefecture" in s:
            expect(sum(s["by_prefecture"].values()) == s["total"], f"facts 店舗 {key}: 県別合計")

    lg = f["logistics"]
    expect(lg["centers_owned"] + lg["centers_leased"] == lg["centers"], "facts 物流: 拠点数")
    expect(p["kpi"]["tpl_clients"] + k["new_3pl_clients"] == lg["tpl_clients"], "facts 物流: 3PL荷主数の推移")
    hm = lg["smart_dc"]["hamamatsu"]
    expect(hm["amr_phase1"] + hm["amr_phase2_add"] == hm["amr_total_after_phase2"], "facts 浜松スマートDC: AMR台数")

    dg = f["digital"]
    expect(dg["logi_cloud"]["arr"]["2026-02"] == p["kpi"]["logi_cloud_arr"], "facts: Logi Cloud ARR（2026-02）の重複値が不一致")
    expect(dg["logi_cloud"]["arr"]["2026-08"] == k["logi_cloud_arr"], "facts: Logi Cloud ARR（2026-08）の重複値が不一致")
    expect(dg["logi_cloud"]["customers_2026_08"] == k["logi_cloud_customers"], "facts: Logi Cloud 導入社数の重複値が不一致")
    expect(dg["logi_cloud"]["profit_2026_02"] == p["segments"]["digital_other"]["detail"]["logi_cloud_profit"],
           "facts: Logi Cloud 損益の重複値が不一致")

    it = f["it"]
    spend = it["it_spend_2026_02"]
    expect(sum(v for key, v in spend.items() if key != "total") == spend["total"], "facts IT費: 内訳合計")
    expect(spend["security"] == it["security"]["annual_budget_2026_02"], "facts IT費: セキュリティ費の重複値")
    expect(approx(spend["total"] * it["cost_cut_proposal"]["pct"] / 100, it["cost_cut_proposal"]["amount"], 0.5),
           "facts IT費: 削減案の金額")
    tide = it["tide"]
    expect(approx(sum(tide["additional_budget_breakdown"].values()), tide["additional_request"]), "facts TIDE: 追加予算の内訳")
    expect(tide["budget_initial"] + tide["additional_request"] == tide["budget_revised"], "facts TIDE: 改定後予算")
    expect(approx(tide["moss_extension_monthly"] * 6, tide["additional_budget_breakdown"]["moss_extension"]),
           "facts TIDE: MOSS延命費（月額×6か月）")

    st = f["stock"]
    shares_n = st["shares_outstanding_ex_treasury"] / 1e8
    expect(approx(st["price_2026_10_30"] * shares_n, st["market_cap"], 1), "facts 株式: 時価総額")
    expect(approx(p["equity"] / shares_n, st["bps_yen_2026_02"], 1), "facts 株式: BPS")
    expect(approx(st["price_2026_10_30"] / st["bps_yen_2026_02"], st["pbr"], 0.01), "facts 株式: PBR")
    expect(approx(st["price_2026_10_30"] / fc["eps_yen"], st["per_forecast"], 0.06), "facts 株式: PER")

    ages = {e["id"]: e["age"] for e in f["executives"]}
    expect(set(f["meetings"]["management_committee"]["members"]) <= set(ages), "facts 会議: メンバーIDが executives にない")


# ---------------------------------------------------------------------
# ペルソナ
# ---------------------------------------------------------------------
def active_titles(career: list[dict], when: str) -> set[str]:
    out = set()
    for c in career:
        end = ym(c["to"]) if c.get("to") else "9999-12"
        if ym(c["from"]) <= when <= end:
            out.add(f"{c['org']} {c['title']}")
    return out


def check_persona(pdir: Path, facts: dict, issue_ids: set[str]) -> None:
    prof = load_yaml(pdir / "profile.yaml")
    if not prof:
        return
    name = pdir.name
    as_of = to_date(facts["as_of"])

    expect(prof["id"] == name, f"{name}: id がフォルダ名と違う")
    b = to_date(prof["birth_date"])
    age = as_of.year - b.year - ((as_of.month, as_of.day) < (b.month, b.day))
    expect(age == prof["age"], f"{name}: 年齢 {prof['age']} ≠ 生年月日からの計算 {age}")
    exec_age = {e["id"]: e["age"] for e in facts["executives"]}.get(name)
    expect(exec_age == prof["age"], f"{name}: facts.executives の年齢と不一致")

    for c in prof["career"]:
        if c.get("to"):
            expect(ym(c["from"]) <= ym(c["to"]), f"{name}: 経歴の期間が逆転 {c}")
    current = active_titles(prof["career"], as_of.strftime("%Y-%m"))
    for t in prof["current_titles"]:
        expect(t in current, f"{name}: current_titles「{t}」が現在の経歴にない")

    n = len(prof["card"])
    if not 300 <= n <= 700:
        warnings.append(f"{name}: card が {n} 字（目安 500 字前後）")

    for cp in prof["catchphrases"]:
        for key in ("phrase", "meaning", "trigger", "frequency", "scenes", "example"):
            expect(key in cp, f"{name}: 口癖「{cp.get('phrase')}」に {key} がない")
        for s in cp.get("scenes", []):
            expect(s in {"meeting", "one_on_one", "external"}, f"{name}: 口癖の scene が不正: {s}")
    for key in ("meeting", "one_on_one", "external"):
        expect(key in prof["speech_style"]["first_person"], f"{name}: first_person.{key} がない")
        expect(key in prof["speech_style"]["keigo_level"], f"{name}: keigo_level.{key} がない")

    covered = {i["id"] for i in prof["stances"]["issues"]}
    expect(issue_ids <= covered, f"{name}: stances に論点がない: {sorted(issue_ids - covered)}")
    expect(not covered - issue_ids, f"{name}: 未定義の論点ID: {sorted(covered - issue_ids)}")

    for a in prof.get("appearances", []):
        expect(to_date(a["date"]) <= as_of, f"{name}: 登壇歴 {a['date']} が基準日より後")
        if a.get("file"):
            expect((pdir / a["file"]).exists(), f"{name}: 講演ファイルがない: {a['file']}")
        else:
            expect(bool(a.get("summary")), f"{name}: 講演録のない登壇歴には summary が必要: {a['title']}")

    for sp in sorted((pdir / "speeches").glob("*.md")):
        if sp.name in OKF_RESERVED:
            continue
        meta, body = split_front_matter(sp)
        if not meta:
            errors.append(f"{rel(sp)}: front matter がない")
            continue
        d = to_date(meta["date"])
        expect(d <= as_of, f"{rel(sp)}: 講演日 {d} が基準日より後")
        expect(sp.name.startswith(d.isoformat()), f"{rel(sp)}: ファイル名の日付と front matter の date が違う")
        expect(meta.get("speaker") == prof["id"], f"{rel(sp)}: speaker が {prof['id']} ではない")
        titles = active_titles(prof["career"], d.strftime("%Y-%m"))
        for part in str(meta["speaker_title"]).split("／"):
            expect(part.strip() in titles,
                   f"{rel(sp)}: 講演時の肩書き「{part.strip()}」が経歴と合わない（候補: {sorted(titles)}）")
        for path, value in (meta.get("cited_facts") or {}).items():
            try:
                actual = get_path(facts, path)
            except KeyError:
                errors.append(f"{rel(sp)}: cited_facts のパスが facts.yaml にない: {path}")
                continue
            expect(actual == value, f"{rel(sp)}: {path} = {value} だが facts.yaml は {actual}")
            variants = {str(value)}
            if isinstance(value, int):
                variants.add(f"{value:,}")
            if not any(v in body for v in variants):
                warnings.append(f"{rel(sp)}: 引用した数字 {value}（{path}）が本文に見当たらない")


def check_relationships(rel_data: dict, persona_ids: set[str], exec_ids: set[str]) -> None:
    matrix = rel_data["address_matrix"]
    for pid in persona_ids:
        expect(pid in matrix, f"relationships: address_matrix に {pid} がない")
    for speaker, row in matrix.items():
        expect(speaker in exec_ids, f"relationships: 未知のID {speaker}")
        for target in row:
            expect(target in exec_ids | {"user"}, f"relationships: 未知のID {target}")
    for pair in rel_data["pairs"]:
        for pid in pair["between"]:
            expect(pid in exec_ids, f"relationships: pairs に未知のID {pid}")


# ---------------------------------------------------------------------
# 全文チェック（曜日・FY表記）
# ---------------------------------------------------------------------
def check_text_files() -> None:
    for path in sorted(ROOT.rglob("*")):
        if path.suffix not in {".md", ".yaml"} or "scripts" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for m in DATE_WITH_WEEKDAY.finditer(text):
            y, mo, d, w = int(m[1]), int(m[2]), int(m[3]), m[4]
            actual = WEEKDAYS[dt.date(y, mo, d).weekday()]
            expect(actual == w, f"{rel(path)}: {m[0]} の曜日は（{actual}）")
        for m in FY_LABEL.finditer(text):
            warnings.append(f"{rel(path)}: 決算期は「20XX年2月期」で表記する（{m[0]}）")


def main() -> int:
    facts = load_yaml(ROOT / "company" / "facts.yaml")
    if not facts:
        print("facts.yaml を読めないため中断します")
        return 1
    check_facts(facts)

    issue_ids = {i["id"] for i in facts["open_issues"]}
    issues_md = (ROOT / "company" / "06_open_issues.md")
    if issues_md.exists():
        text = issues_md.read_text(encoding="utf-8")
        for iid in issue_ids:
            expect(iid in text, f"06_open_issues.md に論点ID {iid} がない")

    persona_dirs = sorted(p for p in (ROOT / "personas").iterdir() if p.is_dir()) if (ROOT / "personas").exists() else []
    for pdir in persona_dirs:
        check_persona(pdir, facts, issue_ids)

    rel_path = ROOT / "relationships.yaml"
    if rel_path.exists():
        rel_data = load_yaml(rel_path)
        if rel_data:
            check_relationships(rel_data, {p.name for p in persona_dirs}, {e["id"] for e in facts["executives"]})

    for path in sorted(ROOT.rglob("*.yaml")):
        load_yaml(path)
    for path in sorted(ROOT.rglob("*.md")):
        split_front_matter(path)
    check_text_files()

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(f"\n{len(errors)} errors, {len(warnings)} warnings"
          f"（ペルソナ {len(persona_dirs)} 名、基準日 {facts['as_of']}）")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
