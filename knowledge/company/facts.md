---
type: Dataset
title: 数値・事実の正（facts.yaml）
description: 財務、中計、店舗、物流、デジタル、IT、人員、株式、経営陣、会議、論点、競合の数値と事実。Single Source of Truth。
resource: ./facts.yaml
tags: [facts, financials, kpi, ssot]
issues: [h1_recovery, tide_budget, logi_cloud_carveout, ai_ordering_rollout, security_budget, driver_shortage, rural_stores, it_cost_cut]
load: always
generated: { by: human:minatoya-dataset, at: 2026-11-01T00:00:00+09:00 }
status: stable
---

# 概要

数値はまず [facts.yaml](./facts.yaml) に書き、ほかの資料はそれに合わせます。整合性は `scripts/validate.py` で確かめます。

# 主なキー

| キー | 内容 |
|---|---|
| `financials` | 5期の推移、2027年2月期の計画と上期実績、計画差異の要因 |
| `mid_term_plan` | 中計の目標と投資枠 |
| `stores` / `store_ops` / `rural` | 店舗数、オペレーション、中山間地 |
| `logistics` / `digital` / `it` | 物流、ミナトヤ・デジタル、TIDE・セキュリティ・IT費 |
| `executives` / `board` / `meetings` | 経営陣、取締役会、会議の予定 |
| `open_issues` | 経営の論点8件（ID は各ペルソナの stances と対応） |
