#!/usr/bin/env python3
"""Create a reproducible, standard-library-only audit of completed iterations."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
ANALYSIS = ROOT / "analysis"
PLOTS = ROOT / "plots"
OPERATIONAL_FIELDS = (
    "base_count",
    "review_count",
    "review_trigger",
    "review_mode",
    "review_style",
    "candidate_limit",
    "candidate_source",
    "show_frequencies",
    "final_rule",
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def signature(strategy: dict[str, Any]) -> str:
    value = {
        key: strategy.get(key, "base_unique" if key == "candidate_source" else None)
        for key in OPERATIONAL_FIELDS
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def mechanism_label(strategy: dict[str, Any]) -> str:
    base = strategy["base_count"]
    reviews = strategy["review_count"]
    mode = strategy["review_mode"]
    rule = strategy["final_rule"]
    if base == 1 and reviews == 0:
        return "Direct one-call baseline"
    if reviews == 0 and rule == "base_plurality":
        return f"{base}-solver plurality"
    if mode == "repair":
        return f"{base}-bank + {reviews} parallel repair"
    if mode == "choose":
        return f"{base}-bank + {reviews} choose-only selector"
    if mode == "cross_examine":
        return f"{base}-bank + {reviews}-step cross-examination"
    if mode == "regenerate":
        return f"{base}-bank + {reviews} blind regenerators"
    if mode == "regenerate_then_repair":
        return f"{base}-bank + 3 regenerators + integrator"
    return strategy["name"]


def wilson(correct: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    p = correct / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def exact_two_sided_binomial(left_only: int, right_only: int) -> float:
    discordant = left_only + right_only
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(min(left_only, right_only) + 1)) / (2**discordant)
    return min(1.0, 2 * tail)


def pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def svg_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def main() -> None:
    state = read_json(ROOT / "state.json")
    iterations = [int(value) for value in state["completed_iterations"]]
    summaries = {
        number: read_json(ROOT / "iterations" / f"iteration-{number:03d}" / "results" / "summary.json")
        for number in iterations
    }

    groups: dict[str, dict[str, Any]] = {}
    iteration_signatures: dict[int, dict[str, str]] = {}
    for number, summary in summaries.items():
        iteration_signatures[number] = {}
        for strategy in summary["strategies"]:
            key = signature(strategy)
            iteration_signatures[number][strategy["strategy_id"]] = key
            group = groups.setdefault(
                key,
                {
                    "signature": key,
                    "label": mechanism_label(strategy),
                    "panels": 0,
                    "iterations": [],
                    "correct": 0,
                    "total": 0,
                    "partial_weight": 0.0,
                    "call_weight": 0.0,
                    "base_correct": 0,
                    "base_oracle_correct": 0,
                    "expanded_oracle_correct": 0,
                    "expanded_oracle_total": 0,
                    "expanded_final_correct": 0,
                    "helpful": 0,
                    "harmful": 0,
                    "families": defaultdict(lambda: [0, 0]),
                    "strategy_ids": Counter(),
                    "strategy": strategy,
                },
            )
            group["panels"] += 1
            group["iterations"].append(number)
            group["correct"] += strategy["correct"]
            group["total"] += strategy["total"]
            group["partial_weight"] += strategy["partial_accuracy"] * strategy["total"]
            group["call_weight"] += strategy["mean_effective_calls"] * strategy["total"]
            helpful = int(strategy.get("helpful_interventions", 0))
            harmful = int(strategy.get("harmful_interventions", 0))
            group["helpful"] += helpful
            group["harmful"] += harmful
            group["base_correct"] += strategy["correct"] - helpful + harmful
            group["base_oracle_correct"] += int(strategy.get("base_oracle_correct", strategy["correct"]))
            if "expanded_oracle_correct" in strategy:
                group["expanded_oracle_correct"] += int(strategy["expanded_oracle_correct"])
                group["expanded_oracle_total"] += int(strategy["total"])
                group["expanded_final_correct"] += int(strategy["correct"])
            group["strategy_ids"][strategy["strategy_id"]] += 1
            group["strategy"] = strategy
            for family, metrics in strategy["families"].items():
                group["families"][family][0] += metrics["correct"]
                group["families"][family][1] += metrics["total"]

    mechanism_rows: list[dict[str, Any]] = []
    for group in groups.values():
        total = group["total"]
        accuracy = group["correct"] / total
        low, high = wilson(group["correct"], total)
        family_accuracy = {
            family: correct / family_total
            for family, (correct, family_total) in sorted(group["families"].items())
            if family_total
        }
        expanded = group["expanded_oracle_correct"]
        expanded_total = group["expanded_oracle_total"]
        mechanism_rows.append(
            {
                "label": group["label"],
                "signature": group["signature"],
                "strategy_ids": ";".join(sorted(group["strategy_ids"])),
                "panels": group["panels"],
                "iterations": ";".join(str(value) for value in group["iterations"]),
                "correct": group["correct"],
                "total": total,
                "accuracy": accuracy,
                "ci95_low": low,
                "ci95_high": high,
                "worst_family_accuracy": min(family_accuracy.values()),
                "constraint_accuracy": family_accuracy.get("constraint"),
                "logic_accuracy": family_accuracy.get("logic"),
                "sequence_accuracy": family_accuracy.get("sequence"),
                "mean_calls": group["call_weight"] / total,
                "base_accuracy": group["base_correct"] / total,
                "base_oracle_accuracy": group["base_oracle_correct"] / total,
                "expanded_oracle_cases": expanded_total,
                "expanded_oracle_accuracy": expanded / expanded_total if expanded_total else None,
                "oracle_conversion": group["expanded_final_correct"] / expanded if expanded else None,
                "helpful_interventions": group["helpful"],
                "harmful_interventions": group["harmful"],
                "net_corrections": group["helpful"] - group["harmful"],
            }
        )
    mechanism_rows.sort(key=lambda row: (row["accuracy"], row["total"]), reverse=True)

    case_values: dict[tuple[int, str], dict[str, int]] = defaultdict(dict)
    case_diagnostics: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for number in iterations:
        path = ROOT / "iterations" / f"iteration-{number:03d}" / "results" / "case_results.json"
        for row in read_json(path):
            key = iteration_signatures[number][row["strategy_id"]]
            case_values[(number, key)][row["case_id"]] = int(row["exact"])
            diagnostics = case_diagnostics[key]
            diagnostics["base_total"] += 1
            diagnostics["base_covered"] += int(row["base_oracle"])
            diagnostics["base_covered_final"] += int(bool(row["base_oracle"]) and bool(row["exact"]))
            diagnostics["base_uncovered_final"] += int(not row["base_oracle"] and bool(row["exact"]))
            if "expanded_oracle" in row:
                diagnostics["expanded_total"] += 1
                diagnostics["expanded_covered"] += int(row["expanded_oracle"])
                diagnostics["expanded_covered_final"] += int(bool(row["expanded_oracle"]) and bool(row["exact"]))
                diagnostics["expanded_uncovered_final"] += int(not row["expanded_oracle"] and bool(row["exact"]))

    for row in mechanism_rows:
        diagnostics = case_diagnostics[row["signature"]]
        base_covered = diagnostics["base_covered"]
        base_uncovered = diagnostics["base_total"] - base_covered
        row["base_oracle_conversion"] = (
            diagnostics["base_covered_final"] / base_covered if base_covered else None
        )
        row["base_no_oracle_rescue"] = (
            diagnostics["base_uncovered_final"] / base_uncovered if base_uncovered else None
        )
        if diagnostics["expanded_total"]:
            expanded_covered = diagnostics["expanded_covered"]
            expanded_uncovered = diagnostics["expanded_total"] - expanded_covered
            row["expanded_oracle_cases"] = diagnostics["expanded_total"]
            row["expanded_oracle_accuracy"] = expanded_covered / diagnostics["expanded_total"]
            row["oracle_conversion"] = (
                diagnostics["expanded_covered_final"] / expanded_covered if expanded_covered else None
            )
            row["expanded_no_oracle_rescue"] = (
                diagnostics["expanded_uncovered_final"] / expanded_uncovered if expanded_uncovered else None
            )
        else:
            row["expanded_no_oracle_rescue"] = None

    incumbent_signature = next(
        row["signature"]
        for row in mechanism_rows
        if row["label"] == "5-bank + 3 parallel repair"
        and '"candidate_limit":5' in row["signature"]
        and '"candidate_source":"base_unique"' in row["signature"]
        and '"final_rule":"review_plurality_fallback_base"' in row["signature"]
        and '"show_frequencies":false' in row["signature"]
    )

    paired_rows: list[dict[str, Any]] = []
    for candidate in mechanism_rows:
        candidate_signature = candidate["signature"]
        if candidate_signature == incumbent_signature:
            continue
        pairs: list[tuple[int, int]] = []
        matched_iterations: list[int] = []
        for number in iterations:
            incumbent = case_values.get((number, incumbent_signature))
            challenger = case_values.get((number, candidate_signature))
            if incumbent is None or challenger is None:
                continue
            common = sorted(set(incumbent) & set(challenger))
            if common:
                matched_iterations.append(number)
                pairs.extend((incumbent[case_id], challenger[case_id]) for case_id in common)
        if not pairs:
            continue
        both = sum(left == 1 and right == 1 for left, right in pairs)
        incumbent_only = sum(left == 1 and right == 0 for left, right in pairs)
        challenger_only = sum(left == 0 and right == 1 for left, right in pairs)
        neither = len(pairs) - both - incumbent_only - challenger_only
        paired_rows.append(
            {
                "challenger": candidate["label"],
                "challenger_signature": candidate_signature,
                "panels": len(matched_iterations),
                "iterations": ";".join(str(value) for value in matched_iterations),
                "cases": len(pairs),
                "both_correct": both,
                "incumbent_only_correct": incumbent_only,
                "challenger_only_correct": challenger_only,
                "neither_correct": neither,
                "incumbent_accuracy": (both + incumbent_only) / len(pairs),
                "challenger_accuracy": (both + challenger_only) / len(pairs),
                "challenger_delta": (challenger_only - incumbent_only) / len(pairs),
                "exact_binomial_p": exact_two_sided_binomial(incumbent_only, challenger_only),
            }
        )
    paired_rows.sort(key=lambda row: (row["cases"], row["challenger_delta"]), reverse=True)

    phases: list[dict[str, Any]] = []
    for start in (1, 11, 21):
        selected = [summaries[number] for number in iterations if start <= number <= start + 9]
        total_cases = sum(summary["cases"] for summary in selected)
        phases.append(
            {
                "iterations": f"{start}-{start + 9}",
                "cases": total_cases,
                "worker_calls": sum(summary["worker_calls"] for summary in selected),
                "transport_attempts": sum(summary["transport_attempts"] for summary in selected),
                "panel_winner_accuracy": sum(summary["winner"]["correct"] for summary in selected) / total_cases,
                "direct_accuracy": sum(
                    next(item for item in summary["strategies"] if item["strategy_id"] == "direct-1")["correct"]
                    for summary in selected
                )
                / total_cases,
                "plurality_accuracy": sum(
                    next(item for item in summary["strategies"] if item["strategy_id"] == "plurality-5")["correct"]
                    for summary in selected
                )
                / total_cases,
            }
        )

    ANALYSIS.mkdir(exist_ok=True)
    with (ANALYSIS / "iteration-030-mechanisms.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(mechanism_rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(mechanism_rows)
    with (ANALYSIS / "iteration-030-paired.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(paired_rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(paired_rows)

    summary_output = {
        "generated_from_completed_iterations": iterations,
        "invalid_archived_runs_included": False,
        "total_fresh_cases": sum(summary["cases"] for summary in summaries.values()),
        "total_worker_calls": sum(summary["worker_calls"] for summary in summaries.values()),
        "total_transport_attempts": sum(summary["transport_attempts"] for summary in summaries.values()),
        "families": {family: sum(summary["cases"] for summary in summaries.values()) // 3 for family in ("constraint", "logic", "sequence")},
        "incumbent_signature": incumbent_signature,
        "phases": phases,
        "mechanisms": mechanism_rows,
        "paired_comparisons": paired_rows,
    }
    write_json(ANALYSIS / "iteration-030-summary.json", summary_output)

    preferred = [
        "Direct one-call baseline",
        "5-solver plurality",
        "5-bank + 1 parallel repair",
        "5-bank + 3 parallel repair",
        "9-bank + 1 parallel repair",
        "9-bank + 1 choose-only selector",
        "5-bank + 3-step cross-examination",
        "5-bank + 3 regenerators + integrator",
    ]
    plot_rows: list[dict[str, Any]] = []
    for label in preferred:
        candidates = [row for row in mechanism_rows if row["label"] == label]
        if not candidates:
            continue
        candidates.sort(key=lambda row: (row["total"], row["panels"]), reverse=True)
        plot_rows.append(candidates[0])

    width, height = 1320, 900
    left, right = 440, 1190
    top, row_height = 164, 62
    x_max = 0.65

    def x(value: float) -> float:
        return left + value / x_max * (right - left)

    palette = ["#9ca9c7", "#7d8cae", "#6fd6ff", "#63f2bd", "#ffc95c", "#ff8bd4", "#bb9cff", "#ff927a"]
    svg: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#070b15"/>',
        '<rect x="24" y="24" width="1272" height="852" rx="28" fill="#0d1424" stroke="#263554" stroke-width="2"/>',
        '<text x="64" y="76" fill="#f4f7ff" font-family="Inter,system-ui,sans-serif" font-size="30" font-weight="700">Thirty-iteration deep evaluation</text>',
        '<text x="64" y="112" fill="#9eacc8" font-family="Inter,system-ui,sans-serif" font-size="17">Exact accuracy on fresh sealed development panels · bars show 95% Wilson intervals</text>',
    ]
    for tick in range(0, 7):
        value = tick / 10
        position = x(value)
        svg.append(f'<line x1="{position:.1f}" y1="140" x2="{position:.1f}" y2="{top + row_height * len(plot_rows) - 16}" stroke="#25314a" stroke-width="1"/>')
        svg.append(f'<text x="{position:.1f}" y="136" text-anchor="middle" fill="#7382a0" font-family="ui-monospace,monospace" font-size="13">{tick * 10}%</text>')
    for index, row in enumerate(plot_rows):
        y = top + index * row_height + 22
        color = palette[index % len(palette)]
        label = svg_escape(row["label"])
        svg.append(f'<text x="64" y="{y + 5}" fill="#e9eefc" font-family="Inter,system-ui,sans-serif" font-size="17" font-weight="600">{label}</text>')
        svg.append(f'<text x="418" y="{y + 5}" text-anchor="end" fill="#7f8eaa" font-family="ui-monospace,monospace" font-size="12">n={row["total"]} · {row["mean_calls"]:.0f} calls</text>')
        svg.append(f'<line x1="{x(row["ci95_low"]):.1f}" y1="{y}" x2="{x(row["ci95_high"]):.1f}" y2="{y}" stroke="{color}" stroke-width="8" stroke-linecap="round" opacity="0.42"/>')
        svg.append(f'<circle cx="{x(row["accuracy"]):.1f}" cy="{y}" r="9" fill="{color}" stroke="#f7fbff" stroke-width="2"/>')
        svg.append(f'<text x="{x(row["accuracy"]) + 16:.1f}" y="{y + 5}" fill="{color}" font-family="ui-monospace,monospace" font-size="14" font-weight="700">{pct(row["accuracy"])}</text>')
    bottom = top + row_height * len(plot_rows) + 18
    svg.extend(
        [
            f'<line x1="64" y1="{bottom}" x2="1256" y2="{bottom}" stroke="#263554"/>',
            f'<text x="64" y="{bottom + 38}" fill="#63f2bd" font-family="Inter,system-ui,sans-serif" font-size="17" font-weight="700">Robust result</text>',
            f'<text x="190" y="{bottom + 38}" fill="#cbd4e8" font-family="Inter,system-ui,sans-serif" font-size="16">The retained 5 + 3 repair swarm reaches 41.2%, versus 9.3% direct and 12.1% plurality.</text>',
            f'<text x="64" y="{bottom + 70}" fill="#ffc95c" font-family="Inter,system-ui,sans-serif" font-size="17" font-weight="700">Search result</text>',
            f'<text x="190" y="{bottom + 70}" fill="#cbd4e8" font-family="Inter,system-ui,sans-serif" font-size="16">No later mechanism has yet produced a matched, statistically persuasive improvement over the incumbent.</text>',
            f'<text x="64" y="{bottom + 102}" fill="#ff8bd4" font-family="Inter,system-ui,sans-serif" font-size="17" font-weight="700">Main bottleneck</text>',
            f'<text x="190" y="{bottom + 102}" fill="#cbd4e8" font-family="Inter,system-ui,sans-serif" font-size="16">Candidate banks often contain the truth, but weak integration leaves correct minority answers unused.</text>',
            '</svg>',
        ]
    )
    PLOTS.mkdir(exist_ok=True)
    (PLOTS / "deep-evaluation-30.svg").write_text("".join(svg), encoding="utf-8")

    print(
        json.dumps(
            {
                "iterations": len(iterations),
                "cases": summary_output["total_fresh_cases"],
                "worker_calls": summary_output["total_worker_calls"],
                "mechanisms": len(mechanism_rows),
                "outputs": [
                    str(ANALYSIS / "iteration-030-summary.json"),
                    str(ANALYSIS / "iteration-030-mechanisms.csv"),
                    str(ANALYSIS / "iteration-030-paired.csv"),
                    str(PLOTS / "deep-evaluation-30.svg"),
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
