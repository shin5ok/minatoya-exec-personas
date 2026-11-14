"""knowledge/（OKF v0.2 バンドル）を読み込み、エージェントに渡す形に整える。

- コンセプトは frontmatter（type / title / description / tags / issues / load）で選ぶ
- ペルソナのシステムプロンプトは scripts/build_prompt.py（データセット付属の組み立て器）で作る
- 数値の正は company/facts.yaml。資料にない数字は作らせない
"""

from __future__ import annotations

import functools
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from app import config

RESERVED = {"index.md", "log.md"}


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    return yaml.safe_load(text[4:end]) or {}, text[end + 5 :]


@dataclass(frozen=True)
class Concept:
    """OKF のコンセプト 1 件。id はバンドル直下からのパス（.md なし）。"""

    id: str
    path: Path
    meta: dict[str, Any] = field(hash=False)
    body: str = field(hash=False)

    @property
    def type(self) -> str:
        return str(self.meta.get("type", ""))

    @property
    def title(self) -> str:
        return str(self.meta.get("title", self.path.stem))

    @property
    def load(self) -> str:
        return str(self.meta.get("load", "on_demand"))

    @property
    def issues(self) -> list[str]:
        return list(self.meta.get("issues") or [])

    @property
    def description(self) -> str:
        return str(self.meta.get("description", ""))


@dataclass(frozen=True)
class PersonaInfo:
    id: str
    name: str
    kana: str
    title: str
    age: int
    role_label: str
    tagline: str
    color: str
    voice: str
    avatar: str
    avatar_talk: str
    card: str

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kana": self.kana,
            "title": self.title,
            "age": self.age,
            "role_label": self.role_label,
            "tagline": self.tagline,
            "color": self.color,
            "avatar": self.avatar,
            "avatar_talk": self.avatar_talk,
        }


class KnowledgeBundle:
    def __init__(self, root: Path):
        self.root = root
        self.concepts: dict[str, Concept] = {}
        for p in sorted(root.rglob("*.md")):
            if p.name in RESERVED:
                continue
            meta, body = split_front_matter(p.read_text(encoding="utf-8"))
            cid = p.relative_to(root).with_suffix("").as_posix()
            self.concepts[cid] = Concept(id=cid, path=p, meta=meta, body=body)
        self.facts: dict[str, Any] = self._yaml("company/facts.yaml")
        self.relationships: dict[str, Any] = self._yaml("relationships.yaml")
        self.personas: dict[str, PersonaInfo] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        for c in self.concepts.values():
            if c.type != "Persona":
                continue
            pid = c.meta["persona_id"]
            prof = self._yaml(f"personas/{pid}/profile.yaml")
            ui = c.meta.get("ui") or {}
            self.profiles[pid] = prof
            self.personas[pid] = PersonaInfo(
                id=pid,
                name=prof["display_name"],
                kana=prof["kana"],
                title=" ／ ".join(prof["current_titles"]),
                age=prof["age"],
                role_label=ui.get("role_label", pid),
                tagline=ui.get("tagline", ""),
                color=ui.get("color", "#888888"),
                voice=ui.get("voice", "Charon"),
                avatar=ui.get("avatar", ""),
                avatar_talk=ui.get("avatar_talk", ui.get("avatar", "")),
                card=prof["card"].strip(),
            )
        order = ["president", "cto", "cio"]
        self.personas = dict(
            sorted(self.personas.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 99, kv[0]))
        )

    def _yaml(self, rel: str) -> dict[str, Any]:
        return yaml.safe_load((self.root / rel).read_text(encoding="utf-8")) or {}

    # --- 論点 -----------------------------------------------------------------
    @property
    def open_issues(self) -> list[dict[str, Any]]:
        return list(self.facts.get("open_issues", []))

    def issue_title(self, issue_id: str) -> str:
        return next((i["title"] for i in self.open_issues if i["id"] == issue_id), issue_id)

    # --- 選択・本文 -----------------------------------------------------------
    def catalog(self, persona_ids: list[str] | None = None) -> str:
        """資料選び（librarian）に渡す目次。OKF の段階的開示にならい、本文は渡さない。"""
        lines = []
        for c in self.concepts.values():
            if c.type == "Persona":
                continue
            if c.type == "Speech" and persona_ids and c.meta.get("speaker") not in persona_ids:
                continue
            issues = ",".join(c.issues) or "-"
            lines.append(f"- id={c.id} | type={c.type} | load={c.load} | issues={issues} | {c.title}: {c.description}")
        return "\n".join(lines)

    def always_ids(self) -> list[str]:
        return [c.id for c in self.concepts.values() if c.load == "always" and c.type != "Persona"]

    def few_shot_ids(self) -> list[str]:
        return [c.id for c in self.concepts.values() if c.load == "few_shot"]

    def ids_for_issues(self, issue_ids: list[str], persona_ids: list[str]) -> list[str]:
        """論点 ID から、関係する資料と講演録を決め打ちで選ぶ（librarian が使えないときの代替）。"""
        out = []
        for c in self.concepts.values():
            if c.load != "on_demand":
                continue
            if c.type == "Speech" and c.meta.get("speaker") not in persona_ids:
                continue
            if set(c.issues) & set(issue_ids):
                out.append(c.id)
        return out

    def render(self, cid: str, max_chars: int | None = None) -> str:
        """コンセプト 1 件を、出典つきの Markdown にする。Dataset は resource の YAML を添える。"""
        c = self.concepts[cid]
        body = c.body.strip()
        resource = c.meta.get("resource")
        if c.type == "Dataset" and isinstance(resource, str) and resource.endswith((".yaml", ".yml")):
            raw = (c.path.parent / resource).read_text(encoding="utf-8").strip()
            body = f"```yaml\n{raw}\n```"
        if max_chars and len(body) > max_chars:
            body = body[:max_chars] + "\n…（以下略）"
        return f"<document id=\"{cid}\" type=\"{c.type}\" title=\"{c.title}\">\n{body}\n</document>"

    def render_many(self, ids: list[str], max_chars_total: int | None = None) -> str:
        parts, total = [], 0
        for cid in dict.fromkeys(ids):
            if cid not in self.concepts:
                continue
            text = self.render(cid)
            if max_chars_total and total + len(text) > max_chars_total:
                remaining = max_chars_total - total
                if remaining > 2000:
                    parts.append(self.render(cid, max_chars=remaining - 500))
                break
            parts.append(text)
            total += len(text)
        return "\n\n".join(parts)

    # --- ペルソナ -------------------------------------------------------------
    def persona_prompt(self, pid: str, scene: str) -> str:
        return _build_prompt_module().build(pid, scene)

    def address(self, speaker: str, target: str) -> str:
        return str(self.relationships.get("address_matrix", {}).get(speaker, {}).get(target, ""))


@functools.cache
def _build_prompt_module() -> ModuleType:
    """データセット付属の scripts/build_prompt.py を、そのまま部品として読み込む。"""
    path = config.ROOT / "scripts" / "build_prompt.py"
    spec = importlib.util.spec_from_file_location("minatoya_build_prompt", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = config.KNOWLEDGE_DIR  # KNOWLEDGE_DIR の上書きに追従させる
    return module


@functools.cache
def get_bundle() -> KnowledgeBundle:
    return KnowledgeBundle(config.KNOWLEDGE_DIR)
