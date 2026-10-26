"""Rule version history: every published version and every held candidate, with its
rule details, its measured effect and its change against the parent it was compared to.

Versions are numbered in publication order (v0 is the hand-set reference rule); a
held candidate keeps the number it would have taken, marked as not published.
"""

from __future__ import annotations

from .rule_program import program_of

SYMBOL = {"ge": "≥", "le": "≤"}


def conditions(program: dict) -> dict[str, dict[tuple[str, str], float]]:
    rules = {r["rule_id"]: r for r in program["rules"]}
    return {
        g["group_id"]: {(rules[r]["feature_id"], rules[r]["operator"]): rules[r]["threshold"] for r in g["rule_ids"]}
        for g in program["groups"]
    }


def text(condition: dict[tuple[str, str], float]) -> list[str]:
    return [f"{feature} {SYMBOL[op]} {threshold:g}" for (feature, op), threshold in sorted(condition.items())]


def diff(old: dict | None, new: dict) -> dict:
    """Rule-level change: groups matched by the overlap of their conditions (Jaccard ≥ 0.5)."""
    after = conditions(new)
    if old is None:
        return {"added": [{"group": g, "conditions": text(c)} for g, c in after.items()], "removed": [], "changed": [], "unchanged": 0}
    before = conditions(old)
    matched, used = [], set()
    for group, condition in after.items():
        scored = [
            (len(set(c) & set(condition)) / len(set(c) | set(condition)), g) for g, c in before.items() if g not in used
        ]
        best = max(scored, default=(0.0, None))
        if best[0] >= 0.5:
            used.add(best[1])
            matched.append((best[1], group))
    changed, unchanged = [], 0
    for old_group, new_group in matched:
        a, b = before[old_group], after[new_group]
        if a == b:
            unchanged += 1
            continue
        changed.append({
            "from": old_group, "to": new_group,
            "thresholds": [{"condition": f"{f} {SYMBOL[op]}", "from": a[(f, op)], "to": b[(f, op)]}
                           for (f, op) in sorted(a.keys() & b.keys()) if a[(f, op)] != b[(f, op)]],
            "added_conditions": text({k: b[k] for k in b.keys() - a.keys()}),
            "removed_conditions": text({k: a[k] for k in a.keys() - b.keys()}),
        })
    new_groups = {g for _, g in matched}
    return {
        "added": [{"group": g, "conditions": text(c)} for g, c in after.items() if g not in new_groups],
        "removed": [{"group": g, "conditions": text(c)} for g, c in before.items() if g not in used],
        "changed": changed,
        "unchanged": unchanged,
    }


def infer_trigger(candidate: dict) -> str:
    """Versions frozen before triggers were recorded: an update plan carries the agent's rationale."""
    rationale = (candidate.get("plan") or {}).get("rationale", "")
    if not rationale:
        return "initial_training"
    return "rule_update_fallback" if rationale.startswith("Fallback") else "rule_update_agent"


def history(store) -> list[dict]:
    """All rule versions and held candidates in evaluation order, newest last."""
    versions = dict(store.artifacts("rule_program_version"))
    by_evaluation = {body["evaluation"]["evaluation_id"]: vid for vid, body in versions.items()
                     if body.get("evaluation", {}).get("evaluation_id")}
    entries, number = [], 0
    reference = next(((vid, body) for vid, body in versions.items() if body.get("status") == "research_reference"), None)
    if reference:
        entries.append({"id": reference[0], "label": "v0", "status": "reference", "parent": None,
                        "program": program_of(reference[1]).model_dump(mode="json"), "metrics": None, "diff": None,
                        "basis": "Hand-set reference thresholds"})
    # Every stored copy of the hand-set reference is v0.
    labels = {vid: "v0" for vid, body in versions.items() if body.get("status") == "research_reference"}
    for eid, evaluation in store.artifacts("rule_program_evaluation"):
        try:
            candidate = store.read_artifact(evaluation["candidate_version"], "rule_program_candidate")
        except KeyError:
            continue
        published = by_evaluation.get(eid)
        number += 1 if published else 0
        label = f"v{number}" if published else f"v{number + 1} 候选"
        if published:
            labels[published] = label
        parent = evaluation.get("base_version")
        objective = "chain_recall" if "chain_recall" in (evaluation.get("candidate") or {}) else "daily_budget"
        entries.append({
            "id": published or evaluation["candidate_version"],
            "label": label,
            "status": "published" if published else "held",
            "decision": evaluation["decision"],
            "reason": evaluation["reason"],
            "objective": objective,
            "parent": parent,
            "parent_label": labels.get(parent),
            "program": program_of(candidate).model_dump(mode="json"),
            "group_stats": candidate.get("group_stats"),
            "explanation": candidate.get("explanation"),
            "plan": candidate.get("plan"),
            "fit": candidate.get("fit"),
            "trigger": candidate.get("trigger") or infer_trigger(candidate),
            "cycle_id": candidate.get("cycle_id"),
            "investigated_cases": candidate.get("investigated_cases"),
            "development_sample": candidate.get("development_sample"),
            "created": candidate.get("created"),
            "tree": candidate.get("tree"),
            "metrics": {"validation": evaluation.get("candidate"), "parent_validation": evaluation.get("baseline")},
            "diff": evaluation.get("diff"),
        })
    return entries
