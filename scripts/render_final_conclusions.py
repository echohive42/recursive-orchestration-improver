#!/usr/bin/env python3
"""Render the concluded research summary from published evidence files."""

from __future__ import annotations

import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ANALYSIS = ROOT / "analysis" / "iteration-030-summary.json"
TRANSFER = ROOT / "transfer" / "cross-model-screen" / "results" / "summary.json"
OUTPUT = ROOT / "images" / "final-conclusions.svg"
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


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def signature(strategy: dict) -> str:
    value = {
        key: strategy.get(key, "base_unique" if key == "candidate_source" else None)
        for key in OPERATIONAL_FIELDS
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def main() -> None:
    analysis = load(ANALYSIS)
    transfer = load(TRANSFER)
    incumbent_signature = analysis["incumbent_signature"]

    paired = {row["challenger"]: row for row in analysis["paired_comparisons"]}
    plurality = paired["5-solver plurality"]
    direct = paired["Direct one-call baseline"]
    incumbent = next(
        row for row in analysis["mechanisms"] if row["signature"] == incumbent_signature
    )

    phase_values = []
    for start, end in ((1, 10), (11, 20), (21, 30)):
        correct = total = 0
        for number in range(start, end + 1):
            summary = load(ROOT / "iterations" / f"iteration-{number:03d}" / "results" / "summary.json")
            for strategy in summary["strategies"]:
                if signature(strategy) == incumbent_signature:
                    correct += int(strategy["correct"])
                    total += int(strategy["total"])
                    break
        phase_values.append((f"Rounds {start}-{end}", correct / total, correct, total))

    transfer_rows = {
        (row["condition_id"], row["strategy_id"]): row
        for row in transfer["summary"]["strategies"]
    }
    transfer_summary = [
        (
            "Luna Light",
            "one repairer",
            transfer_rows[("luna-light-existing", "repair-falsify-5x1-efficient")]["accuracy"],
        ),
        (
            "Terra Low",
            "one repairer was enough",
            transfer_rows[("terra-low", "repair-falsify-5x1-efficient")]["accuracy"],
        ),
        (
            "Luna Medium",
            "cross-examination",
            transfer_rows[("luna-medium", "cross-examine-falsify-5x3")]["accuracy"],
        ),
    ]

    colors = {
        "bg0": "#07101f",
        "bg1": "#10132a",
        "panel": "#111b31",
        "border": "#2b3c5b",
        "white": "#f6f9ff",
        "muted": "#9babc4",
        "cyan": "#48d7ef",
        "mint": "#5ee7c5",
        "violet": "#9b87ff",
        "amber": "#ffc36b",
        "grid": "#2a3851",
    }

    width = 1600
    height = 1000
    svg: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        '<title id="title">Final conclusions from the Recursive Orchestration Improver</title>',
        '<desc id="desc">A concluded research summary showing a strong repair effect, no improvement of the fixed swarm over time, and model-dependent value from deeper orchestration.</desc>',
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{colors["bg0"]}"/><stop offset="1" stop-color="{colors["bg1"]}"/>'
        '</linearGradient></defs>',
        f'<rect width="{width}" height="{height}" rx="34" fill="url(#bg)"/>',
        f'<text x="70" y="72" fill="{colors["mint"]}" font-family="Inter,system-ui,sans-serif" font-size="19" font-weight="700" letter-spacing="4">RECURSIVE ORCHESTRATION IMPROVER · FINAL RECORD</text>',
        f'<text x="70" y="132" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="45" font-weight="750">Orchestration worked. Recursive improvement did not.</text>',
        f'<text x="70" y="172" fill="{colors["muted"]}" font-family="Inter,system-ui,sans-serif" font-size="21">30 development rounds · 396 sealed cases · 7,535 Luna Light calls · one fixed transfer screen</text>',
    ]

    # Matched development evidence.
    svg.extend(
        [
            f'<rect x="55" y="220" width="760" height="430" rx="26" fill="{colors["panel"]}" stroke="{colors["border"]}"/>',
            f'<text x="90" y="270" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="27" font-weight="700">A real orchestration effect</text>',
            f'<text x="90" y="304" fill="{colors["muted"]}" font-family="Inter,system-ui,sans-serif" font-size="17">Exact accuracy on the same 354 Luna Light cases</text>',
        ]
    )
    bar_rows = [
        ("Direct", direct["challenger_accuracy"], colors["muted"]),
        ("Five-agent plurality", plurality["challenger_accuracy"], colors["violet"]),
        ("Five agents + three repairers", plurality["incumbent_accuracy"], colors["mint"]),
    ]
    for index, (label, value, color) in enumerate(bar_rows):
        y = 360 + index * 92
        bar_width = 500 * value / 0.5
        svg.extend(
            [
                f'<text x="90" y="{y}" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="19">{esc(label)}</text>',
                f'<rect x="90" y="{y + 18}" width="500" height="24" rx="12" fill="{colors["grid"]}"/>',
                f'<rect x="90" y="{y + 18}" width="{bar_width:.1f}" height="24" rx="12" fill="{color}"/>',
                f'<text x="610" y="{y + 39}" fill="{color}" font-family="ui-monospace,monospace" font-size="22" font-weight="700">{pct(value)}</text>',
            ]
        )
    svg.extend(
        [
            f'<text x="90" y="622" fill="{colors["mint"]}" font-family="Inter,system-ui,sans-serif" font-size="19" font-weight="700">+28.8 points over plurality</text>',
            f'<text x="390" y="622" fill="{colors["muted"]}" font-family="Inter,system-ui,sans-serif" font-size="17">{incumbent["helpful_interventions"]} helpful · {incumbent["harmful_interventions"]} harmful reversals</text>',
        ]
    )

    # Temporal plateau.
    svg.extend(
        [
            f'<rect x="845" y="220" width="700" height="430" rx="26" fill="{colors["panel"]}" stroke="{colors["border"]}"/>',
            f'<text x="880" y="270" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="27" font-weight="700">The fixed swarm plateaued</text>',
            f'<text x="880" y="304" fill="{colors["muted"]}" font-family="Inter,system-ui,sans-serif" font-size="17">Same five-agent + three-repair mechanism over time</text>',
        ]
    )
    chart_x = [950, 1175, 1400]
    points = [(x, 565 - value * 470) for x, (_, value, _, _) in zip(chart_x, phase_values)]
    svg.append(
        f'<polyline points="{" ".join(f"{x},{y:.1f}" for x, y in points)}" fill="none" stroke="{colors["amber"]}" stroke-width="4"/>'
    )
    for (x, y), (label, value, correct, total) in zip(points, phase_values):
        svg.extend(
            [
                f'<line x1="{x}" y1="340" x2="{x}" y2="565" stroke="{colors["grid"]}"/>',
                f'<circle cx="{x}" cy="{y:.1f}" r="11" fill="{colors["amber"]}" stroke="{colors["white"]}" stroke-width="2"/>',
                f'<text x="{x}" y="{y - 22:.1f}" text-anchor="middle" fill="{colors["amber"]}" font-family="ui-monospace,monospace" font-size="22" font-weight="700">{pct(value)}</text>',
                f'<text x="{x}" y="596" text-anchor="middle" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="16">{esc(label)}</text>',
                f'<text x="{x}" y="620" text-anchor="middle" fill="{colors["muted"]}" font-family="ui-monospace,monospace" font-size="13">{correct}/{total}</text>',
            ]
        )

    # Cross-model implication.
    svg.extend(
        [
            f'<rect x="55" y="680" width="1490" height="245" rx="26" fill="#0d2030" stroke="#286775"/>',
            f'<text x="90" y="730" fill="{colors["cyan"]}" font-family="Inter,system-ui,sans-serif" font-size="24" font-weight="700">Depth must be calibrated to the model</text>',
            f'<text x="90" y="760" fill="{colors["muted"]}" font-family="Inter,system-ui,sans-serif" font-size="16">Best observed organization on the 12-case transfer screen</text>',
        ]
    )
    card_x = [90, 495, 900]
    card_colors = [colors["cyan"], colors["amber"], colors["violet"]]
    for x, color, (model, method, accuracy) in zip(card_x, card_colors, transfer_summary):
        svg.extend(
            [
                f'<text x="{x}" y="812" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="19" font-weight="700">{esc(model)}</text>',
                f'<text x="{x}" y="850" fill="{color}" font-family="ui-monospace,monospace" font-size="28" font-weight="700">{pct(accuracy)}</text>',
                f'<text x="{x + 105}" y="850" fill="{colors["muted"]}" font-family="Inter,system-ui,sans-serif" font-size="16">{esc(method)}</text>',
            ]
        )
    svg.extend(
        [
            f'<line x1="1290" y1="790" x2="1290" y2="875" stroke="{colors["border"]}"/>',
            f'<text x="1325" y="810" fill="{colors["mint"]}" font-family="Inter,system-ui,sans-serif" font-size="16" font-weight="700">PRACTICAL RULE</text>',
            f'<text x="1325" y="842" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="16">Start with five answers.</text>',
            f'<text x="1325" y="868" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="16">Add one repairer.</text>',
            f'<text x="1325" y="894" fill="{colors["white"]}" font-family="Inter,system-ui,sans-serif" font-size="16">Escalate only after calibration.</text>',
            f'<text x="70" y="965" fill="{colors["muted"]}" font-family="Inter,system-ui,sans-serif" font-size="15">Development evidence only. The transfer result is 11/12 with wide uncertainty and does not establish unrelated-domain generality.</text>',
            '</svg>',
        ]
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text("".join(svg), encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
