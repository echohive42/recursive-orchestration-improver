#!/usr/bin/env python3
"""Export completed iterations from the private live lab into this public snapshot.

The script copies frozen prompts, compact terminal results, scores, and provenance.
It deliberately excludes raw Codex event streams, shell logs, command files, local
paths, internal thread identifiers, and any active partial iteration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


PUBLIC_ROOT = Path(__file__).resolve().parent.parent


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def copy_file(source: Path, target: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix == ".csv":
        target.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    else:
        shutil.copy2(source, target)


def copy_if_present(source: Path, target: Path) -> None:
    if source.is_file():
        copy_file(source, target)


def compact_results(stage: Path, output: Path) -> int:
    records: list[dict[str, Any]] = []
    for path in sorted((stage / "jobs").glob("*/result.json")):
        result = load_json(path)
        records.append(
            {
                key: result[key]
                for key in (
                    "job_id",
                    "outcome",
                    "document",
                    "attempt_count",
                    "terminal_attempt",
                    "duration_ms",
                    "response_sha256",
                    "usage",
                )
                if key in result
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    return len(records)


def export_cross_review(source_iteration: Path, public_iteration: Path) -> int:
    """Export an optional layered cross-examination stage and its compact results."""
    source = source_iteration / "cross-review"
    if not source.is_dir():
        return 0

    public = public_iteration / "cross-review"
    for name in ("manifest.json", "plan.jsonl"):
        copy_if_present(source / name, public / name)

    total = 0
    for layer in sorted(path for path in source.glob("layer-*") if path.is_dir()):
        target = public / layer.name
        for name in ("jobs.jsonl", "manifest.json", "progress.json"):
            copy_if_present(layer / name, target / name)
        total += compact_results(layer, target / "results.jsonl")
    return total


def export_regenerate_integrator(source_iteration: Path, public_iteration: Path) -> int:
    """Export an optional terminal integration stage and its compact results."""
    source = source_iteration / "regenerate-integrator"
    if not source.is_dir():
        return 0

    public = public_iteration / "regenerate-integrator"
    for name in (
        "jobs.jsonl",
        "manifest.json",
        "plan.json",
        "plan_manifest.json",
        "progress.json",
    ):
        copy_if_present(source / name, public / name)
    return compact_results(source, public / "results.jsonl")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export(source_root: Path, through: int | None = None) -> None:
    source_root = source_root.resolve()
    if source_root == PUBLIC_ROOT.resolve():
        raise ValueError("Source and public snapshot must be different directories")

    state = load_json(source_root / "state.json")
    completed = [int(number) for number in state.get("completed_iterations", [])]
    if through is not None:
        completed = [number for number in completed if number <= through]
    if not completed:
        raise ValueError("No completed iterations found")

    for name in ("run.py", "researcher.md"):
        copy_file(source_root / name, PUBLIC_ROOT / name)

    public_protocol = load_json(source_root / "protocol.json")
    generator_source = (source_root / public_protocol["panel_generation"]["source"]).resolve()
    public_protocol["panel_generation"]["source"] = "scripts/generate_extreme_calibration.py"
    write_json(PUBLIC_ROOT / "protocol.json", public_protocol)
    copy_file(generator_source, PUBLIC_ROOT / "scripts" / "generate_extreme_calibration.py")

    public_state = dict(state)
    public_state["status"] = "public-snapshot"
    public_state["snapshot_scope"] = f"completed iterations {min(completed)}-{max(completed)}"
    public_state["completed_iterations"] = completed
    public_state["next_iteration"] = max(completed) + 1
    public_state["current_strategy_file"] = f"strategies/iteration-{max(completed) + 1:03d}.json"
    write_json(PUBLIC_ROOT / "state.json", public_state)

    next_iteration = max(completed) + 1
    for number in range(1, next_iteration + 1):
        copy_if_present(
            source_root / "strategies" / f"iteration-{number:03d}.json",
            PUBLIC_ROOT / "strategies" / f"iteration-{number:03d}.json",
        )

    copied_results: dict[str, dict[str, int]] = {}
    shallow_files = (
        "protocol_snapshot.json",
        "protocol_snapshot_manifest.json",
        "registry.json",
        "panel/panel_hashes.json",
        "panel/public_cases.json",
        "panel/sealed_answers.json",
        "base/jobs.jsonl",
        "base/manifest.json",
        "base/progress.json",
        "review/jobs.jsonl",
        "review/manifest.json",
        "review/progress.json",
        "director/prompt.txt",
        "director/request.json",
        "director/result_summary.json",
        "results/case_results.csv",
        "results/case_results.json",
        "results/plot.svg",
        "results/summary.json",
    )
    for number in completed:
        name = f"iteration-{number:03d}"
        source_iteration = source_root / "iterations" / name
        if not (source_iteration / "results" / "summary.json").is_file():
            raise ValueError(f"Iteration {number} is marked complete but has no summary")
        public_iteration = PUBLIC_ROOT / "iterations" / name
        for relative in shallow_files:
            copy_if_present(source_iteration / relative, public_iteration / relative)
        copied_results[name] = {
            "base": compact_results(source_iteration / "base", public_iteration / "base" / "results.jsonl"),
            "review": compact_results(
                source_iteration / "review", public_iteration / "review" / "results.jsonl"
            ),
            "cross_review": export_cross_review(source_iteration, public_iteration),
            "regenerate_integrator": export_regenerate_integrator(
                source_iteration, public_iteration
            ),
        }

    manifest_files = sorted(
        path
        for path in PUBLIC_ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.name not in {".DS_Store", "snapshot_manifest.json"}
    )
    write_json(
        PUBLIC_ROOT / "snapshot_manifest.json",
        {
            "completed_iterations": completed,
            "compact_terminal_results": copied_results,
            "files": {
                str(path.relative_to(PUBLIC_ROOT)): sha256(path)
                for path in manifest_files
            },
            "note": "Generated from completed stages only; raw runtime artifacts and active partial work are excluded.",
        },
    )
    print(f"Exported {len(completed)} completed iterations to {PUBLIC_ROOT}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        default=PUBLIC_ROOT.parent / "orchestration-autoresearch-lab",
        help="Path to the private live lab",
    )
    parser.add_argument(
        "--through",
        type=int,
        help="Export only completed iterations up to this number",
    )
    args = parser.parse_args()
    export(args.source, args.through)


if __name__ == "__main__":
    main()
