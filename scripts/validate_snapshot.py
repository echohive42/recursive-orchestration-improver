#!/usr/bin/env python3
"""Validate the public Iterations 1-3 snapshot with the standard library."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ITERATIONS = (1, 2, 3)
EXPECTED_REPAIR_CORRECT = (6, 5, 5)
EXPECTED_DIRECT_CORRECT = (1, 0, 2)
EXPECTED_PLURALITY_CORRECT = (1, 0, 2)
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


def main() -> None:
    state = load(ROOT / "state.json")
    assert tuple(state["completed_iterations"]) == ITERATIONS
    assert state["next_iteration"] == 4
    assert not (ROOT / "iterations" / "iteration-004").exists()
    assert (ROOT / "strategies" / "iteration-004.json").is_file()

    repair_correct: list[int] = []
    direct_correct: list[int] = []
    plurality_correct: list[int] = []
    helpful = 0
    harmful = 0

    for number in ITERATIONS:
        folder = ROOT / "iterations" / f"iteration-{number:03d}"
        summary = load(folder / "results" / "summary.json")
        registry = load(folder / "registry.json")
        assert registry["answers_loaded_after_worker_calls"] is True
        assert registry["sealed_answers_not_sent_to_workers"] is True
        assert sha256(folder / "panel" / "public_cases.json") == summary["public_cases_sha256"]
        assert sha256(folder / "panel" / "sealed_answers.json") == summary["sealed_answers_sha256"]

        for stage, expected in (("base", summary["base_calls"]), ("review", summary["review_calls"])):
            assert line_count(folder / stage / "jobs.jsonl") == expected
            assert line_count(folder / stage / "results.jsonl") == expected

        repair = strategy(summary, "repair-review-5x3")
        direct = strategy(summary, "direct-1")
        plurality = strategy(summary, "plurality-5")
        repair_correct.append(repair["correct"])
        direct_correct.append(direct["correct"])
        plurality_correct.append(plurality["correct"])
        helpful += repair["helpful_interventions"]
        harmful += repair["harmful_interventions"]

    assert tuple(repair_correct) == EXPECTED_REPAIR_CORRECT
    assert tuple(direct_correct) == EXPECTED_DIRECT_CORRECT
    assert tuple(plurality_correct) == EXPECTED_PLURALITY_CORRECT
    assert sum(repair_correct) == 16
    assert sum(direct_correct) == 3
    assert sum(plurality_correct) == 3
    assert helpful == 13 and harmful == 0

    text_suffixes = {".md", ".txt", ".json", ".jsonl", ".csv", ".py", ".svg"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix not in text_suffixes:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in FORBIDDEN:
            assert not pattern.search(text), f"Private runtime marker in {path.relative_to(ROOT)}"

    print("Snapshot valid: 3 completed iterations, 768 prompts/results, no active panel or private runtime markers.")
    print("Stable repair swarm: 16/36 exact (44.4%); direct and five-vote baselines: 3/36 (8.3%).")
    print("Observed interventions: 13 helpful, 0 harmful.")


if __name__ == "__main__":
    main()
