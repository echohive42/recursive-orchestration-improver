#!/usr/bin/env python3
"""Publish newly completed live-lab iterations to the public repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from export_completed import export


ROOT = Path(__file__).resolve().parent.parent
LIVE_START = "<!-- LIVE_PROGRESS_START -->"
LIVE_END = "<!-- LIVE_PROGRESS_END -->"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def run(*command: str, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def percent(value: float) -> str:
    return f"{100 * value:.1f}%"


def load_rounds(completed: list[int]) -> list[dict]:
    return [
        load(ROOT / "iterations" / f"iteration-{number:03d}" / "results" / "summary.json")
        for number in completed
    ]


def named_strategy(summary: dict, strategy_id: str) -> dict:
    return next(item for item in summary["strategies"] if item["strategy_id"] == strategy_id)


def render_svg(rounds: list[dict]) -> str:
    width, height = 1200, 630
    left, right, top, bottom = 115, 70, 180, 130
    plot_w, plot_h = width - left - right, height - top - bottom
    count = len(rounds)

    def x(index: int) -> float:
        return left + (plot_w / max(1, count - 1)) * index

    def y(value: float) -> float:
        return top + plot_h * (1 - value)

    winner_values = [item["winner"]["accuracy"] for item in rounds]
    direct_values = [named_strategy(item, "direct-1")["accuracy"] for item in rounds]
    winner_points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(winner_values))
    direct_points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(direct_values))
    latest = rounds[-1]["winner"]

    grid = []
    for step in range(0, 101, 20):
        yy = y(step / 100)
        grid.append(
            f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" class="grid"/>'
            f'<text x="{left-22}" y="{yy+6:.1f}" text-anchor="end" class="axis">{step}%</text>'
        )

    labels = []
    dots = []
    for index, summary in enumerate(rounds):
        xx = x(index)
        labels.append(f'<text x="{xx:.1f}" y="{top+plot_h+40}" text-anchor="middle" class="axis strong">R{summary["iteration"]}</text>')
        for value, css in ((winner_values[index], "winner"), (direct_values[index], "direct")):
            dots.append(f'<circle cx="{xx:.1f}" cy="{y(value):.1f}" r="7" class="dot {css}"/>')
            dots.append(
                f'<text x="{xx:.1f}" y="{y(value)-15:.1f}" text-anchor="middle" class="value {css}">{percent(value)}</text>'
            )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">Recursive Orchestration Improver progress</title>
<desc id="desc">Best registered orchestration and direct baseline accuracy across {count} fresh sealed rounds.</desc>
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#07111f"/><stop offset="1" stop-color="#140b2d"/></linearGradient>
  <filter id="glow"><feGaussianBlur stdDeviation="5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<style>
  .title{{font:700 38px ui-sans-serif,system-ui;fill:#f8fafc}} .subtitle{{font:500 20px ui-sans-serif,system-ui;fill:#a5b4c8}}
  .grid{{stroke:#27344a;stroke-width:1}} .axis{{font:500 16px ui-monospace,SFMono-Regular,monospace;fill:#8291a8}} .strong{{fill:#dbeafe;font-weight:700}}
  .line{{fill:none;stroke-width:5;stroke-linejoin:round;stroke-linecap:round}} .line.winner{{stroke:#31d7f4;filter:url(#glow)}} .line.direct{{stroke:#f5a4ff;stroke-dasharray:10 10}}
  .dot{{stroke:#07111f;stroke-width:4}} .dot.winner{{fill:#31d7f4}} .dot.direct{{fill:#f5a4ff}}
  .value{{font:700 15px ui-monospace,SFMono-Regular,monospace}} .value.winner{{fill:#8cecff}} .value.direct{{fill:#ffc4ff}}
  .legend{{font:600 17px ui-sans-serif,system-ui;fill:#dce8f7}} .note{{font:500 15px ui-sans-serif,system-ui;fill:#8fa0b8}}
</style>
<rect width="{width}" height="{height}" rx="28" fill="url(#bg)"/>
<text x="70" y="68" class="title">Recursive Orchestration Improver</text>
<text x="70" y="104" class="subtitle">Can AI agents improve how they work together?</text>
<line x1="760" y1="70" x2="810" y2="70" class="line winner"/><text x="826" y="76" class="legend">Best registered system</text>
<line x1="760" y1="105" x2="810" y2="105" class="line direct"/><text x="826" y="111" class="legend">One Luna Light call</text>
{''.join(grid)}
<polyline points="{winner_points}" class="line winner"/>
<polyline points="{direct_points}" class="line direct"/>
{''.join(dots)}
{''.join(labels)}
<text x="70" y="570" class="note">Each round uses a fresh sealed panel. The line shows observed round outcomes, not training accuracy.</text>
<text x="70" y="598" class="note">Latest: R{rounds[-1]['iteration']} · Best orchestration · {latest['correct']}/{latest['total']} exact</text>
</svg>
'''


def progress_table(rounds: list[dict]) -> str:
    rows = []
    for summary in rounds:
        winner = summary["winner"]
        direct = named_strategy(summary, "direct-1")
        rows.append(
            f"| {summary['iteration']} | {winner['name']} | "
            f"{winner['correct']}/{winner['total']} · **{percent(winner['accuracy'])}** | "
            f"{percent(winner['worst_family_accuracy'])} | "
            f"{direct['correct']}/{direct['total']} · {percent(direct['accuracy'])} | "
            f"{summary['worker_calls']} |"
        )
    return "\n".join(rows)


def render_progress(rounds: list[dict]) -> None:
    latest_summary = rounds[-1]
    latest = latest_summary["winner"]
    completed = [item["iteration"] for item in rounds]
    table = progress_table(rounds)

    block = f'''{LIVE_START}
## Live research progress

**{len(rounds)} completed rounds.** The latest winner was **{latest['name']}**, which solved **{latest['correct']}/{latest['total']} ({percent(latest['accuracy'])})** with a weakest-family accuracy of **{percent(latest['worst_family_accuracy'])}**.

| Round | Best registered system | Exact accuracy | Weakest family | Direct baseline | Worker calls |
|---:|---|---:|---:|---:|---:|
{table}

![Research progress across completed rounds](images/progress.svg)

[Read the round-by-round progress notes](PROGRESS.md)

> [!NOTE]
> Every point uses a different fresh sealed panel. This is an honest sequence of research outcomes, not a conventional training curve. Final performance still requires a frozen system and untouched validation.
{LIVE_END}'''

    readme_path = ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    if LIVE_START in readme and LIVE_END in readme:
        before, remainder = readme.split(LIVE_START, 1)
        _, after = remainder.split(LIVE_END, 1)
        readme = before + block + after
    else:
        marker = "## Initial repair signal (Rounds 1-3)"
        if marker not in readme:
            raise ValueError(f"README insertion marker missing: {marker}")
        readme = readme.replace(marker, block + "\n\n" + marker, 1)

    readme = rephrase_snapshot_count(readme, len(rounds), max(completed) + 1)
    write(readme_path, readme)

    details = [
        "# Research progress\n",
        "This page is regenerated only after a round is fully sealed, scored, and followed by a registered next strategy. Active partial work is never published.\n",
        "| Round | Best registered system | Exact accuracy | Weakest family | Direct baseline | Worker calls |",
        "|---:|---|---:|---:|---:|---:|",
        table,
        "\n## Round notes\n",
    ]
    for summary in rounds:
        winner = summary["winner"]
        families = ", ".join(
            f"{name} {percent(result['accuracy'])}" for name, result in sorted(winner["families"].items())
        )
        details.append(
            f"### Round {summary['iteration']}\n\n"
            f"**{winner['name']}** won at {winner['correct']}/{winner['total']} ({percent(winner['accuracy'])}). "
            f"Family results: {families}. Helpful interventions: {winner.get('helpful_interventions', 0)}. "
            f"Harmful interventions: {winner.get('harmful_interventions', 0)}.\n"
        )
    details.append(
        "## Interpretation\n\n"
        "The chart compares each round's best registered orchestration with a one-call Luna Light baseline. "
        "Because the panel changes every round, short-term rises and falls combine strategy differences with case difficulty. "
        "The useful signal is repeated performance across fresh panels and problem families, not a single peak.\n"
    )
    write(ROOT / "PROGRESS.md", "\n".join(details))
    write(ROOT / "images" / "progress.svg", render_svg(rounds))


def rephrase_snapshot_count(readme: str, rounds: int, next_iteration: int) -> str:
    import re

    replacement = (
        f"The repository currently contains {rounds} completed research rounds and the registered strategy "
        f"for Round {next_iteration}. The numbers below are promising development evidence, not independent final validation."
    )
    return re.sub(
        r"The repository currently contains .*? The numbers below are promising development evidence, not independent final validation\.",
        replacement,
        readme,
        count=1,
    )


def refresh_manifest(completed: list[int]) -> None:
    manifest_path = ROOT / "snapshot_manifest.json"
    old = load(manifest_path)
    files = sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.name not in {".DS_Store", "snapshot_manifest.json"}
    )

    def digest(path: Path) -> str:
        value = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                value.update(chunk)
        return value.hexdigest()

    old["completed_iterations"] = completed
    old["files"] = {str(path.relative_to(ROOT)): digest(path) for path in files}
    old["note"] = "Generated from completed stages only; raw runtime artifacts and active partial work are excluded."
    write(manifest_path, json.dumps(old, indent=2, sort_keys=True) + "\n")


def source_is_ready(source: Path, latest: int) -> bool:
    state = load(source / "state.json")
    next_iteration = latest + 1
    required = (
        source / "iterations" / f"iteration-{latest:03d}" / "results" / "summary.json",
        source / "iterations" / f"iteration-{latest:03d}" / "director" / "result_summary.json",
        source / "strategies" / f"iteration-{next_iteration:03d}.json",
    )
    return (
        state.get("next_iteration") == next_iteration
        and state.get("current_strategy_file") == f"strategies/iteration-{next_iteration:03d}.json"
        and all(path.is_file() for path in required)
    )


def publish(source: Path, push: bool) -> bool:
    source = source.resolve()
    live_state = load(source / "state.json")
    live_completed = [int(number) for number in live_state.get("completed_iterations", [])]
    public_completed = [int(number) for number in load(ROOT / "state.json").get("completed_iterations", [])]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    if not live_completed or max(live_completed) <= max(public_completed, default=0):
        print(f"[{stamp}] No newly completed round. Public={public_completed}; live={live_completed}.", flush=True)
        return False

    latest = max(live_completed)
    if not source_is_ready(source, latest):
        print(f"[{stamp}] Round {latest} is marked complete but its director handoff is not ready.", flush=True)
        return False

    if push and run("git", "status", "--porcelain", capture=True):
        raise RuntimeError("Public repository is not clean; refusing automatic publication")

    export(source, latest)
    completed = [int(number) for number in load(ROOT / "state.json")["completed_iterations"]]
    render_progress(load_rounds(completed))
    refresh_manifest(completed)
    run(sys.executable, str(ROOT / "scripts" / "validate_snapshot.py"))
    run("git", "diff", "--check")

    if push:
        run("git", "add", "-A")
        run("git", "commit", "-m", f"Publish completed iteration {latest}")
        run("git", "push", "origin", "main")
    print(f"[{stamp}] Published completed iteration {latest}.", flush=True)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT.parent / "orchestration-autoresearch-lab",
        help="Path to the read-only live experiment",
    )
    parser.add_argument("--push", action="store_true", help="Commit and push a clean automatic update")
    args = parser.parse_args()
    publish(args.source, args.push)


if __name__ == "__main__":
    main()
