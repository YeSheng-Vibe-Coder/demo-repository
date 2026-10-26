"""Rule version changelog: a Markdown record generated from the stored version history.

Every entry comes from frozen artifacts (candidates, evaluations, published versions and
monitor checks), so the document can be regenerated at any time and never drifts from the
registry. No account identifiers or labels of individual cases appear in it.
"""

from __future__ import annotations

import time

from .rule_versions import history

SOURCE = {
    "initial_training": "初始训练（决策树在开发集标签上拟合）",
    "rule_update_agent": "规则更新 agent 触发（依据调查结论与标签反馈制定方案）",
    "rule_update_fallback": "规则更新（agent 方案无效，沿用上一版配置）",
}
OBJECTIVE = {
    "chain_recall": "高召回：验证期链路召回 ≥ 97%，再比告警量与 precision",
    "daily_budget": "旧目标：每天 200 个告警下的 TP 与 precision",
    "reference": "人工设定阈值",
}
SYMBOL = {"ge": "≥", "le": "≤"}


def pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def num(value) -> str:
    return "—" if value is None else f"{round(value):,}"


def rules_text(program: dict) -> list[str]:
    atoms = {r["rule_id"]: r for r in program["rules"]}
    return [
        f"{index}. " + " 且 ".join(f"`{atoms[r]['feature_id']}` {SYMBOL[atoms[r]['operator']]} {atoms[r]['threshold']:g}" for r in group["rule_ids"])
        for index, group in enumerate(program["groups"], 1)
    ]


def metrics_rows(entry: dict) -> list[str]:
    now = (entry.get("metrics") or {}).get("validation") or {}
    before = (entry.get("metrics") or {}).get("parent_validation") or {}
    if entry.get("objective") == "chain_recall":
        rows = [("链路召回", pct(now.get("chain_recall")), pct(before.get("chain_recall"))),
                ("每天告警账户日", num(now.get("flagged_per_day")), num(before.get("flagged_per_day"))),
                ("窗口 precision", pct(now.get("window_precision")), pct(before.get("window_precision")))]
    else:
        rows = [("告警真正例 TP", num(now.get("tp")), num(before.get("tp"))),
                ("告警 precision", pct(now.get("precision")), pct(before.get("precision")))]
    return ["| 验证期指标 | 本版本 | 父版本 |", "|---|---|---|"] + [f"| {a} | {b} | {c} |" for a, b, c in rows]


def monitor_line(store, version_id: str) -> str | None:
    checks = [body for _, body in store.artifacts("rule_program_monitoring") if body.get("checked") and
              (body.get("active_version") == version_id or body.get("parent_version") and body.get("current") and body.get("active_version") == version_id)]
    if not checks:
        return None
    check = checks[-1]
    now, parent = check.get("current") or {}, check.get("parent") or {}
    if "chain_recall" in now:
        detail = f"链路召回 {pct(now['chain_recall'])}（父版本 {pct(parent.get('chain_recall'))}），每天告警 {num(now.get('flagged_per_day'))}（父版本 {num(parent.get('flagged_per_day'))}）"
    else:
        detail = f"TP {num(now.get('tp'))}（父版本 {num(parent.get('tp'))}），precision {pct(now.get('precision'))}"
    return f"{'已回滚' if check.get('rolled_back') else '未回滚'} · {detail}"


def diff_lines(diff: dict | None) -> list[str]:
    if not diff:
        return ["- 生成于规则对比功能上线之前，没有记录规则级变化。"]
    lines = [f"- 新增 {len(diff['added'])} 条 · 删除 {len(diff['removed'])} 条 · 调整 {len(diff['changed'])} 条 · 不变 {diff['unchanged']} 条"]
    lines += [f"- 新增：{' 且 '.join(g['conditions'])}" for g in diff["added"]]
    lines += [f"- 删除：{' 且 '.join(g['conditions'])}" for g in diff["removed"]]
    for change in diff["changed"]:
        parts = [f"{t['condition']} {t['from']:g} → {t['to']:g}" for t in change["thresholds"]]
        parts += [f"＋ {c}" for c in change["added_conditions"]] + [f"－ {c}" for c in change["removed_conditions"]]
        lines.append(f"- 调整：{'；'.join(parts)}")
    return lines


def render(store, active_version: str | None) -> str:
    entries = history(store)
    published = [e for e in entries if e["status"] in ("published", "reference")]
    out = [
        "# 规则版本记录",
        "",
        f"由 `python -m aml_agent.cli rule-changelog` 根据规则版本库自动生成（{time.strftime('%Y-%m-%d %H:%M')}），不要手工修改。",
        "评估都在 Zetaris 内完成：验证期 09-05–09-25 用于门禁，监控期 10-21–11-03 用于回滚检查，测试期始终关闭。",
        "",
        "## 总览",
        "",
        "| 版本 | 状态 | 来源 | 目标 | 父版本 | 验证期链路召回 | 每天告警 | 门禁结论 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for entry in entries:
        m = (entry.get("metrics") or {}).get("validation") or {}
        state = "当前生效" if entry["id"] == active_version else {"published": "已发布", "held": "暂缓", "reference": "参考"}[entry["status"]]
        out.append(f"| {entry['label']} | {state} | {SOURCE.get(entry.get('trigger'), '人工设定').split('（')[0]} | "
                   f"{OBJECTIVE.get(entry.get('objective'), '—').split('：')[0]} | {entry.get('parent_label') or '—'} | "
                   f"{pct(m.get('chain_recall'))} | {num(m.get('flagged_per_day'))} | "
                   f"{'接受' if entry.get('decision') == 'accept_for_experiment' else '暂缓' if entry.get('decision') else '—'} |")
    for entry in reversed(entries):
        if entry["status"] == "held" and entry.get("objective") != "chain_recall":
            continue  # Held candidates of the retired objective stay in the table only.
        plan = entry.get("plan") or {}
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["created"])) if entry.get("created") else "—"
        out += ["", f"## {entry['label']}{' · 当前生效' if entry['id'] == active_version else ''}", ""]
        out += [f"- 状态：{ {'published': '已发布', 'held': '暂缓（未发布）', 'reference': '参考规则'}[entry['status']] }",
                f"- 来源：{SOURCE.get(entry.get('trigger'), '人工设定的参考阈值')}",
                f"- 目标：{OBJECTIVE.get(entry.get('objective'), '—')}",
                f"- 父版本：{entry.get('parent_label') or '无'} · 生成时间：{created}",
                f"- 版本 ID：`{entry['id']}`"]
        if entry.get("trigger"):
            out.append(f"- 训练配置：{len(plan.get('features', []))} 个指标，"
                       + (f"最多 {plan['max_leaf_nodes']} 个叶子" if plan.get("max_leaf_nodes") else f"树深 {plan.get('max_depth', '—')}")
                       + f"，最小叶子 {plan.get('min_samples_leaf', '—')}"
                       + (f"；训练反馈包含 {entry['investigated_cases']} 个调查案件" if entry.get("investigated_cases") else ""))
            if (entry.get("fit") or {}).get("development_chain_recall") is not None:
                fit = entry["fit"]
                out.append(f"- 开发集拟合：链路召回 {pct(fit['development_chain_recall'])}，选用 {fit['chosen_leaves']}/{fit['leaves']} 个叶子，合并为 {fit['groups']} 条规则")
        if entry.get("decision"):
            out.append(f"- 门禁结论：{'接受' if entry['decision'] == 'accept_for_experiment' else '暂缓'} · {entry.get('reason', '')}")
        monitor = monitor_line(store, entry["id"]) if entry["status"] == "published" else None
        if monitor:
            out.append(f"- 监控期检查：{monitor}")
        if plan.get("rationale") and entry.get("trigger") != "initial_training":
            out += ["", f"> 规则更新 agent 的理由：{plan['rationale']}"]
        if entry.get("metrics"):
            out += [""] + metrics_rows(entry)
        if entry.get("status") != "reference":
            out += ["", "**相对父版本的规则变化**", ""] + diff_lines(entry.get("diff"))
        out += ["", f"**规则（{len(entry['program']['groups'])} 条，任一成立即告警）**", ""] + rules_text(entry["program"])
    out += ["", f"共 {len(published)} 个已发布或参考版本，{len(entries) - len(published)} 个暂缓候选。", ""]
    return "\n".join(out)
