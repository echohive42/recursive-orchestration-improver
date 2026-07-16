#!/usr/bin/env python3
"""Validate every completed iteration in the public research snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = (
    re.compile("/" + "Users/"),
    re.compile("/private/" + "var/|/var/" + "folders/"),
    re.compile('"thread' + '_id"|"thread' + 'Id"'),
    re.compile(r"\b(?:gh[opsu]_" + r"|s" + r"k-)[A-Za-z0-9_-]{12,}"),
    re.compile(r"\b019" + r"f[0-9a-f-]{20,}\b"),
)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def line_count(path: Path) -> int:
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strategy(summary: dict, strategy_id: str) -> dict:
    return next(item for item in summary["strategies"] if item["strategy_id"] == strategy_id)


def snapshot_files() -> set[str]:
    return {
        str(path.relative_to(ROOT))
        for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
        and path.name not in {".DS_Store", "snapshot_manifest.json"}
    }


def main() -> None:
    state = load(ROOT / "state.json")
    completed = tuple(int(number) for number in state["completed_iterations"])
    assert completed, "Snapshot contains no completed iterations"
    assert completed == tuple(range(1, max(completed) + 1)), "Completed iterations must be contiguous"

    next_iteration = max(completed) + 1
    assert state["next_iteration"] == next_iteration
    assert state["current_strategy_file"] == f"strategies/iteration-{next_iteration:03d}.json"
    assert not (ROOT / "iterations" / f"iteration-{next_iteration:03d}").exists()
    assert (ROOT / "strategies" / f"iteration-{next_iteration:03d}.json").is_file()

    total_cases = 0
    total_prompts = 0
    latest_winner = None

    for number in completed:
        folder = ROOT / "iterations" / f"iteration-{number:03d}"
        summary = load(folder / "results" / "summary.json")
        registry = load(folder / "registry.json")

        assert summary["iteration"] == number
        assert registry["iteration"] == number
        assert registry["registered_before_calls"] is True
        assert registry["answers_loaded_after_worker_calls"] is True
        assert registry["sealed_answers_not_sent_to_workers"] is True
        assert summary["cases"] == registry["cases"]
        assert sha256(folder / "panel" / "public_cases.json") == summary["public_cases_sha256"]
        assert sha256(folder / "panel" / "sealed_answers.json") == summary["sealed_answers_sha256"]

        for stage, expected in (("base", summary["base_calls"]), ("review", summary["review_calls"])):
            assert line_count(folder / stage / "jobs.jsonl") == expected
            assert line_count(folder / stage / "results.jsonl") == expected

        direct = strategy(summary, "direct-1")
        plurality = strategy(summary, "plurality-5")
        winner = summary["winner"]
        assert any(item["strategy_id"] == winner["strategy_id"] for item in summary["strategies"])
        assert winner["total"] == summary["cases"]
        assert abs(winner["accuracy"] - winner["correct"] / winner["total"]) < 1e-12
        assert direct["total"] == summary["cases"]
        assert plurality["total"] == summary["cases"]
        assert sum(item["total"] for item in winner["families"].values()) == summary["cases"]

        total_cases += summary["cases"]
        total_prompts += summary["worker_calls"]
        latest_winner = winner

    manifest = load(ROOT / "snapshot_manifest.json")
    assert tuple(manifest["completed_iterations"]) == completed
    listed = set(manifest["files"])
    actual = snapshot_files()
    assert listed == actual, f"Manifest mismatch: missing={sorted(actual-listed)}, stale={sorted(listed-actual)}"
    for relative, expected in manifest["files"].items():
        assert sha256(ROOT / relative) == expected, f"Hash mismatch: {relative}"

    text_suffixes = {".md", ".txt", ".json", ".jsonl", ".csv", ".py", ".svg"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix not in text_suffixes:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in FORBIDDEN:
            assert not pattern.search(text), f"Private runtime marker in {path.relative_to(ROOT)}"

    assert latest_winner is not None
    print(
        f"Snapshot valid: {len(completed)} completed iterations, {total_cases} sealed cases, "
        f"{total_prompts} prompts/results, and no active panel or private runtime markers."
    )
    print(
        f"Latest round winner: {latest_winner['name']} at "
        f"{latest_winner['correct']}/{latest_winner['total']} ({latest_winner['accuracy']:.1%})."
    )


if __name__ == "__main__":
    main()
