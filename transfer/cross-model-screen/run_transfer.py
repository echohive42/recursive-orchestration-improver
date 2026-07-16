#!/usr/bin/env python3
"""Run a compact Terra Low and Luna Medium orchestration transfer screen."""

from __future__ import annotations

import argparse
import csv
import json
import random
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
LAB = ROOT.parents[1]
sys.path.insert(0, str(LAB))
import run as core  # noqa: E402


PROTOCOL = ROOT / "protocol.json"
PANEL = ROOT / "panel"
RESULTS = ROOT / "results"
CALLS = ROOT / "calls"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def freeze(path: Path, value: Any) -> None:
    core.freeze_json(path, value)


def config() -> dict[str, Any]:
    return load(PROTOCOL)


def public_cases() -> list[dict[str, Any]]:
    return load(PANEL / "public_cases.json")["cases"]


def strategies() -> list[dict[str, Any]]:
    return config()["strategies"]


def conditions() -> list[dict[str, Any]]:
    return config()["conditions"]


def stage_registry(stage: Path, jobs: list[dict[str, Any]], extra: dict[str, Any]) -> None:
    rendered = "".join(core.canonical(job) + "\n" for job in jobs)
    registry = stage / "jobs.jsonl"
    if registry.exists() and registry.read_text(encoding="utf-8") != rendered:
        raise core.LabError(f"Frozen job registry changed: {registry}")
    if not registry.exists():
        core.atomic_text(registry, rendered)
    freeze(
        stage / "manifest.json",
        {
            "registered_before_calls": True,
            "registered_at": core.registered_at(stage / "manifest.json"),
            "jobs": len(jobs),
            "jobs_sha256": core.sha256_file(registry),
            "accuracy_withheld": True,
            **extra,
        },
    )


def assert_terminal(stage: str, jobs: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> None:
    blocked = [
        job["job_id"]
        for job in jobs
        if results[job["job_id"]].get("outcome") in {"infrastructure_exhausted", "configuration_failure"}
    ]
    if blocked:
        freeze(
            ROOT / "infrastructure_hold.json",
            {
                "stage": stage,
                "blocked_jobs": blocked,
                "count": len(blocked),
                "note": "Not scored. Resume only after resolving pre-inference infrastructure failures.",
            },
        )
        raise core.LabError(f"{stage} has {len(blocked)} pre-inference failures; scoring remains sealed")


def selected_case(selection: dict[str, Any]) -> dict[str, Any]:
    number = int(selection["iteration"])
    source = LAB / "iterations" / f"iteration-{number:03d}" / "panel" / "public_cases.json"
    cases = {item["case_id"]: item for item in load(source)["cases"]}
    item = dict(cases[selection["case_id"]])
    item["source_iteration"] = number
    item["source_case_id"] = selection["case_id"]
    item["case_id"] = f"i{number:03d}-{selection['case_id']}"
    return item


def source_strategy_id(case: dict[str, Any], strategy: dict[str, Any]) -> str:
    number = int(case["source_iteration"])
    summary = load(LAB / "iterations" / f"iteration-{number:03d}" / "results" / "summary.json")
    target = core.operational_signature(strategy)
    matches = [
        item["strategy_id"]
        for item in summary["strategies"]
        if core.operational_signature(item) == target
    ]
    if len(matches) != 1:
        raise core.LabError(
            f"Expected one historical strategy match in Iteration {number}; found {len(matches)}"
        )
    return matches[0]


def source_parallel_seed(
    case: dict[str, Any], strategy: dict[str, Any], reviewer_index: int
) -> int:
    number = int(case["source_iteration"])
    strategy_id = source_strategy_id(case, strategy)
    path = LAB / "iterations" / f"iteration-{number:03d}" / "review" / "jobs.jsonl"
    matches = [
        item
        for item in core.read_stage_jobs(path)
        if item["case_id"] == case["source_case_id"]
        and item["strategy_id"] == strategy_id
        and int(item["reviewer_index"]) == reviewer_index
    ]
    if len(matches) != 1:
        raise core.LabError(
            f"Expected one source review seed for {case['case_id']} {strategy_id} r{reviewer_index}; "
            f"found {len(matches)}"
        )
    return int(matches[0]["candidate_order_seed"])


def source_cross_seed(case: dict[str, Any], strategy: dict[str, Any]) -> int:
    number = int(case["source_iteration"])
    strategy_id = source_strategy_id(case, strategy)
    path = LAB / "iterations" / f"iteration-{number:03d}" / "cross-review" / "plan.jsonl"
    matches = [
        item
        for item in core.read_stage_jobs(path)
        if item["case_id"] == case["source_case_id"] and item["strategy_id"] == strategy_id
    ]
    if len(matches) != 1:
        raise core.LabError(
            f"Expected one source cross-exam seed for {case['case_id']} {strategy_id}; "
            f"found {len(matches)}"
        )
    return int(matches[0]["candidate_order_seed"])


def planned_maximum_calls(cfg: dict[str, Any]) -> int:
    condition_count = len(cfg["conditions"])
    case_count = len(cfg["case_selection"])
    base_count = max(int(item["base_count"]) for item in cfg["strategies"])
    review_count = sum(
        int(item["review_count"])
        for item in cfg["strategies"]
        if item["review_mode"] in {"repair", "cross_examine"}
    )
    return condition_count * case_count * (base_count + review_count)


def verify_registered_runtime() -> None:
    registry = load(ROOT / "registry.json")
    checks = {
        "protocol_sha256": PROTOCOL,
        "public_cases_sha256": PANEL / "public_cases.json",
        "runner_sha256": Path(__file__),
        "core_runner_sha256": LAB / "run.py",
    }
    for key, path in checks.items():
        observed = core.sha256_file(path)
        if observed != registry[key]:
            raise core.LabError(f"Registered runtime drift for {key}; refusing to run or score")


def model_telemetry(
    jobs: list[dict[str, Any]], results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    observed: dict[str, set[str]] = defaultdict(set)
    mismatches = []
    for job in jobs:
        reported = results[job["job_id"]].get("reported_model")
        if not reported:
            continue
        reported_text = str(reported)
        observed[job["condition_id"]].add(reported_text)
        if reported_text != job["model"]:
            mismatches.append(
                {
                    "job_id": job["job_id"],
                    "requested": job["model"],
                    "reported": reported_text,
                }
            )
    if mismatches:
        freeze(ROOT / "model_identity_hold.json", {"mismatches": mismatches})
        raise core.LabError("Reported model identity differs from frozen requested configuration")
    return {
        "requested_cli_configuration": conditions(),
        "reported_models_by_condition": {
            condition["condition_id"]: sorted(observed.get(condition["condition_id"], set()))
            for condition in conditions()
        },
        "identity_boundary": (
            "Any available reported-model telemetry matched the requested alias. Empty lists mean "
            "the served model was not independently attested by the CLI output."
        ),
    }


def verify_answer_blind_selection(cfg: dict[str, Any]) -> None:
    rule = cfg["selection_rule"]
    salt = rule["salt_sha256"]
    selected = {(int(item["iteration"]), item["case_id"]): item for item in cfg["case_selection"]}
    expected_keys: set[tuple[int, str]] = set()
    for number_text, families in rule["family_pairs_by_iteration"].items():
        number = int(number_text)
        source = LAB / "iterations" / f"iteration-{number:03d}" / "panel" / "public_cases.json"
        cases = load(source)["cases"]
        for family in families:
            ranked = []
            for case in cases:
                if case["family"] != family:
                    continue
                prompt_hash = core.sha256_bytes(case["prompt"].encode("utf-8"))
                payload = f"{salt}\n{number}\n{case['case_id']}\n{prompt_hash}"
                ranked.append((core.sha256_bytes(payload.encode("utf-8")), case["case_id"]))
            if not ranked:
                raise core.LabError(f"No eligible {family} cases in Iteration {number}")
            rank_hash, case_id = min(ranked)
            key = (number, case_id)
            expected_keys.add(key)
            registered = selected.get(key)
            if registered is None or registered.get("rank_sha256") != rank_hash:
                raise core.LabError(
                    f"Answer-blind selection mismatch for Iteration {number} {family}: "
                    f"expected {case_id} with {rank_hash}"
                )
    if set(selected) != expected_keys:
        raise core.LabError("Registered case selection contains entries outside the frozen hash rule")


def prepare() -> None:
    cfg = config()
    verify_answer_blind_selection(cfg)
    calculated_maximum = planned_maximum_calls(cfg)
    if calculated_maximum != int(cfg["maximum_new_calls"]):
        raise core.LabError(
            f"Maximum-call declaration mismatch: calculated {calculated_maximum}, "
            f"registered {cfg['maximum_new_calls']}"
        )
    cases = [selected_case(item) for item in cfg["case_selection"]]
    families = Counter(item["family"] for item in cases)
    if families != Counter({"sequence": 4, "constraint": 4, "logic": 4}):
        raise core.LabError(f"Selection is not balanced: {dict(families)}")
    if len({item["case_id"] for item in cases}) != len(cases):
        raise core.LabError("Selected transfer case IDs are not unique")

    normalized_strategies: list[dict[str, Any]] = []
    for strategy in cfg["strategies"]:
        normalized, error = core.validate_strategy(strategy)
        if error or normalized is None:
            raise core.LabError(f"Invalid frozen strategy {strategy.get('strategy_id')}: {error}")
        normalized_strategies.append(normalized)
    if normalized_strategies != cfg["strategies"]:
        raise core.LabError("Protocol strategies are not in canonical normalized form")

    required_signatures = {core.operational_signature(item) for item in normalized_strategies}
    for number in sorted({item["source_iteration"] for item in cases}):
        summary = load(LAB / "iterations" / f"iteration-{number:03d}" / "results" / "summary.json")
        available = {core.operational_signature(item) for item in summary["strategies"]}
        missing = required_signatures - available
        if missing:
            raise core.LabError(f"Iteration {number} lacks {len(missing)} frozen mechanisms")

    freeze(
        PANEL / "public_cases.json",
        {
            "experiment_id": cfg["experiment_id"],
            "cases": cases,
            "selection_registered_before_calls": True,
        },
    )
    source_hashes = {}
    for number in sorted({item["source_iteration"] for item in cases}):
        directory = LAB / "iterations" / f"iteration-{number:03d}" / "panel"
        source_registry = load(LAB / "iterations" / f"iteration-{number:03d}" / "registry.json")
        protocol_snapshot = LAB / "iterations" / f"iteration-{number:03d}" / "protocol_snapshot.json"
        if core.sha256_file(protocol_snapshot) != cfg["selection_rule"]["salt_sha256"]:
            raise core.LabError(f"Iteration {number} protocol snapshot does not match selection salt")
        if core.sha256_file(directory / "public_cases.json") != source_registry["public_cases_sha256"]:
            raise core.LabError(f"Iteration {number} public panel changed after registration")
        if core.sha256_file(directory / "sealed_answers.json") != source_registry["sealed_answers_sha256"]:
            raise core.LabError(f"Iteration {number} sealed answers changed after registration")
        source_hashes[str(number)] = {
            "public_cases_sha256": core.sha256_file(directory / "public_cases.json"),
            "sealed_answers_sha256": core.sha256_file(directory / "sealed_answers.json"),
        }
    try:
        codex_version = subprocess.check_output(["codex", "--version"], text=True, timeout=10).strip()
    except (OSError, subprocess.SubprocessError):
        codex_version = "unavailable"
    freeze(
        ROOT / "registry.json",
        {
            "experiment_id": cfg["experiment_id"],
            "registered_before_calls": True,
            "registered_at": core.registered_at(ROOT / "registry.json"),
            "protocol_sha256": core.sha256_file(PROTOCOL),
            "runner_sha256": core.sha256_file(Path(__file__)),
            "core_runner_sha256": core.sha256_file(LAB / "run.py"),
            "public_cases_sha256": core.sha256_file(PANEL / "public_cases.json"),
            "source_panel_hashes": source_hashes,
            "strategy_signatures": sorted(required_signatures),
            "conditions": cfg["conditions"],
            "maximum_new_calls": cfg["maximum_new_calls"],
            "codex_cli_version": codex_version,
            "answers_not_parsed": True,
        },
    )
    print(f"Prepared {len(cases)} balanced cases; maximum {cfg['maximum_new_calls']} new calls.")


def base_jobs() -> tuple[list[dict[str, Any]], dict[str, Callable[[Any], dict[str, Any] | None]]]:
    jobs: list[dict[str, Any]] = []
    normalizers: dict[str, Callable[[Any], dict[str, Any] | None]] = {}
    for condition in conditions():
        for case in public_cases():
            for bank_index in range(1, 6):
                job_id = f"{condition['condition_id']}-base-{case['case_id']}-b{bank_index:02d}"
                jobs.append(
                    {
                        "job_id": job_id,
                        "stage": "base",
                        "condition_id": condition["condition_id"],
                        "case_id": case["case_id"],
                        "family": case["family"],
                        "bank_index": bank_index,
                        "model": condition["model"],
                        "reasoning_effort": condition["reasoning_effort"],
                        "prompt": core.base_prompt(case),
                        "output_schema": core.output_schema(case),
                    }
                )
                normalizers[job_id] = lambda value, case=case: core.normalize_answer(case, value)
    random.Random(560_001).shuffle(jobs)
    return jobs, normalizers


def documents_by_condition(
    jobs: list[dict[str, Any]], results: dict[str, dict[str, Any]]
) -> dict[str, dict[str, list[dict[str, Any] | None]]]:
    grouped: dict[str, dict[str, list[tuple[int, dict[str, Any] | None]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for job in jobs:
        grouped[job["condition_id"]][job["case_id"]].append(
            (job["bank_index"], results[job["job_id"]].get("document"))
        )
    return {
        condition: {
            case_id: [document for _, document in sorted(values)]
            for case_id, values in cases.items()
        }
        for condition, cases in grouped.items()
    }


def parallel_review_jobs(
    base_documents: dict[str, dict[str, list[dict[str, Any] | None]]]
) -> tuple[list[dict[str, Any]], dict[str, Callable[[Any], dict[str, Any] | None]]]:
    jobs: list[dict[str, Any]] = []
    normalizers: dict[str, Callable[[Any], dict[str, Any] | None]] = {}
    cases = public_cases()
    condition_lookup = {item["condition_id"]: item for item in conditions()}
    selected = [item for item in strategies() if item["review_mode"] == "repair"]
    for strategy_index, strategy in enumerate(selected):
        for case_index, case in enumerate(cases):
            for condition_id, condition in condition_lookup.items():
                documents = base_documents[condition_id][case["case_id"]][: strategy["base_count"]]
                _, metrics = core.plurality(documents)
                if not core.review_needed(strategy, metrics, documents):
                    continue
                for reviewer_index in range(1, strategy["review_count"] + 1):
                    seed = source_parallel_seed(case, strategy, reviewer_index)
                    candidates = core.candidates_for(strategy, documents, seed)
                    if not candidates:
                        continue
                    job_id = (
                        f"{condition_id}-review-{strategy['strategy_id']}-{case['case_id']}-r{reviewer_index:02d}"
                    )
                    jobs.append(
                        {
                            "job_id": job_id,
                            "stage": "parallel-review",
                            "condition_id": condition_id,
                            "strategy_id": strategy["strategy_id"],
                            "case_id": case["case_id"],
                            "family": case["family"],
                            "reviewer_index": reviewer_index,
                            "review_mode": strategy["review_mode"],
                            "candidate_documents": [item["document"] for item in candidates],
                            "candidate_frequencies": [item["frequency"] for item in candidates],
                            "candidate_order_seed": seed,
                            "model": condition["model"],
                            "reasoning_effort": condition["reasoning_effort"],
                            "prompt": core.review_prompt(case, strategy, candidates),
                            "output_schema": core.output_schema(case),
                        }
                    )
                    normalizers[job_id] = lambda value, case=case: core.normalize_answer(case, value)
    random.Random(570_001).shuffle(jobs)
    return jobs, normalizers


def cross_plans(
    base_documents: dict[str, dict[str, list[dict[str, Any] | None]]]
) -> list[dict[str, Any]]:
    strategy = next(item for item in strategies() if item["review_mode"] == "cross_examine")
    plans: list[dict[str, Any]] = []
    for case_index, case in enumerate(public_cases()):
        for condition in conditions():
            documents = base_documents[condition["condition_id"]][case["case_id"]][: strategy["base_count"]]
            _, metrics = core.plurality(documents)
            if not core.review_needed(strategy, metrics, documents):
                continue
            seed = source_cross_seed(case, strategy)
            candidates = core.candidates_for(strategy, documents, seed)
            plans.append(
                {
                    "condition_id": condition["condition_id"],
                    "strategy_id": strategy["strategy_id"],
                    "case_id": case["case_id"],
                    "family": case["family"],
                    "review_count": strategy["review_count"],
                    "candidate_documents": [item["document"] for item in candidates],
                    "candidate_frequencies": [item["frequency"] for item in candidates],
                    "candidate_order_seed": seed,
                }
            )
    plans.sort(key=lambda item: (item["condition_id"], item["case_id"]))
    rendered = "".join(core.canonical(item) + "\n" for item in plans)
    path = CALLS / "cross-review" / "plan.jsonl"
    if path.exists() and path.read_text(encoding="utf-8") != rendered:
        raise core.LabError("Frozen cross-examination plan changed")
    if not path.exists():
        core.atomic_text(path, rendered)
    freeze(
        CALLS / "cross-review" / "manifest.json",
        {
            "registered_before_calls": True,
            "registered_at": core.registered_at(CALLS / "cross-review" / "manifest.json"),
            "chains": len(plans),
            "planned_calls": sum(item["review_count"] for item in plans),
            "plan_sha256": core.sha256_file(path),
            "accuracy_withheld": True,
        },
    )
    return plans


def run_cross(plans: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    cfg = config()
    case_lookup = {item["case_id"]: item for item in public_cases()}
    condition_lookup = {item["condition_id"]: item for item in conditions()}
    prior: dict[tuple[str, str], dict[str, Any] | None] = {}
    all_jobs: list[dict[str, Any]] = []
    all_results: dict[str, dict[str, Any]] = {}
    for layer in range(1, 4):
        jobs: list[dict[str, Any]] = []
        normalizers: dict[str, Callable[[Any], dict[str, Any] | None]] = {}
        for plan in plans:
            key = (plan["condition_id"], plan["case_id"])
            if layer > 1 and prior.get(key) is None:
                continue
            case = case_lookup[plan["case_id"]]
            condition = condition_lookup[plan["condition_id"]]
            candidates = [
                {"document": document, "frequency": frequency}
                for document, frequency in zip(
                    plan["candidate_documents"], plan["candidate_frequencies"], strict=True
                )
            ]
            job_id = f"{plan['condition_id']}-cross-{plan['case_id']}-r{layer:02d}"
            jobs.append(
                {
                    "job_id": job_id,
                    "stage": f"cross-review-{layer}",
                    "condition_id": plan["condition_id"],
                    "strategy_id": plan["strategy_id"],
                    "case_id": plan["case_id"],
                    "family": plan["family"],
                    "reviewer_index": layer,
                    "review_mode": "cross_examine",
                    "candidate_documents": plan["candidate_documents"],
                    "candidate_frequencies": plan["candidate_frequencies"],
                    "candidate_order_seed": plan["candidate_order_seed"],
                    "prior_review": prior.get(key),
                    "model": condition["model"],
                    "reasoning_effort": condition["reasoning_effort"],
                    "prompt": core.cross_exam_prompt(case, candidates, prior.get(key), layer),
                    "output_schema": core.cross_exam_schema(case),
                }
            )
            normalizers[job_id] = lambda value, case=case: core.normalize_cross_exam(case, value)
        random.Random(580_000 + layer).shuffle(jobs)
        stage = CALLS / "cross-review" / f"layer-{layer:02d}"
        stage_registry(
            stage,
            jobs,
            {
                "layer": layer,
                "models": conditions(),
                "prior_layer_outputs_sha256": core.sha256_bytes(
                    core.canonical({f"{key[0]}::{key[1]}": value for key, value in sorted(prior.items())}).encode()
                ),
            },
        )
        results = core.run_jobs(jobs, stage, int(cfg["max_concurrency"]), int(cfg["timeout_seconds"]), normalizers)
        assert_terminal(f"cross-review-{layer}", jobs, results)
        for job in jobs:
            prior[(job["condition_id"], job["case_id"])] = results[job["job_id"]].get("document")
        all_jobs.extend(jobs)
        all_results.update(results)
    return all_jobs, all_results


def read_stage(stage: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    jobs = core.read_stage_jobs(stage / "jobs.jsonl")
    return jobs, core.read_stage_results(stage, jobs)


def all_cross_results() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    jobs: list[dict[str, Any]] = []
    results: dict[str, dict[str, Any]] = {}
    for layer in range(1, 4):
        stage = CALLS / "cross-review" / f"layer-{layer:02d}"
        layer_jobs, layer_results = read_stage(stage)
        jobs.extend(layer_jobs)
        results.update(layer_results)
    return jobs, results


def expected_answers() -> dict[str, Any]:
    registry = load(ROOT / "registry.json")
    answers: dict[str, Any] = {}
    for case in public_cases():
        number = case["source_iteration"]
        path = LAB / "iterations" / f"iteration-{number:03d}" / "panel" / "sealed_answers.json"
        expected_hash = registry["source_panel_hashes"][str(number)]["sealed_answers_sha256"]
        if core.sha256_file(path) != expected_hash:
            raise core.LabError(f"Source sealed answers changed for Iteration {number}")
        answers[case["case_id"]] = load(path)["answers"][case["source_case_id"]]
    freeze(
        PANEL / "sealed_answers.json",
        {
            "experiment_id": config()["experiment_id"],
            "opened_only_after_terminal_worker_manifest": True,
            "answers": answers,
        },
    )
    return answers


def historical_luna_rows() -> list[dict[str, Any]]:
    cfg = config()
    condition = cfg["existing_condition"]
    target_signatures = {
        core.operational_signature(strategy): strategy for strategy in cfg["strategies"]
    }
    output: list[dict[str, Any]] = []
    for case in public_cases():
        number = case["source_iteration"]
        summary = load(LAB / "iterations" / f"iteration-{number:03d}" / "results" / "summary.json")
        id_to_signature = {
            strategy["strategy_id"]: core.operational_signature(strategy)
            for strategy in summary["strategies"]
        }
        rows = load(LAB / "iterations" / f"iteration-{number:03d}" / "results" / "case_results.json")
        by_signature = {
            id_to_signature[row["strategy_id"]]: row
            for row in rows
            if row["case_id"] == case["source_case_id"]
            and id_to_signature[row["strategy_id"]] in target_signatures
        }
        if set(by_signature) != set(target_signatures):
            raise core.LabError(f"Missing Luna Light historical rows for {case['case_id']}")
        for signature, strategy in target_signatures.items():
            source = by_signature[signature]
            output.append(
                {
                    **{key: value for key, value in source.items() if key not in {"iteration", "case_id", "strategy_id", "strategy_name"}},
                    "condition_id": condition["condition_id"],
                    "condition_label": condition["label"],
                    "requested_model": condition["model"],
                    "reasoning_effort": condition["reasoning_effort"],
                    "strategy_id": strategy["strategy_id"],
                    "strategy_name": strategy["name"],
                    "case_id": case["case_id"],
                    "source_iteration": number,
                    "source_case_id": case["source_case_id"],
                    "evidence_source": "existing-canonical-luna-light-result",
                }
            )
    return output


def new_condition_rows(
    answers: dict[str, Any],
    base_jobs_value: list[dict[str, Any]],
    base_results: dict[str, dict[str, Any]],
    review_jobs: list[dict[str, Any]],
    review_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    condition_lookup = {item["condition_id"]: item for item in conditions()}
    case_lookup = {item["case_id"]: item for item in public_cases()}
    base_documents = documents_by_condition(base_jobs_value, base_results)
    for condition_id, condition in condition_lookup.items():
        condition_jobs = [job for job in review_jobs if job["condition_id"] == condition_id]
        condition_results = {job["job_id"]: review_results[job["job_id"]] for job in condition_jobs}
        for strategy in strategies():
            for case_id, case in case_lookup.items():
                final, details = core.final_answer_for(
                    strategy,
                    case_id,
                    base_documents[condition_id],
                    condition_jobs,
                    condition_results,
                )
                expected = answers[case_id]
                exact = core.exact_score(final, expected)
                partial_correct, partial_total = core.partial_score(case, final, expected)
                base_exact = core.exact_score(details["base_answer"], expected)
                candidate_pool = base_documents[condition_id][case_id][: strategy["base_count"]]
                base_oracle = int(any(item == expected for item in candidate_pool))
                rows.append(
                    {
                        "condition_id": condition_id,
                        "condition_label": condition["label"],
                        "requested_model": condition["model"],
                        "reasoning_effort": condition["reasoning_effort"],
                        "reported_models": sorted(
                            {
                                str(base_results[job["job_id"]].get("reported_model"))
                                for job in base_jobs_value
                                if job["condition_id"] == condition_id
                                and base_results[job["job_id"]].get("reported_model")
                            }
                        ),
                        "strategy_id": strategy["strategy_id"],
                        "strategy_name": strategy["name"],
                        "case_id": case_id,
                        "source_iteration": case["source_iteration"],
                        "source_case_id": case["source_case_id"],
                        "family": case["family"],
                        "exact": exact,
                        "partial_correct": partial_correct,
                        "partial_total": partial_total,
                        "base_exact": base_exact,
                        "base_oracle": base_oracle,
                        "review_calls": details["review_calls"],
                        "effective_calls": details["effective_calls"],
                        "helpful_intervention": int(not base_exact and exact),
                        "harmful_intervention": int(base_exact and not exact),
                        "final_answer": core.canonical(final) if final is not None else "null",
                        "evidence_source": "new-transfer-call",
                    }
                )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = config()
    order = [cfg["existing_condition"], *cfg["conditions"]]
    summaries: list[dict[str, Any]] = []
    for condition in order:
        for strategy in cfg["strategies"]:
            subset = [
                row for row in rows
                if row["condition_id"] == condition["condition_id"]
                and row["strategy_id"] == strategy["strategy_id"]
            ]
            families = {}
            for family in ("sequence", "constraint", "logic"):
                family_rows = [row for row in subset if row["family"] == family]
                families[family] = {
                    "correct": sum(row["exact"] for row in family_rows),
                    "total": len(family_rows),
                    "accuracy": sum(row["exact"] for row in family_rows) / len(family_rows),
                }
            summaries.append(
                {
                    "condition_id": condition["condition_id"],
                    "condition_label": condition["label"],
                    "requested_model": condition["model"],
                    "reasoning_effort": condition["reasoning_effort"],
                    "strategy_id": strategy["strategy_id"],
                    "strategy_name": strategy["name"],
                    "correct": sum(row["exact"] for row in subset),
                    "total": len(subset),
                    "accuracy": sum(row["exact"] for row in subset) / len(subset),
                    "partial_accuracy": sum(row["partial_correct"] for row in subset)
                    / sum(row["partial_total"] for row in subset),
                    "families": families,
                    "worst_family_accuracy": min(value["accuracy"] for value in families.values()),
                    "helpful_interventions": sum(row.get("helpful_intervention", 0) for row in subset),
                    "harmful_interventions": sum(row.get("harmful_intervention", 0) for row in subset),
                    "review_calls": sum(row.get("review_calls", 0) for row in subset),
                    "mean_effective_calls": sum(row.get("effective_calls", 0) for row in subset) / len(subset),
                }
            )
    by_condition = defaultdict(dict)
    for item in summaries:
        by_condition[item["condition_id"]][item["strategy_id"]] = item
    lifts = []
    for condition in order:
        lookup = by_condition[condition["condition_id"]]
        baseline = lookup["plurality-5"]["accuracy"]
        for strategy_id in (
            "repair-falsify-5x1-efficient",
            "repair-falsify-5x3-efficient",
            "cross-examine-falsify-5x3",
        ):
            lifts.append(
                {
                    "condition_id": condition["condition_id"],
                    "condition_label": condition["label"],
                    "strategy_id": strategy_id,
                    "accuracy": lookup[strategy_id]["accuracy"],
                    "plurality_accuracy": baseline,
                    "lift_over_plurality": lookup[strategy_id]["accuracy"] - baseline,
                }
            )
    return {"strategies": summaries, "lifts": lifts}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row if not isinstance(row[key], (dict, list))})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in rows)


def write_plot(summary: dict[str, Any]) -> None:
    width, height = 1260, 650
    methods = [
        ("direct-1", "Direct"),
        ("plurality-5", "Plurality"),
        ("repair-falsify-5x1-efficient", "1 repair"),
        ("repair-falsify-5x3-efficient", "3 repairs"),
        ("cross-examine-falsify-5x3", "Cross-exam"),
    ]
    conditions_value = [config()["existing_condition"], *config()["conditions"]]
    colors = {"luna-light-existing": "#58d6ff", "terra-low": "#ffca64", "luna-medium": "#c58cff"}
    lookup = {(item["condition_id"], item["strategy_id"]): item for item in summary["strategies"]}
    left, top, chart_w, chart_h = 110, 155, 1080, 360
    group_w = chart_w / len(methods)
    bar_w = 42
    max_value = max(item["accuracy"] for item in summary["strategies"])
    ceiling = max(0.5, (int(max_value * 10) + 2) / 10)
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#070b16"/>',
        '<rect x="24" y="24" width="1212" height="602" rx="28" fill="#0d1527" stroke="#283859" stroke-width="2"/>',
        '<text x="64" y="76" fill="#f4f7ff" font-family="Inter,system-ui,sans-serif" font-size="30" font-weight="700">Cross-model orchestration transfer</text>',
        '<text x="64" y="112" fill="#9aa9c7" font-family="Inter,system-ui,sans-serif" font-size="17">Exact accuracy on 12 identical historical cases · four per family</text>',
    ]
    for tick in range(6):
        value = ceiling * tick / 5
        y = top + chart_h - value / ceiling * chart_h
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + chart_w}" y2="{y:.1f}" stroke="#263451"/>')
        svg.append(f'<text x="{left - 18}" y="{y + 5:.1f}" text-anchor="end" fill="#7585a3" font-family="ui-monospace,monospace" font-size="13">{100*value:.0f}%</text>')
    for method_index, (strategy_id, method_label) in enumerate(methods):
        center = left + group_w * (method_index + 0.5)
        for condition_index, condition in enumerate(conditions_value):
            item = lookup[(condition["condition_id"], strategy_id)]
            value = item["accuracy"]
            x = center + (condition_index - 1) * (bar_w + 8) - bar_w / 2
            y = top + chart_h - value / ceiling * chart_h
            h = top + chart_h - y
            color = colors[condition["condition_id"]]
            svg.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" rx="8" fill="{color}"/>')
            svg.append(f'<text x="{x + bar_w/2:.1f}" y="{y - 9:.1f}" text-anchor="middle" fill="{color}" font-family="ui-monospace,monospace" font-size="13" font-weight="700">{100*value:.1f}%</text>')
        svg.append(f'<text x="{center:.1f}" y="{top + chart_h + 34}" text-anchor="middle" fill="#dce4f5" font-family="Inter,system-ui,sans-serif" font-size="15">{method_label}</text>')
    legend_y = 584
    for index, condition in enumerate(conditions_value):
        x = 320 + index * 260
        color = colors[condition["condition_id"]]
        svg.append(f'<circle cx="{x}" cy="{legend_y}" r="7" fill="{color}"/>')
        svg.append(f'<text x="{x + 15}" y="{legend_y + 5}" fill="#cfd8eb" font-family="Inter,system-ui,sans-serif" font-size="15">{condition["label"]}</text>')
    svg.append('</svg>')
    core.atomic_text(RESULTS / "transfer.svg", "".join(svg))


def write_report(summary: dict[str, Any], total_calls: int) -> None:
    lookup = {(item["condition_id"], item["strategy_id"]): item for item in summary["strategies"]}
    conditions_value = [config()["existing_condition"], *config()["conditions"]]
    strategies_value = config()["strategies"]
    lines = [
        "# Cross-Model Orchestration Transfer Results",
        "",
        "This fixed screen compares existing Luna Light results with new Terra Low and Luna Medium calls on the same 12 historical cases. It is a model-transfer screen, not independent domain validation.",
        "",
        f"- New worker calls: **{total_calls}**",
        "- Cases: **12**, balanced four per family",
        "- Adaptive changes after registration: **none**",
        "",
        "| Method | Luna Light | Terra Low | Luna Medium |",
        "| --- | ---: | ---: | ---: |",
    ]
    for strategy in strategies_value:
        values = [lookup[(condition["condition_id"], strategy["strategy_id"])] for condition in conditions_value]
        lines.append(
            f"| {strategy['name']} | "
            + " | ".join(f"{item['correct']}/{item['total']} ({100*item['accuracy']:.1f}%)" for item in values)
            + " |"
        )
    lines.extend(["", "## Lift over five-vote plurality", ""])
    for condition in conditions_value:
        lines.append(f"**{condition['label']}**")
        lines.append("")
        condition_lifts = [item for item in summary["lifts"] if item["condition_id"] == condition["condition_id"]]
        for item in condition_lifts:
            name = next(strategy["name"] for strategy in strategies_value if strategy["strategy_id"] == item["strategy_id"])
            lines.append(f"- {name}: **{100*item['lift_over_plurality']:+.1f} points**")
        lines.append("")
    lines.extend(
        [
            "## Interpretation boundary",
            "",
            "Twelve cases can reveal large directional effects but cannot distinguish small performance differences reliably. Historical cases permit exact cross-model matching, but the mechanisms were developed within this task distribution. Unrelated-domain validation remains separate.",
            "",
            "![Transfer comparison](results/transfer.svg)",
            "",
        ]
    )
    core.atomic_text(ROOT / "REPORT.md", "\n".join(lines))


def score() -> None:
    verify_registered_runtime()
    if not (ROOT / "terminal_worker_manifest.json").is_file():
        raise core.LabError("Worker stages are not durably complete; refusing to parse answers")
    answers = expected_answers()
    base_jobs_value, base_results = read_stage(CALLS / "base")
    parallel_jobs, parallel_results = read_stage(CALLS / "parallel-review")
    cross_jobs_value, cross_results = all_cross_results()
    rows = historical_luna_rows()
    rows.extend(
        new_condition_rows(
            answers,
            base_jobs_value,
            base_results,
            parallel_jobs + cross_jobs_value,
            parallel_results | cross_results,
        )
    )
    summary = summarize(rows)
    total_calls = len(base_jobs_value) + len(parallel_jobs) + len(cross_jobs_value)
    output = {
        "experiment_id": config()["experiment_id"],
        "scored_after_terminal_worker_manifest": True,
        "new_worker_calls": total_calls,
        "maximum_new_calls": config()["maximum_new_calls"],
        "cases": len(public_cases()),
        "summary": summary,
    }
    freeze(RESULTS / "case_results.json", rows)
    freeze(RESULTS / "summary.json", output)
    write_csv(RESULTS / "case_results.csv", rows)
    write_plot(summary)
    write_report(summary, total_calls)
    print(json.dumps(output, indent=2))


def run() -> None:
    prepare()
    verify_registered_runtime()
    cfg = config()
    if planned_maximum_calls(cfg) > int(cfg["maximum_new_calls"]):
        raise core.LabError("Planned calls exceed the frozen maximum; refusing to start workers")
    jobs, normalizers = base_jobs()
    stage_registry(
        CALLS / "base",
        jobs,
        {"models": conditions(), "max_base_count": 5, "planned_calls": len(jobs)},
    )
    base_results = core.run_jobs(
        jobs,
        CALLS / "base",
        int(cfg["max_concurrency"]),
        int(cfg["timeout_seconds"]),
        normalizers,
    )
    assert_terminal("base", jobs, base_results)
    base_documents = documents_by_condition(jobs, base_results)

    review_jobs_value, review_normalizers = parallel_review_jobs(base_documents)
    stage_registry(
        CALLS / "parallel-review",
        review_jobs_value,
        {"models": conditions(), "planned_calls": len(review_jobs_value)},
    )
    review_results = core.run_jobs(
        review_jobs_value,
        CALLS / "parallel-review",
        int(cfg["max_concurrency"]),
        int(cfg["timeout_seconds"]),
        review_normalizers,
    )
    assert_terminal("parallel-review", review_jobs_value, review_results)

    plans = cross_plans(base_documents)
    cross_jobs_value, cross_results = run_cross(plans)
    all_jobs = jobs + review_jobs_value + cross_jobs_value
    all_results = base_results | review_results | cross_results
    telemetry = model_telemetry(all_jobs, all_results)
    freeze(
        ROOT / "terminal_worker_manifest.json",
        {
            "all_registered_worker_stages_terminal": True,
            "recorded_at": core.registered_at(ROOT / "terminal_worker_manifest.json"),
            "jobs": len(all_jobs),
            "outcomes": dict(Counter(all_results[job["job_id"]]["outcome"] for job in all_jobs)),
            "result_hashes": {
                job["job_id"]: core.sha256_file(
                    next(
                        path
                        for path in CALLS.rglob(f"{job['job_id']}/result.json")
                        if "/attempt-" not in str(path)
                    )
                )
                for job in all_jobs
            },
            "model_telemetry": telemetry,
            "answers_not_yet_parsed": True,
        },
    )
    score()


def status() -> None:
    stages = {}
    for path in sorted(CALLS.glob("**/progress.json")):
        stages[str(path.parent.relative_to(ROOT))] = load(path)
    value = {
        "prepared": (ROOT / "registry.json").is_file(),
        "terminal": (ROOT / "terminal_worker_manifest.json").is_file(),
        "scored": (RESULTS / "summary.json").is_file(),
        "stages": stages,
    }
    print(json.dumps(value, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run", "score", "status"))
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "run":
        run()
    elif args.command == "score":
        score()
    else:
        status()


if __name__ == "__main__":
    main()
