#!/usr/bin/env python3
"""knowledge/ を Open Knowledge Format (OKF) v0.2 のバンドルとして扱う道具。

使い方:
    uv run python scripts/okf.py index   # 各ディレクトリの index.md を作り直す
    uv run python scripts/okf.py lint    # OKF v0.2 の適合性を確かめる（§11）
    uv run python scripts/okf.py list    # コンセプトの一覧（type / title / load）を表示

OKF の要点（仕様 §3〜§11）:
  - index.md / log.md 以外の .md はコンセプト。先頭に YAML の frontmatter を持ち、type が必須
  - index.md はディレクトリの目次（段階的に開示するためのもの）。frontmatter を持たない
    （バンドル直下の index.md だけ okf_version を持てる）
  - リンクはバンドル直下からの絶対パス（/company/facts.md）を推奨
このリポジトリ独自の拡張キー:
  - issues   関係する経営の論点 ID（company/facts.yaml の open_issues）
  - personas 関係するペルソナ ID
  - load     always（常に読む）/ on_demand（テーマに応じて読む）/ few_shot（口調の手本）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent / "knowledge"
OKF_VERSION = "0.2"
RESERVED = {"index.md", "log.md"}
SECTION_TITLES = {
    "": "ミナトヤ・ホールディングス 幹部ペルソナ・ナレッジバンドル（架空）",
    "company": "会社の共通情報",
    "personas": "ペルソナ",
    "scenarios": "シナリオと議事録",
}


def split_front_matter(text: str) -> tuple[dict | None, str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    return yaml.safe_load(text[4:end]) or {}, text[end + 5:]


def concepts(directory: Path) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(directory.glob("*.md")):
        if p.name in RESERVED:
            continue
        meta, _ = split_front_matter(p.read_text(encoding="utf-8"))
        out.append((p, meta or {}))
    return out


def subdirs(directory: Path) -> list[Path]:
    return sorted(d for d in directory.iterdir() if d.is_dir() and not d.name.startswith("."))


def dir_description(d: Path) -> str:
    persona = d / "persona.md"
    if persona.exists():
        meta, _ = split_front_matter(persona.read_text(encoding="utf-8"))
        return f"{meta.get('title', d.name)} — {meta.get('description', '')}"
    if d.name == "speeches":
        return "講演録・寄稿"
    return SECTION_TITLES.get(d.name, d.name)


def render_index(directory: Path) -> str:
    rel = directory.relative_to(ROOT).as_posix()
    rel = "" if rel == "." else rel
    lines = []
    if directory == ROOT:
        lines += ["---", f'okf_version: "{OKF_VERSION}"', "---", ""]
    lines.append(f"# {SECTION_TITLES.get(rel, directory.name)}")
    lines.append("")
    items = concepts(directory)
    if items:
        for p, meta in items:
            title = meta.get("title", p.stem)
            desc = meta.get("description", "")
            kind = meta.get("type", "?")
            lines.append(f"* [{title}]({p.name}) - [{kind}] {desc}".rstrip())
        lines.append("")
    data_files = sorted(p for p in directory.iterdir() if p.suffix in {".yaml", ".yml"})
    if data_files:
        lines.append("# 構造化データ")
        lines.append("")
        lines += [f"* [{p.name}]({p.name}) - 上のコンセプトの resource が指す YAML" for p in data_files]
        lines.append("")
    dirs = subdirs(directory)
    if dirs:
        lines.append("# サブディレクトリ")
        lines.append("")
        lines += [f"* [{d.name}]({d.name}/) - {dir_description(d)}" for d in dirs]
        lines.append("")
    return "\n".join(lines)


def cmd_index() -> int:
    targets = [ROOT, *[d for d in ROOT.rglob("*") if d.is_dir()]]
    for d in targets:
        (d / "index.md").write_text(render_index(d), encoding="utf-8")
        print(f"wrote {(d / 'index.md').relative_to(ROOT.parent)}")
    return 0


def cmd_lint() -> int:
    errors: list[str] = []
    n = 0
    for p in sorted(ROOT.rglob("*.md")):
        rel = p.relative_to(ROOT).as_posix()
        text = p.read_text(encoding="utf-8")
        if p.name == "index.md":
            meta, _ = split_front_matter(text)
            if meta is not None and (p.parent != ROOT or set(meta) - {"okf_version"}):
                errors.append(f"{rel}: index.md に frontmatter は置けない（直下の okf_version を除く）")
            continue
        if p.name == "log.md":
            continue
        n += 1
        try:
            meta, _ = split_front_matter(text)
        except yaml.YAMLError as e:
            errors.append(f"{rel}: frontmatter を読めない: {e}")
            continue
        if meta is None:
            errors.append(f"{rel}: frontmatter がない")
        elif not str(meta.get("type", "")).strip():
            errors.append(f"{rel}: type がない")
        elif meta.get("load") not in (None, "always", "on_demand", "few_shot"):
            errors.append(f"{rel}: load の値が不正: {meta.get('load')}")
    for d in [ROOT, *[d for d in ROOT.rglob("*") if d.is_dir()]]:
        idx = d / "index.md"
        if not idx.exists():
            errors.append(f"{d.relative_to(ROOT.parent)}: index.md がない（scripts/okf.py index で作る）")
        elif idx.read_text(encoding="utf-8") != render_index(d):
            errors.append(f"{idx.relative_to(ROOT.parent)}: 古い（scripts/okf.py index で作り直す）")
    for e in errors:
        print(f"ERROR {e}")
    print(f"\n{len(errors)} errors（コンセプト {n} 件、OKF v{OKF_VERSION}）")
    return 1 if errors else 0


def cmd_list() -> int:
    for p in sorted(ROOT.rglob("*.md")):
        if p.name in RESERVED:
            continue
        meta, _ = split_front_matter(p.read_text(encoding="utf-8"))
        meta = meta or {}
        print(f"{p.relative_to(ROOT).as_posix():60s} {meta.get('type', '?'):20s} {meta.get('load', '-'):10s} {meta.get('title', '')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OKF バンドル（knowledge/）の道具")
    parser.add_argument("command", choices=["index", "lint", "list"])
    args = parser.parse_args()
    return {"index": cmd_index, "lint": cmd_lint, "list": cmd_list}[args.command]()


if __name__ == "__main__":
    sys.exit(main())
