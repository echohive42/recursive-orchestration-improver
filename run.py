#!/usr/bin/env python3
"""Run a continuous, sealed orchestration auto-research loop."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import hashlib
import html
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parent
PROTOCOL_PATH = ROOT / "protocol.json"
RESEARCHER_PATH = ROOT / "researcher.md"
STATE_PATH = ROOT / "state.json"
STRATEGIES = ROOT / "strategies"
ITERATIONS = ROOT / "iterations"
PLOTS = ROOT / "plots"
STOP_PATH = ROOT / "STOP"

DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "enable_mcp_apps",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "multi_agent_v2",
    "plugin_sharing",
    "plugins",
    "remote_plugin",
    "shell_tool",
    "skill_mcp_dependency_install",
    "standalone_web_search",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
)
TOOL_MARKERS = (
    "tool_call",
    "function_call",
    "mcp_tool_call",
    "command_execution",
    "computer_action",
    "browser_action",
)
ODD_BASE_COUNTS = {1, 3, 5, 7, 9, 11, 13, 15}
REVIEW_COUNTS = {0, 1, 3, 5}
REVIEW_TRIGGERS = {"never", "always", "any_disagreement", "no_strict_majority", "committee_disagreement"}
REVIEW_MODES = {"none", "choose", "repair", "cross_examine", "regenerate"}
REVIEW_STYLES = {"verify", "rederive", "falsify", "compare"}
FINAL_RULES = {
    "base_plurality", "review_plurality_fallback_base", "review_plus_base_plurality",
    "last_review_fallback_base", "augmented_plurality",
}
CANDIDATE_SOURCES = {"base_unique", "committee_delegates"}
FAMILY_LABELS = {"sequence": "Sequence", "constraint": "Planning", "logic": "Logic"}
OPERATIONAL_FIELDS = (
    "base_count", "review_count", "review_trigger", "review_mode", "review_style",
    "candidate_limit", "candidate_source", "show_frequencies", "final_rule",
)


class LabError(RuntimeError):
    pass


STOP_REQUESTED = False


def request_stop(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print("\nStop requested. The current durable call stage will finish, then the loop will stop.", flush=True)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_text(path: Path, value: str) -> None:
    atomic_bytes(path, value.encode("utf-8"))


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def freeze_json(path: Path, value: Any) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise LabError(f"Refusing to change frozen file: {path}")
        return
    atomic_text(path, rendered)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def registered_at(path: Path, fallback: str | None = None) -> str:
    """Reuse a frozen registration timestamp when resuming an existing stage."""
    if path.is_file():
        value = load_json(path).get("registered_at")
        if isinstance(value, str) and value:
            return value
    return fallback or utc_now()


def protocol() -> dict[str, Any]:
    return load_json(PROTOCOL_PATH)


def iteration_dir(number: int) -> Path:
    return ITERATIONS / f"iteration-{number:03d}"


def strategy_path(number: int) -> Path:
    return STRATEGIES / f"iteration-{number:03d}.json"


def output_schema(case: dict[str, Any]) -> dict[str, Any]:
    family = case["family"]
    if family == "sequence":
        length = len(case["answer_schema"]["answer"])
        return {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": length,
                    "maxItems": length,
                }
            },
            "required": ["answer"],
            "additionalProperties": False,
        }
    if family == "constraint":
        jobs = list(case["answer_schema"]["answer"])
        return {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "object",
                    "properties": {job: {"type": "string", "enum": ["W", "X", "Y", "Z"]} for job in jobs},
                    "required": jobs,
                    "additionalProperties": False,
                },
                "total_cost": {"type": "integer"},
            },
            "required": ["answer", "total_cost"],
            "additionalProperties": False,
        }
    people = list(case["answer_schema"]["answer"])
    return {
        "type": "object",
        "properties": {
            "answer": {
                "type": "object",
                "properties": {person: {"type": "string", "enum": ["truthful", "liar"]} for person in people},
                "required": people,
                "additionalProperties": False,
            }
        },
        "required": ["answer"],
        "additionalProperties": False,
    }


def normalize_answer(case: dict[str, Any], value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    family = case["family"]
    if family == "sequence":
        length = len(case["answer_schema"]["answer"])
        answer = value.get("answer")
        if set(value) != {"answer"} or not isinstance(answer, list) or len(answer) != length:
            return None
        if any(not isinstance(item, int) or isinstance(item, bool) for item in answer):
            return None
        return {"answer": list(answer)}
    keys = list(case["answer_schema"]["answer"])
    answer = value.get("answer")
    if not isinstance(answer, dict) or set(answer) != set(keys):
        return None
    if family == "constraint":
        if set(value) != {"answer", "total_cost"}:
            return None
        if any(item not in {"W", "X", "Y", "Z"} for item in answer.values()):
            return None
        total_cost = value.get("total_cost")
        if not isinstance(total_cost, int) or isinstance(total_cost, bool):
            return None
        return {"answer": dict(answer), "total_cost": total_cost}
    if set(value) != {"answer"} or any(item not in {"truthful", "liar"} for item in answer.values()):
        return None
    return {"answer": dict(answer)}


def choice_schema(candidate_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"choice": {"type": "integer", "minimum": 0, "maximum": candidate_count - 1}},
        "required": ["choice"],
        "additionalProperties": False,
    }


def normalize_choice(candidate_count: int, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"choice"}:
        return None
    choice = value["choice"]
    if not isinstance(choice, int) or isinstance(choice, bool) or not 0 <= choice < candidate_count:
        return None
    return {"choice": choice}


def cross_exam_schema(case: dict[str, Any]) -> dict[str, Any]:
    schema = output_schema(case)
    schema["properties"] = dict(schema["properties"])
    schema["properties"]["critique"] = {"type": "string"}
    schema["required"] = list(schema["required"]) + ["critique"]
    return schema


def normalize_cross_exam(case: dict[str, Any], value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("critique"), str):
        return None
    critique = value["critique"].strip()
    if not critique or len(critique) > 600:
        return None
    answer_value = {key: item for key, item in value.items() if key != "critique"}
    answer = normalize_answer(case, answer_value)
    if answer is None:
        return None
    return {"answer_document": answer, "critique": critique}


def codex_command(
    binary: Path,
    model: str,
    effort: str,
    schema_path: Path,
    last_path: Path,
    workspace: Path,
) -> list[str]:
    command = [
        str(binary),
        "exec",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-c",
        'approval_policy="never"',
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
    ]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    command.extend((
        "--json",
        "--color",
        "never",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(last_path),
        "-C",
        str(workspace),
        "-",
    ))
    return command


def parse_events(raw: str) -> tuple[list[dict[str, Any]], int, bool]:
    events: list[dict[str, Any]] = []
    failures = 0
    tool_event = False
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            failures += 1
            continue
        if not isinstance(event, dict):
            failures += 1
            continue
        events.append(event)
        event_type = str(event.get("type", "")).lower()
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_type = str(item.get("type", "")).lower()
        tool_event = tool_event or any(marker in event_type or marker in item_type for marker in TOOL_MARKERS)
    return events, failures, tool_event


def final_message(events: list[dict[str, Any]], path: Path) -> str | None:
    if path.is_file():
        value = path.read_text(encoding="utf-8", errors="replace").strip()
        if value:
            return value
    messages: list[str] = []
    for event in events:
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if str(event.get("type", "")).lower() == "item.completed" and item.get("type") == "agent_message":
            value = item.get("text", item.get("content"))
            if isinstance(value, str) and value.strip():
                messages.append(value.strip())
    return messages[-1] if messages else None


def telemetry(events: list[dict[str, Any]]) -> tuple[dict[str, int] | None, str | None]:
    usage: dict[str, int] | None = None
    model: str | None = None
    for event in events:
        raw_usage = event.get("usage")
        if isinstance(raw_usage, dict):
            normalized = {
                key: value for key, value in raw_usage.items()
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            }
            if normalized:
                usage = normalized
        for candidate in (event.get("model"), event.get("model_id"), event.get("model_name")):
            if isinstance(candidate, str) and candidate:
                model = candidate
    return usage, model


Normalizer = Callable[[Any], dict[str, Any] | None]


def run_attempt(
    binary: Path,
    job: dict[str, Any],
    attempt_dir: Path,
    timeout_seconds: int,
    normalizer: Normalizer,
) -> dict[str, Any]:
    attempt_dir.mkdir(parents=True, exist_ok=False)
    schema_path = attempt_dir / "output_schema.json"
    last_path = attempt_dir / "last_message.txt"
    workspace = Path(tempfile.mkdtemp(prefix="codex-work-"))
    atomic_json(schema_path, job["output_schema"])
    atomic_text(attempt_dir / "prompt.txt", job["prompt"])
    command = codex_command(
        binary,
        job["model"],
        job["reasoning_effort"],
        schema_path,
        last_path,
        workspace,
    )
    public_command = ["<CODEX_BINARY>" if index == 0 else value for index, value in enumerate(command)]
    atomic_json(attempt_dir / "command.json", {"argv": public_command, "prompt_transport": "stdin"})
    started_at = utc_now()
    started = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    stdout = b""
    stderr = b""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        atomic_json(attempt_dir / "process.json", {"pid": process.pid, "started_at": started_at})
        try:
            stdout, stderr = process.communicate(job["prompt"].encode("utf-8"), timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate()
        exit_code = process.returncode
    except OSError as exc:
        stderr = str(exc).encode("utf-8", errors="replace")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    duration_ms = round((time.monotonic() - started) * 1000)
    atomic_bytes(attempt_dir / "events.jsonl", stdout)
    atomic_bytes(attempt_dir / "stderr.txt", stderr)
    events, parse_failures, tool_event = parse_events(stdout.decode("utf-8", errors="replace"))
    response_text = final_message(events, last_path)
    if response_text is not None and not last_path.exists():
        atomic_text(last_path, response_text)
    usage, reported_model = telemetry(events)
    document: dict[str, Any] | None = None
    errors: list[str] = []
    event_text = canonical(events)
    configuration_error = "invalid_json_schema" in event_text or "invalid_request_error" in event_text
    if tool_event:
        status = "protocol_violation"
        errors.append("forbidden tool event")
    elif configuration_error and not response_text:
        status = "configuration_failure"
        errors.append("request rejected before inference")
    elif isinstance(response_text, str) and response_text.strip():
        try:
            raw_document = json.loads(response_text)
        except json.JSONDecodeError as exc:
            status = "schema_invalid"
            errors.append(f"invalid JSON: {exc}")
        else:
            document = normalizer(raw_document)
            if document is None:
                status = "schema_invalid"
                errors.append("response failed exact semantic normalization")
            else:
                status = "valid_output"
    else:
        status = "infrastructure_failure"
        if timed_out:
            errors.append("hard timeout")
        if exit_code not in {0, None}:
            errors.append(f"exit code {exit_code}")
        if stderr:
            errors.append(stderr.decode("utf-8", errors="replace")[:1000])
        if not errors:
            errors.append("no substantive final agent message")
    result = {
        "job_id": job["job_id"],
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_ms": duration_ms,
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "jsonl_event_count": len(events),
        "jsonl_parse_failures": parse_failures,
        "tool_event": tool_event,
        "reported_model": reported_model,
        "usage": usage,
        "response_sha256": sha256_bytes(response_text.encode("utf-8")) if response_text else None,
        "document": document,
        "errors": errors,
    }
    atomic_json(attempt_dir / "result.json", result)
    return result


def execute_job(
    binary: Path,
    job: dict[str, Any],
    stage_dir: Path,
    timeout_seconds: int,
    max_infrastructure_attempts: int,
    normalizer: Normalizer,
) -> dict[str, Any]:
    job_dir = stage_dir / "jobs" / job["job_id"]
    final_path = job_dir / "result.json"
    if final_path.is_file():
        return load_json(final_path)
    job_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(job_dir / "request.json", {
        key: value for key, value in job.items() if key not in {"prompt", "output_schema"}
    } | {
        "prompt_sha256": sha256_bytes(job["prompt"].encode("utf-8")),
        "schema_sha256": sha256_bytes(canonical(job["output_schema"]).encode("utf-8")),
    })
    existing = sorted(path for path in job_dir.glob("attempt-*") if path.is_dir())
    attempts: list[dict[str, Any]] = []
    for path in existing:
        result_path = path / "result.json"
        if result_path.is_file():
            attempts.append(load_json(result_path))
        else:
            synthetic = {
                "job_id": job["job_id"], "status": "infrastructure_failure", "duration_ms": 0,
                "reported_model": None, "usage": None, "document": None,
                "response_sha256": None, "errors": ["interrupted before durable attempt result"],
            }
            atomic_json(result_path, synthetic)
            attempts.append(synthetic)
    terminal = next((item for item in attempts if item["status"] != "infrastructure_failure"), None)
    while terminal is None and len(attempts) < max_infrastructure_attempts:
        attempt = run_attempt(
            binary,
            job,
            job_dir / f"attempt-{len(attempts) + 1:02d}",
            timeout_seconds,
            normalizer,
        )
        attempts.append(attempt)
        if attempt["status"] != "infrastructure_failure":
            terminal = attempt
    if not attempts:
        raise LabError(f"No attempt could be created for {job['job_id']}")
    terminal = terminal or attempts[-1]
    final = {
        "job_id": job["job_id"],
        "outcome": terminal["status"] if terminal["status"] != "infrastructure_failure" else "infrastructure_exhausted",
        "attempt_count": len(attempts),
        "terminal_attempt": attempts.index(terminal) + 1,
        "duration_ms": sum(int(item.get("duration_ms", 0)) for item in attempts),
        "reported_model": terminal.get("reported_model"),
        "usage": terminal.get("usage"),
        "document": terminal.get("document"),
        "response_sha256": terminal.get("response_sha256"),
    }
    atomic_json(final_path, final)
    return final


class Progress:
    def __init__(self, path: Path, total: int, stage: str):
        self.path = path
        self.total = total
        self.stage = stage
        self.completed = 0
        self.statuses: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.started = time.monotonic()

    def record(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.completed += 1
            self.statuses[result["outcome"]] += 1
            elapsed = time.monotonic() - self.started
            rate = self.completed / elapsed if elapsed else 0.0
            remaining = (self.total - self.completed) / rate if rate else None
            atomic_json(self.path, {
                "updated_at": utc_now(),
                "stage": self.stage,
                "completed": self.completed,
                "total": self.total,
                "percent": self.completed / self.total if self.total else 1.0,
                "status_counts": dict(self.statuses),
                "jobs_per_minute": rate * 60,
                "estimated_seconds_remaining": remaining,
                "accuracy_withheld": True,
            })
            interval = 20 if self.total >= 100 else 10
            if self.completed == self.total or self.completed % interval == 0:
                eta = f", ETA {remaining / 60:.1f}m" if remaining is not None else ""
                print(f"[{utc_now()}] {self.stage}: {self.completed}/{self.total} ({100 * self.completed / self.total:.1f}%){eta}", flush=True)


def run_jobs(
    jobs: list[dict[str, Any]],
    stage_dir: Path,
    concurrency: int,
    timeout_seconds: int,
    normalizers: dict[str, Normalizer],
) -> dict[str, dict[str, Any]]:
    if not jobs:
        atomic_json(stage_dir / "progress.json", {
            "updated_at": utc_now(), "stage": stage_dir.name, "completed": 0, "total": 0,
            "percent": 1.0, "status_counts": {}, "accuracy_withheld": True,
        })
        return {}
    config = protocol()
    binary = Path(shutil.which("codex") or "codex").resolve()
    if not binary.is_file():
        raise LabError("Codex CLI not found")
    progress = Progress(stage_dir / "progress.json", len(jobs), stage_dir.name)
    results: dict[str, dict[str, Any]] = {}

    def task(job: dict[str, Any]) -> dict[str, Any]:
        return execute_job(
            binary,
            job,
            stage_dir,
            timeout_seconds,
            int(config["max_infrastructure_attempts"]),
            normalizers[job["job_id"]],
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_map = {pool.submit(task, job): job for job in jobs}
        for future in concurrent.futures.as_completed(future_map):
            job = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                raise LabError(f"Job crashed outside its durable attempt record: {job['job_id']}: {exc}") from exc
            results[job["job_id"]] = result
            progress.record(result)
    return results


def validate_strategy(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    required = {
        "strategy_id", "name", "hypothesis", "base_count", "review_count",
        "review_trigger", "review_mode", "review_style", "candidate_limit",
        "show_frequencies", "final_rule",
    }
    allowed = required | {"candidate_source"}
    if not isinstance(value, dict) or not required.issubset(value) or not set(value).issubset(allowed):
        return None, "strategy fields do not match the grammar"
    if not all(isinstance(value[key], str) and value[key].strip() for key in ("strategy_id", "name", "hypothesis")):
        return None, "strategy identifiers and prose must be nonempty strings"
    strategy_id = value["strategy_id"]
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in strategy_id):
        return None, "strategy_id must use lowercase letters, digits, and hyphens"
    if value["base_count"] not in ODD_BASE_COUNTS:
        return None, "unsupported base_count"
    if value["review_count"] not in REVIEW_COUNTS:
        return None, "unsupported review_count"
    if value["review_trigger"] not in REVIEW_TRIGGERS:
        return None, "unsupported review_trigger"
    if value["review_mode"] not in REVIEW_MODES:
        return None, "unsupported review_mode"
    if value["review_style"] not in REVIEW_STYLES:
        return None, "unsupported review_style"
    if value["final_rule"] not in FINAL_RULES:
        return None, "unsupported final_rule"
    if not isinstance(value["candidate_limit"], int) or isinstance(value["candidate_limit"], bool) or not 2 <= value["candidate_limit"] <= 8:
        return None, "candidate_limit must be 2 through 8"
    if not isinstance(value["show_frequencies"], bool):
        return None, "show_frequencies must be Boolean"
    candidate_source = value.get("candidate_source", "base_unique")
    if candidate_source not in CANDIDATE_SOURCES:
        return None, "unsupported candidate_source"
    if candidate_source == "committee_delegates" and value["base_count"] != 9:
        return None, "committee delegates currently require exactly nine base solvers"
    if value["review_trigger"] == "committee_disagreement" and candidate_source != "committee_delegates":
        return None, "committee disagreement requires committee delegates"
    if value["review_count"] == 0:
        if value["review_mode"] != "none" or value["review_trigger"] != "never" or value["final_rule"] != "base_plurality":
            return None, "zero-review strategies must be pure base plurality"
    elif value["review_mode"] == "none" or value["review_trigger"] == "never" or value["final_rule"] == "base_plurality":
        return None, "review strategies must specify a real review mode, trigger, and final rule"
    if value["review_mode"] == "cross_examine":
        if value["review_count"] != 3:
            return None, "cross-examination currently requires exactly three sequential reviewers"
        if value["final_rule"] != "last_review_fallback_base":
            return None, "cross-examination requires the last-review fallback rule"
        if candidate_source != "base_unique":
            return None, "cross-examination currently requires raw base candidates"
        if value["review_style"] != "falsify" or value["show_frequencies"]:
            return None, "cross-examination requires falsification with hidden frequencies"
    elif value["final_rule"] == "last_review_fallback_base":
        return None, "last-review fallback is reserved for cross-examination"
    if value["review_mode"] == "regenerate":
        if value["review_count"] != 3:
            return None, "blind regeneration requires exactly three regenerators"
        if value["final_rule"] != "augmented_plurality":
            return None, "blind regeneration requires augmented plurality"
        if candidate_source != "base_unique":
            return None, "blind regeneration requires the raw base bank"
        if value["review_style"] != "rederive" or value["show_frequencies"]:
            return None, "blind regeneration uses fixed rederivation lenses without frequencies"
        if value["candidate_limit"] != value["base_count"]:
            return None, "blind regeneration requires candidate_limit to equal base_count"
    elif value["final_rule"] == "augmented_plurality":
        return None, "augmented plurality is reserved for blind regeneration"
    normalized = dict(value)
    normalized["candidate_source"] = candidate_source
    return normalized, None


def validate_strategy_set(document: Any, cases: int) -> list[dict[str, Any]]:
    config = protocol()
    if not isinstance(document, dict) or not isinstance(document.get("strategies"), list):
        raise LabError("Strategy file must contain a strategies list")
    raw = document["strategies"]
    if not 1 <= len(raw) <= int(config["budgets"]["max_strategies"]):
        raise LabError("Strategy batch has the wrong size")
    strategies: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, item in enumerate(raw, 1):
        normalized, error = validate_strategy(item)
        if error:
            errors.append(f"strategy {index}: {error}")
        else:
            strategies.append(normalized or {})
    if errors:
        raise LabError("; ".join(errors))
    ids = [item["strategy_id"] for item in strategies]
    if len(set(ids)) != len(ids):
        raise LabError("Strategy IDs must be unique within an iteration")
    max_base = max(item["base_count"] for item in strategies)
    maximum_calls = cases * (max_base + sum(item["review_count"] for item in strategies))
    checkpoint = cases > 12
    budget_key = "checkpoint_max_luna_calls" if checkpoint else "ordinary_max_luna_calls"
    budget = int(config["budgets"][budget_key])
    if maximum_calls > budget:
        raise LabError(f"Strategy batch can use {maximum_calls} Luna calls, above the {budget} budget")
    return strategies


def initialize() -> None:
    config = protocol()
    STRATEGIES.mkdir(parents=True, exist_ok=True)
    ITERATIONS.mkdir(parents=True, exist_ok=True)
    PLOTS.mkdir(parents=True, exist_ok=True)
    seed = load_json(strategy_path(1))
    validate_strategy_set(seed, 12)
    if not STATE_PATH.exists():
        atomic_json(STATE_PATH, {
            "experiment_id": config["experiment_id"],
            "status": "ready",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "next_iteration": 1,
            "completed_iterations": [],
            "current_strategy_file": str(strategy_path(1).relative_to(ROOT)),
            "best_observed": null_record(),
            "notes": [
                "Fresh panels are disposable search evidence, not independent final validation.",
                "The main agent may revise researcher.md between iterations; every director call records its hash.",
            ],
        })
    state = load_json(STATE_PATH)
    if state.get("experiment_id") != config["experiment_id"]:
        raise LabError("state.json belongs to another experiment")
    # Older iterations created before protocol snapshotting are migrated only
    # when the still-current protocol hash exactly matches their frozen hash.
    current_protocol_hash = sha256_file(PROTOCOL_PATH)
    for registry_path in sorted(ITERATIONS.glob("iteration-*/registry.json")):
        snapshot_path = registry_path.parent / "protocol_snapshot.json"
        snapshot_manifest_path = registry_path.parent / "protocol_snapshot_manifest.json"
        registry = load_json(registry_path)
        if not snapshot_path.exists() and registry.get("protocol_sha256") == current_protocol_hash:
            freeze_json(snapshot_path, config)
            freeze_json(snapshot_manifest_path, {
                "source_protocol_sha256": registry["protocol_sha256"],
                "snapshot_sha256": sha256_file(snapshot_path),
                "created_while_source_hash_matched_registry": True,
            })
    print(f"Lab initialized. Next iteration: {state['next_iteration']}")


def null_record() -> dict[str, Any]:
    return {
        "iteration": None,
        "strategy_id": None,
        "name": None,
        "accuracy": None,
        "worst_family_accuracy": None,
    }


def panel_spec(number: int) -> dict[str, int]:
    config = protocol()["panel_generation"]
    checkpoint = number % int(config["checkpoint_every"]) == 0
    cases_per_family = int(config["checkpoint_cases_per_family"] if checkpoint else config["ordinary_cases_per_family"])
    return {
        "seed": int(config["base_seed"]) + number * 1009,
        "cases_per_family": cases_per_family,
        "total_cases": cases_per_family * 3,
        "checkpoint": int(checkpoint),
    }


def generate_panel(number: int) -> Path:
    config = protocol()
    directory = iteration_dir(number) / "panel"
    required = [directory / name for name in ("public_cases.json", "sealed_answers.json", "panel_hashes.json")]
    if all(path.is_file() for path in required):
        return directory
    if directory.exists() and any(directory.iterdir()):
        raise LabError(f"Incomplete nonempty panel directory: {directory}")
    spec = panel_spec(number)
    source = (ROOT / config["panel_generation"]["source"]).resolve()
    if not source.is_file():
        raise LabError(f"Panel generator not found: {source}")
    command = [
        sys.executable,
        str(source),
        "--tier", str(config["panel_generation"]["tier"]),
        "--seed", str(spec["seed"]),
        "--cases-per-family", str(spec["cases_per_family"]),
        "--families", str(config["panel_generation"]["families"]),
        "--phase", str(config["panel_generation"]["phase"]),
        "--output-dir", str(directory),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
    if completed.returncode != 0:
        raise LabError(f"Panel generation failed: {completed.stderr or completed.stdout}")
    print(completed.stdout.strip())
    return directory


def freeze_iteration(number: int) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]]]:
    directory = iteration_dir(number)
    directory.mkdir(parents=True, exist_ok=True)
    panel = generate_panel(number)
    public = load_json(panel / "public_cases.json")
    cases = public["cases"]
    expected_families = {"sequence": len(cases) // 3, "constraint": len(cases) // 3, "logic": len(cases) // 3}
    actual = Counter(case["family"] for case in cases)
    if dict(actual) != expected_families:
        raise LabError(f"Unbalanced panel: {dict(actual)}")
    for case in cases:
        if any(key in case for key in ("answer", "expected", "solution")):
            raise LabError(f"Answer-like field leaked into public case {case['case_id']}")
    strategies_doc = load_json(strategy_path(number))
    strategies = validate_strategy_set(strategies_doc, len(cases))
    snapshot_path = directory / "protocol_snapshot.json"
    freeze_json(snapshot_path, protocol())
    registry = {
        "experiment_id": protocol()["experiment_id"],
        "iteration": number,
        "registered_before_calls": True,
        "registered_at": registered_at(directory / "registry.json"),
        "checkpoint": bool(panel_spec(number)["checkpoint"]),
        "cases": len(cases),
        "family_counts": dict(actual),
        "public_cases_sha256": sha256_file(panel / "public_cases.json"),
        "sealed_answers_sha256": sha256_file(panel / "sealed_answers.json"),
        "panel_hashes_sha256": sha256_file(panel / "panel_hashes.json"),
        "strategy_file": str(strategy_path(number).relative_to(ROOT)),
        "strategy_file_sha256": sha256_file(strategy_path(number)),
        "protocol_sha256": sha256_file(PROTOCOL_PATH),
        "protocol_snapshot_sha256": sha256_file(snapshot_path),
        "researcher_sha256_at_start": sha256_file(RESEARCHER_PATH),
        "answers_loaded_after_worker_calls": True,
        "sealed_answers_not_sent_to_workers": True,
        "strategies": strategies,
    }
    freeze_json(directory / "registry.json", registry)
    return directory, cases, strategies


def verify_iteration(number: int) -> None:
    directory = iteration_dir(number)
    registry = load_json(directory / "registry.json")
    panel = directory / "panel"
    snapshot_path = directory / "protocol_snapshot.json"
    snapshot_manifest_path = directory / "protocol_snapshot_manifest.json"
    if "protocol_snapshot_sha256" in registry:
        expected_protocol_hash = registry["protocol_snapshot_sha256"]
    elif snapshot_manifest_path.is_file():
        snapshot_manifest = load_json(snapshot_manifest_path)
        if snapshot_manifest.get("source_protocol_sha256") != registry["protocol_sha256"]:
            raise LabError(f"Iteration {number} protocol snapshot source hash drifted")
        expected_protocol_hash = snapshot_manifest["snapshot_sha256"]
    else:
        expected_protocol_hash = registry["protocol_sha256"]
    actual_protocol_hash = sha256_file(snapshot_path) if snapshot_path.is_file() else sha256_file(PROTOCOL_PATH)
    checks = {
        "public_cases_sha256": sha256_file(panel / "public_cases.json"),
        "sealed_answers_sha256": sha256_file(panel / "sealed_answers.json"),
        "panel_hashes_sha256": sha256_file(panel / "panel_hashes.json"),
        "strategy_file_sha256": sha256_file(ROOT / registry["strategy_file"]),
        "protocol_snapshot_sha256": actual_protocol_hash,
    }
    for key, actual in checks.items():
        expected = expected_protocol_hash if key == "protocol_snapshot_sha256" else registry[key]
        if expected != actual:
            raise LabError(f"Iteration {number} frozen hash drifted: {key}")


def base_prompt(case: dict[str, Any]) -> str:
    return (
        "You are one independent solver in a controlled reasoning study. Work only from the problem below. "
        "Use no tools, code execution, Python, web access, or external files. Solve the exact problem carefully "
        "and verify the final result. Do not discuss the study or other agents.\n\n"
        f"<problem>\n{case['prompt'].strip()}\n</problem>\n\n"
        "Return only the exact requested JSON object."
    )


def freeze_base_jobs(number: int, cases: list[dict[str, Any]], strategies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Normalizer]]:
    config = protocol()
    max_base = max(item["base_count"] for item in strategies)
    jobs: list[dict[str, Any]] = []
    normalizers: dict[str, Normalizer] = {}
    for case in cases:
        for bank_index in range(1, max_base + 1):
            job_id = f"i{number:03d}-base-{case['case_id']}-b{bank_index:02d}"
            job = {
                "job_id": job_id,
                "iteration": number,
                "stage": "base",
                "case_id": case["case_id"],
                "family": case["family"],
                "bank_index": bank_index,
                "model": config["worker_model"],
                "reasoning_effort": config["worker_reasoning_effort"],
                "prompt": base_prompt(case),
                "output_schema": output_schema(case),
            }
            jobs.append(job)
            normalizers[job_id] = lambda value, case=case: normalize_answer(case, value)
    random.Random(100_000 + number).shuffle(jobs)
    stage = iteration_dir(number) / "base"
    rendered = "".join(canonical(job) + "\n" for job in jobs)
    registry_path = stage / "jobs.jsonl"
    if registry_path.exists() and registry_path.read_text(encoding="utf-8") != rendered:
        raise LabError("Frozen base registry changed")
    if not registry_path.exists():
        atomic_text(registry_path, rendered)
    freeze_json(stage / "manifest.json", {
        "registered_before_calls": True,
        "registered_at": registered_at(stage / "manifest.json"),
        "jobs": len(jobs),
        "jobs_sha256": sha256_file(registry_path),
        "max_base_count": max_base,
        "model": config["worker_model"],
        "reasoning_effort": config["worker_reasoning_effort"],
        "public_cases_sha256": sha256_file(iteration_dir(number) / "panel" / "public_cases.json"),
        "sealed_answers_sha256": sha256_file(iteration_dir(number) / "panel" / "sealed_answers.json"),
        "accuracy_withheld": True,
    })
    return jobs, normalizers


def result_documents(jobs: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any] | None]]:
    grouped: dict[str, list[tuple[int, dict[str, Any] | None]]] = defaultdict(list)
    for job in jobs:
        result = results[job["job_id"]]
        grouped[job["case_id"]].append((job["bank_index"], result.get("document")))
    return {
        case_id: [document for _, document in sorted(values)]
        for case_id, values in grouped.items()
    }


def plurality(documents: Iterable[dict[str, Any] | None]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    valid = [item for item in documents if item is not None]
    if not valid:
        return None, {"valid": 0, "unique": 0, "top_count": 0, "tie": False}
    counts = Counter(canonical(item) for item in valid)
    top_count = max(counts.values())
    top_keys = {key for key, count in counts.items() if count == top_count}
    selected = next(item for item in valid if canonical(item) in top_keys)
    return selected, {
        "valid": len(valid),
        "unique": len(counts),
        "top_count": top_count,
        "tie": len(top_keys) > 1,
        "counts": dict(counts),
    }


def plurality_prefer(
    documents: Iterable[dict[str, Any] | None],
    preferred: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    selected, metrics = plurality(documents)
    if preferred is None or selected is None:
        return selected, metrics
    preferred_count = metrics.get("counts", {}).get(canonical(preferred), 0)
    if preferred_count == metrics["top_count"]:
        return preferred, metrics
    return selected, metrics


def committee_delegates(documents: list[dict[str, Any] | None]) -> list[dict[str, Any] | None]:
    return [plurality(documents[start : start + 3])[0] for start in range(0, 9, 3)]


def review_needed(
    strategy: dict[str, Any],
    metrics: dict[str, Any],
    documents: list[dict[str, Any] | None],
) -> bool:
    if strategy["review_count"] == 0 or metrics["valid"] == 0:
        return False
    trigger = strategy["review_trigger"]
    if trigger == "always":
        return True
    if trigger == "committee_disagreement":
        delegates = [item for item in committee_delegates(documents) if item is not None]
        return len(delegates) >= 2 and len({canonical(item) for item in delegates}) > 1
    if metrics["unique"] <= 1:
        return False
    if trigger == "any_disagreement":
        return True
    if trigger == "no_strict_majority":
        return metrics["top_count"] <= metrics["valid"] / 2
    return False


def candidates_for(
    strategy: dict[str, Any],
    base_documents: list[dict[str, Any] | None],
    seed: int,
) -> list[dict[str, Any]]:
    selected = base_documents[: strategy["base_count"]]
    if strategy.get("candidate_source", "base_unique") == "committee_delegates":
        valid = [item for item in committee_delegates(selected) if item is not None]
    else:
        valid = [item for item in selected if item is not None]
    counts = Counter(canonical(item) for item in valid)
    first_seen: dict[str, int] = {}
    values: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(valid):
        key = canonical(item)
        first_seen.setdefault(key, index)
        values[key] = item
    ordered = sorted(values, key=lambda key: (-counts[key], first_seen[key]))[: strategy["candidate_limit"]]
    rng = random.Random(seed)
    rng.shuffle(ordered)
    return [
        {"document": values[key], "frequency": counts[key]}
        for key in ordered
    ]


def review_prompt(
    case: dict[str, Any],
    strategy: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> str:
    style = {
        "verify": "Check every candidate against the exact rules and calculations.",
        "rederive": "Re-solve the problem independently before comparing your result with the candidates.",
        "falsify": "Actively try to falsify each candidate, then keep or repair only what survives.",
        "compare": "Compare the candidates step by step and identify the one with the strongest exact support.",
    }[strategy["review_style"]]
    entries: list[str] = []
    for index, candidate in enumerate(candidates):
        suffix = f" (appeared {candidate['frequency']} times)" if strategy["show_frequencies"] else ""
        entries.append(f"Candidate {index}{suffix}:\n{json.dumps(candidate['document'], sort_keys=True)}")
    finish = (
        "Return only JSON with the zero-based candidate index: {\"choice\": <integer>}."
        if strategy["review_mode"] == "choose"
        else "Return only the exact requested answer JSON. You may return a new answer if every candidate is wrong."
    )
    return (
        "You are an independent reviewer in a controlled reasoning study. Work only from the problem and candidate "
        "answers below. Use no tools, code execution, Python, web access, or external files. Candidate order is random, "
        "and any or all candidates may be wrong.\n\n"
        f"<problem>\n{case['prompt'].strip()}\n</problem>\n\n"
        f"<review_method>\n{style}\n</review_method>\n\n"
        f"<candidates>\n{'\n\n'.join(entries)}\n</candidates>\n\n{finish}"
    )


def regeneration_prompt(case: dict[str, Any], regenerator_index: int) -> str:
    lenses = {
        1: (
            "Construct the solution independently from first principles. Make every decisive constraint explicit and "
            "verify the exact requested output before answering."
        ),
        2: (
            "Solve independently by hunting for contradictions, boundary failures, and tempting false answers. "
            "Eliminate what cannot satisfy every rule, then verify the survivor."
        ),
        3: (
            "Reformulate the problem in an equivalent representation, solve it independently in that form, then "
            "translate back and verify the exact requested output."
        ),
    }
    return (
        "You are a blind independent regenerator in a controlled reasoning study. You receive only the original "
        "problem. You do not see other agents' answers, votes, or confidence. Use no tools, code execution, Python, "
        "web access, or external files.\n\n"
        f"<problem>\n{case['prompt'].strip()}\n</problem>\n\n"
        f"<independent_lens>\n{lenses[regenerator_index]}\n</independent_lens>\n\n"
        "Return only the exact requested JSON object."
    )


def freeze_review_jobs(
    number: int,
    cases: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
    base_documents: dict[str, list[dict[str, Any] | None]],
) -> tuple[list[dict[str, Any]], dict[str, Normalizer]]:
    config = protocol()
    jobs: list[dict[str, Any]] = []
    normalizers: dict[str, Normalizer] = {}
    for strategy_index, strategy in enumerate(strategies):
        if strategy["review_count"] == 0 or strategy["review_mode"] == "cross_examine":
            continue
        for case_index, case in enumerate(cases):
            documents = base_documents[case["case_id"]][: strategy["base_count"]]
            _, metrics = plurality(documents)
            if not review_needed(strategy, metrics, documents):
                continue
            for reviewer_index in range(1, strategy["review_count"] + 1):
                seed = number * 1_000_000 + strategy_index * 10_000 + case_index * 100 + reviewer_index
                if strategy["review_mode"] == "regenerate":
                    candidates: list[dict[str, Any]] = []
                else:
                    candidates = candidates_for(strategy, documents, seed)
                    if not candidates or (strategy["review_mode"] == "choose" and len(candidates) < 2):
                        continue
                job_id = f"i{number:03d}-review-{strategy['strategy_id']}-{case['case_id']}-r{reviewer_index:02d}"
                schema = choice_schema(len(candidates)) if strategy["review_mode"] == "choose" else output_schema(case)
                prompt = (
                    regeneration_prompt(case, reviewer_index)
                    if strategy["review_mode"] == "regenerate"
                    else review_prompt(case, strategy, candidates)
                )
                job = {
                    "job_id": job_id,
                    "iteration": number,
                    "stage": "review",
                    "strategy_id": strategy["strategy_id"],
                    "case_id": case["case_id"],
                    "family": case["family"],
                    "reviewer_index": reviewer_index,
                    "review_mode": strategy["review_mode"],
                    "candidate_documents": [item["document"] for item in candidates],
                    "candidate_frequencies": [item["frequency"] for item in candidates],
                    "candidate_order_seed": seed,
                    "model": config["worker_model"],
                    "reasoning_effort": config["worker_reasoning_effort"],
                    "prompt": prompt,
                    "output_schema": schema,
                }
                jobs.append(job)
                if strategy["review_mode"] == "choose":
                    normalizers[job_id] = lambda value, count=len(candidates): normalize_choice(count, value)
                else:
                    normalizers[job_id] = lambda value, case=case: normalize_answer(case, value)
    random.Random(200_000 + number).shuffle(jobs)
    stage = iteration_dir(number) / "review"
    rendered = "".join(canonical(job) + "\n" for job in jobs)
    registry_path = stage / "jobs.jsonl"
    if registry_path.exists() and registry_path.read_text(encoding="utf-8") != rendered:
        raise LabError("Frozen review registry changed")
    if not registry_path.exists():
        atomic_text(registry_path, rendered)
    freeze_json(stage / "manifest.json", {
        "registered_before_calls": True,
        "registered_at": registered_at(stage / "manifest.json"),
        "jobs": len(jobs),
        "jobs_sha256": sha256_file(registry_path),
        "derived_only_from_public_cases_and_base_outputs": True,
        "sealed_answers_unopened": True,
        "model": config["worker_model"],
        "reasoning_effort": config["worker_reasoning_effort"],
        "base_jobs_sha256": sha256_file(iteration_dir(number) / "base" / "jobs.jsonl"),
    })
    return jobs, normalizers


def cross_exam_prompt(
    case: dict[str, Any],
    candidates: list[dict[str, Any]],
    prior_review: dict[str, Any] | None,
    layer: int,
) -> str:
    entries = [
        f"Candidate {index}:\n{json.dumps(candidate['document'], sort_keys=True)}"
        for index, candidate in enumerate(candidates)
    ]
    if prior_review is None:
        inherited = "No earlier review is available. Establish the strongest initial answer and its decisive critique."
    else:
        inherited = (
            "The preceding reviewer proposed:\n"
            + json.dumps(prior_review["answer_document"], sort_keys=True)
            + "\n\nIts critique was:\n"
            + prior_review["critique"]
        )
    return (
        f"You are reviewer {layer} in a sequential cross-examination study. Work only from the problem, shuffled "
        "candidate answers, and preceding review below. Use no tools, code execution, Python, web access, or external "
        "files. Any candidate and the preceding reviewer may be wrong. Actively falsify the decisive claims in the "
        "preceding critique, re-check the exact rules, and revise the answer when needed.\n\n"
        f"<problem>\n{case['prompt'].strip()}\n</problem>\n\n"
        f"<candidates>\n{'\n\n'.join(entries)}\n</candidates>\n\n"
        f"<preceding_review>\n{inherited}\n</preceding_review>\n\n"
        "Return only the exact requested answer JSON with one additional string field named critique. The critique must "
        "use fewer than 600 characters and state what you tested, what failed or survived, and why the returned exact answer should replace or "
        "retain the preceding proposal."
    )


def freeze_cross_exam_plan(
    number: int,
    cases: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
    base_documents: dict[str, list[dict[str, Any] | None]],
) -> list[dict[str, Any]]:
    config = protocol()
    plans: list[dict[str, Any]] = []
    for strategy_index, strategy in enumerate(strategies):
        if strategy["review_mode"] != "cross_examine":
            continue
        for case_index, case in enumerate(cases):
            documents = base_documents[case["case_id"]][: strategy["base_count"]]
            _, metrics = plurality(documents)
            if not review_needed(strategy, metrics, documents):
                continue
            seed = number * 1_000_000 + strategy_index * 10_000 + case_index * 100 + 77
            candidates = candidates_for(strategy, documents, seed)
            if not candidates:
                continue
            plans.append({
                "strategy_id": strategy["strategy_id"],
                "case_id": case["case_id"],
                "family": case["family"],
                "review_count": strategy["review_count"],
                "candidate_documents": [item["document"] for item in candidates],
                "candidate_frequencies": [item["frequency"] for item in candidates],
                "candidate_order_seed": seed,
            })
    plans.sort(key=lambda item: (item["strategy_id"], item["case_id"]))
    stage = iteration_dir(number) / "cross-review"
    registry_path = stage / "plan.jsonl"
    rendered = "".join(canonical(plan) + "\n" for plan in plans)
    if registry_path.exists() and registry_path.read_text(encoding="utf-8") != rendered:
        raise LabError("Frozen cross-examination plan changed")
    if not registry_path.exists():
        atomic_text(registry_path, rendered)
    freeze_json(stage / "manifest.json", {
        "registered_before_calls": True,
        "registered_at": registered_at(stage / "manifest.json"),
        "chains": len(plans),
        "planned_calls": sum(plan["review_count"] for plan in plans),
        "plan_sha256": sha256_file(registry_path),
        "derived_only_from_public_cases_and_base_outputs": True,
        "sealed_answers_unopened": True,
        "model": config["worker_model"],
        "reasoning_effort": config["worker_reasoning_effort"],
        "base_jobs_sha256": sha256_file(iteration_dir(number) / "base" / "jobs.jsonl"),
    })
    return plans


def run_cross_exam_reviews(
    number: int,
    cases: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
    plans: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not plans:
        return [], {}
    config = protocol()
    cases_by_id = {case["case_id"]: case for case in cases}
    strategies_by_id = {strategy["strategy_id"]: strategy for strategy in strategies}
    prior: dict[tuple[str, str], dict[str, Any] | None] = {}
    all_jobs: list[dict[str, Any]] = []
    all_results: dict[str, dict[str, Any]] = {}
    maximum_layer = max(plan["review_count"] for plan in plans)
    for layer in range(1, maximum_layer + 1):
        jobs: list[dict[str, Any]] = []
        normalizers: dict[str, Normalizer] = {}
        for plan in plans:
            if layer > plan["review_count"]:
                continue
            chain_key = (plan["strategy_id"], plan["case_id"])
            if layer > 1 and prior.get(chain_key) is None:
                continue
            case = cases_by_id[plan["case_id"]]
            strategy = strategies_by_id[plan["strategy_id"]]
            candidates = [
                {"document": document, "frequency": frequency}
                for document, frequency in zip(
                    plan["candidate_documents"], plan["candidate_frequencies"], strict=True
                )
            ]
            previous = prior.get(chain_key)
            job_id = (
                f"i{number:03d}-cross-{plan['strategy_id']}-{plan['case_id']}-r{layer:02d}"
            )
            job = {
                "job_id": job_id,
                "iteration": number,
                "stage": f"cross-review-{layer}",
                "strategy_id": strategy["strategy_id"],
                "case_id": case["case_id"],
                "family": case["family"],
                "reviewer_index": layer,
                "review_mode": "cross_examine",
                "candidate_documents": plan["candidate_documents"],
                "candidate_frequencies": plan["candidate_frequencies"],
                "candidate_order_seed": plan["candidate_order_seed"],
                "prior_review": previous,
                "model": config["worker_model"],
                "reasoning_effort": config["worker_reasoning_effort"],
                "prompt": cross_exam_prompt(case, candidates, previous, layer),
                "output_schema": cross_exam_schema(case),
            }
            jobs.append(job)
            normalizers[job_id] = lambda value, case=case: normalize_cross_exam(case, value)
        random.Random(300_000 + number * 10 + layer).shuffle(jobs)
        layer_stage = iteration_dir(number) / "cross-review" / f"layer-{layer:02d}"
        registry_path = layer_stage / "jobs.jsonl"
        rendered = "".join(canonical(job) + "\n" for job in jobs)
        if registry_path.exists() and registry_path.read_text(encoding="utf-8") != rendered:
            raise LabError(f"Frozen cross-examination layer {layer} changed")
        if not registry_path.exists():
            atomic_text(registry_path, rendered)
        prior_hash = sha256_bytes(canonical({
            f"{key[0]}::{key[1]}": value for key, value in sorted(prior.items())
        }).encode("utf-8"))
        freeze_json(layer_stage / "manifest.json", {
            "registered_before_calls": True,
            "registered_at": registered_at(layer_stage / "manifest.json"),
            "layer": layer,
            "jobs": len(jobs),
            "jobs_sha256": sha256_file(registry_path),
            "prior_layer_outputs_sha256": prior_hash,
            "sealed_answers_unopened": True,
            "model": config["worker_model"],
            "reasoning_effort": config["worker_reasoning_effort"],
        })
        results = run_jobs(
            jobs,
            layer_stage,
            int(config["max_concurrency"]),
            int(config["timeout_seconds"]),
            normalizers,
        )
        for job in jobs:
            prior[(job["strategy_id"], job["case_id"])] = results[job["job_id"]].get("document")
        all_jobs.extend(jobs)
        all_results.update(results)
    return all_jobs, all_results


def exact_score(document: dict[str, Any] | None, expected: dict[str, Any]) -> int:
    return int(document == expected)


def partial_score(
    case: dict[str, Any],
    document: dict[str, Any] | None,
    expected: dict[str, Any],
) -> tuple[int, int]:
    family = case["family"]
    if family == "sequence":
        total = len(expected["answer"])
        if document is None:
            return 0, total
        return sum(left == right for left, right in zip(document["answer"], expected["answer"], strict=True)), total
    if family == "constraint":
        total = len(expected["answer"]) + 1
        if document is None:
            return 0, total
        correct = sum(document["answer"].get(key) == value for key, value in expected["answer"].items())
        correct += int(document.get("total_cost") == expected["total_cost"])
        return correct, total
    total = len(expected["answer"])
    if document is None:
        return 0, total
    return sum(document["answer"].get(key) == value for key, value in expected["answer"].items()), total


def read_stage_jobs(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_stage_results(stage: Path, jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for job in jobs:
        path = stage / "jobs" / job["job_id"] / "result.json"
        if not path.is_file():
            raise LabError(f"Missing terminal result for {job['job_id']}")
        results[job["job_id"]] = load_json(path)
    return results


def review_outputs_for(
    strategy: dict[str, Any],
    case_id: str,
    review_jobs: list[dict[str, Any]],
    review_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any] | None]:
    outputs: list[dict[str, Any] | None] = []
    matching = sorted(
        (
            job for job in review_jobs
            if job["strategy_id"] == strategy["strategy_id"] and job["case_id"] == case_id
        ),
        key=lambda item: item["reviewer_index"],
    )
    for job in matching:
        document = review_results[job["job_id"]].get("document")
        if strategy["review_mode"] == "choose":
            if isinstance(document, dict) and isinstance(document.get("choice"), int):
                choice = document["choice"]
                candidates = job["candidate_documents"]
                outputs.append(candidates[choice] if 0 <= choice < len(candidates) else None)
            else:
                outputs.append(None)
        elif strategy["review_mode"] == "cross_examine":
            if isinstance(document, dict) and isinstance(document.get("answer_document"), dict):
                outputs.append(document["answer_document"])
            else:
                outputs.append(None)
        else:
            outputs.append(document)
    return outputs


def terminal_cross_answer_for(
    strategy: dict[str, Any],
    case_id: str,
    review_jobs: list[dict[str, Any]],
    review_results: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    terminal = next((
        job for job in review_jobs
        if job["strategy_id"] == strategy["strategy_id"]
        and job["case_id"] == case_id
        and job["reviewer_index"] == strategy["review_count"]
    ), None)
    if terminal is None:
        return None
    document = review_results[terminal["job_id"]].get("document")
    if isinstance(document, dict) and isinstance(document.get("answer_document"), dict):
        return document["answer_document"]
    return None


def final_answer_for(
    strategy: dict[str, Any],
    case_id: str,
    base_documents: dict[str, list[dict[str, Any] | None]],
    review_jobs: list[dict[str, Any]],
    review_results: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    base = base_documents[case_id][: strategy["base_count"]]
    base_answer, base_metrics = plurality(base)
    reviewed = review_outputs_for(strategy, case_id, review_jobs, review_results)
    review_answer, review_metrics = plurality(reviewed)
    if not reviewed or review_answer is None:
        final = base_answer
    elif strategy["final_rule"] == "last_review_fallback_base":
        final = terminal_cross_answer_for(
            strategy, case_id, review_jobs, review_results
        ) or base_answer
    elif strategy["final_rule"] == "review_plus_base_plurality":
        final, _ = plurality(reviewed + [base_answer])
    elif strategy["final_rule"] == "augmented_plurality":
        final, _ = plurality_prefer(base + reviewed, base_answer)
    else:
        final = review_answer
    return final, {
        "base_answer": base_answer,
        "base_metrics": base_metrics,
        "review_answer": review_answer,
        "review_metrics": review_metrics,
        "review_calls": len(reviewed),
        "effective_calls": strategy["base_count"] + len(reviewed),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "iteration", "strategy_id", "strategy_name", "case_id", "family", "exact",
        "partial_correct", "partial_total", "base_exact", "base_oracle", "review_calls",
        "review_candidate_oracle", "expanded_oracle", "new_correct_generated",
        "repair_created_correct_answer", "effective_calls",
        "base_unique_candidates", "review_unique_outputs", "helpful_intervention",
        "harmful_intervention", "final_answer",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: row.get(key, "") for key in fields} for row in rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def rank_key(summary: dict[str, Any]) -> tuple[Any, ...]:
    return (
        -summary["worst_family_accuracy"],
        -summary["accuracy"],
        -summary["partial_accuracy"],
        summary["mean_effective_calls"],
        summary["strategy_id"],
    )


def score_iteration(
    number: int,
    cases: list[dict[str, Any]],
    strategies: list[dict[str, Any]],
    base_jobs: list[dict[str, Any]],
    base_results: dict[str, dict[str, Any]],
    review_jobs: list[dict[str, Any]],
    review_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    verify_iteration(number)
    panel = iteration_dir(number) / "panel"
    registry = load_json(iteration_dir(number) / "registry.json")
    if sha256_file(panel / "sealed_answers.json") != registry["sealed_answers_sha256"]:
        raise LabError("Sealed answers changed during calls")
    expected = load_json(panel / "sealed_answers.json")["answers"]
    by_id = {case["case_id"]: case for case in cases}
    if set(expected) != set(by_id):
        raise LabError("Public case IDs and sealed answer IDs differ")
    base_documents = result_documents(base_jobs, base_results)
    rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for strategy in strategies:
        strategy_rows: list[dict[str, Any]] = []
        for case in cases:
            case_id = case["case_id"]
            final, details = final_answer_for(strategy, case_id, base_documents, review_jobs, review_results)
            answer = expected[case_id]
            exact = exact_score(final, answer)
            partial_correct, partial_total = partial_score(case, final, answer)
            base_exact = exact_score(details["base_answer"], answer)
            candidate_pool = base_documents[case_id][: strategy["base_count"]]
            base_oracle = int(any(item == answer for item in candidate_pool))
            matching_review_jobs = [
                job for job in review_jobs
                if job["strategy_id"] == strategy["strategy_id"] and job["case_id"] == case_id
            ]
            exposed_candidates = (
                matching_review_jobs[0]["candidate_documents"] if matching_review_jobs else []
            )
            review_candidate_oracle = (
                int(any(item == answer for item in exposed_candidates))
                if matching_review_jobs else None
            )
            reviewed_outputs = review_outputs_for(
                strategy, case_id, review_jobs, review_results
            )
            expanded_oracle = int(any(
                item == answer for item in candidate_pool + reviewed_outputs
            ))
            new_correct_generated = int(
                strategy["review_mode"] == "regenerate"
                and not base_oracle
                and any(item == answer for item in reviewed_outputs)
            )
            repair_created_correct_answer = int(
                bool(matching_review_jobs)
                and exact == 1
                and (
                    (
                        strategy["review_mode"] in {"repair", "cross_examine"}
                        and review_candidate_oracle == 0
                    )
                    or (
                        strategy["review_mode"] == "regenerate"
                        and base_oracle == 0
                    )
                )
            )
            row = {
                "iteration": number,
                "strategy_id": strategy["strategy_id"],
                "strategy_name": strategy["name"],
                "case_id": case_id,
                "family": case["family"],
                "exact": exact,
                "partial_correct": partial_correct,
                "partial_total": partial_total,
                "base_exact": base_exact,
                "base_oracle": base_oracle,
                "review_calls": details["review_calls"],
                "review_candidate_oracle": review_candidate_oracle,
                "expanded_oracle": expanded_oracle,
                "new_correct_generated": new_correct_generated,
                "repair_created_correct_answer": repair_created_correct_answer,
                "effective_calls": details["effective_calls"],
                "base_unique_candidates": details["base_metrics"]["unique"],
                "review_unique_outputs": details["review_metrics"]["unique"],
                "helpful_intervention": int(not base_exact and exact),
                "harmful_intervention": int(base_exact and not exact),
                "final_answer": canonical(final) if final is not None else "null",
                "base_metrics": details["base_metrics"],
                "review_metrics": details["review_metrics"],
            }
            rows.append(row)
            strategy_rows.append(row)
        families: dict[str, Any] = {}
        for family in ("sequence", "constraint", "logic"):
            subset = [row for row in strategy_rows if row["family"] == family]
            families[family] = {
                "correct": sum(row["exact"] for row in subset),
                "total": len(subset),
                "accuracy": sum(row["exact"] for row in subset) / len(subset),
                "base_correct": sum(row["base_exact"] for row in subset),
                "base_accuracy": sum(row["base_exact"] for row in subset) / len(subset),
                "base_oracle_correct": sum(row["base_oracle"] for row in subset),
                "expanded_oracle_correct": sum(row["expanded_oracle"] for row in subset),
                "new_correct_generated": sum(row["new_correct_generated"] for row in subset),
                "review_activated_cases": sum(row["review_calls"] > 0 for row in subset),
                "helpful_interventions": sum(row["helpful_intervention"] for row in subset),
                "harmful_interventions": sum(row["harmful_intervention"] for row in subset),
                "repair_created_correct_answers": sum(row["repair_created_correct_answer"] for row in subset),
                "mean_unique_base_candidates": sum(row["base_unique_candidates"] for row in subset) / len(subset),
                "harm_rate_given_base_correct": (
                    sum(row["harmful_intervention"] for row in subset)
                    / sum(row["base_exact"] for row in subset)
                    if sum(row["base_exact"] for row in subset) else None
                ),
                "help_rate_given_base_wrong": (
                    sum(row["helpful_intervention"] for row in subset)
                    / (len(subset) - sum(row["base_exact"] for row in subset))
                    if len(subset) > sum(row["base_exact"] for row in subset) else None
                ),
            }
        correct = sum(row["exact"] for row in strategy_rows)
        partial_correct = sum(row["partial_correct"] for row in strategy_rows)
        partial_total = sum(row["partial_total"] for row in strategy_rows)
        summaries.append({
            **strategy,
            "correct": correct,
            "total": len(strategy_rows),
            "accuracy": correct / len(strategy_rows),
            "partial_accuracy": partial_correct / partial_total if partial_total else 0.0,
            "worst_family_accuracy": min(item["accuracy"] for item in families.values()),
            "families": families,
            "base_oracle_correct": sum(row["base_oracle"] for row in strategy_rows),
            "expanded_oracle_correct": sum(row["expanded_oracle"] for row in strategy_rows),
            "new_correct_generated": sum(row["new_correct_generated"] for row in strategy_rows),
            "helpful_interventions": sum(row["helpful_intervention"] for row in strategy_rows),
            "harmful_interventions": sum(row["harmful_intervention"] for row in strategy_rows),
            "review_calls_used": sum(row["review_calls"] for row in strategy_rows),
            "mean_effective_calls": sum(row["effective_calls"] for row in strategy_rows) / len(strategy_rows),
            "mean_unique_base_candidates": sum(row["base_unique_candidates"] for row in strategy_rows) / len(strategy_rows),
            "harm_rate_given_base_correct": (
                sum(row["harmful_intervention"] for row in strategy_rows)
                / sum(row["base_exact"] for row in strategy_rows)
                if sum(row["base_exact"] for row in strategy_rows) else None
            ),
            "help_rate_given_base_wrong": (
                sum(row["helpful_intervention"] for row in strategy_rows)
                / (len(strategy_rows) - sum(row["base_exact"] for row in strategy_rows))
                if len(strategy_rows) > sum(row["base_exact"] for row in strategy_rows) else None
            ),
        })
    summaries.sort(key=rank_key)
    status_counts = Counter(result["outcome"] for result in list(base_results.values()) + list(review_results.values()))
    attempts = sum(result["attempt_count"] for result in list(base_results.values()) + list(review_results.values()))
    summary = {
        "experiment_id": protocol()["experiment_id"],
        "iteration": number,
        "scored_at": utc_now(),
        "checkpoint": bool(panel_spec(number)["checkpoint"]),
        "cases": len(cases),
        "worker_calls": len(base_jobs) + len(review_jobs),
        "base_calls": len(base_jobs),
        "review_calls": len(review_jobs),
        "transport_attempts": attempts,
        "status_counts": dict(status_counts),
        "public_cases_sha256": registry["public_cases_sha256"],
        "sealed_answers_sha256": registry["sealed_answers_sha256"],
        "winner": summaries[0],
        "strategies": summaries,
    }
    output = iteration_dir(number) / "results"
    atomic_json(output / "summary.json", summary)
    atomic_json(output / "case_results.json", rows)
    write_csv(output / "case_results.csv", rows)
    return summary


def svg_text(x: float, y: float, value: str, size: int, color: str, anchor: str = "start", weight: int = 400) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{color}" font-size="{size}" '
        f'font-family="Inter, ui-sans-serif, system-ui" text-anchor="{anchor}" font-weight="{weight}">'
        f"{html.escape(value)}</text>"
    )


def render_iteration_plot(summary: dict[str, Any], path: Path) -> None:
    strategies = summary["strategies"]
    width = 1280
    left = 390
    chart_width = 760
    row_height = 82
    height = 185 + row_height * len(strategies)
    colors = {"sequence": "#39dcff", "constraint": "#ffc857", "logic": "#c477ff"}
    body: list[str] = [
        svg_text(58, 56, f"Iteration {summary['iteration']}: orchestration accuracy", 30, "#ffffff", weight=750),
        svg_text(58, 88, f"{summary['cases']} fresh sealed problems · {summary['worker_calls']} Luna Light calls", 16, "#9fb0d0"),
    ]
    for tick in range(0, 101, 10):
        x = left + chart_width * tick / 100
        body.append(f'<line x1="{x}" y1="126" x2="{x}" y2="{height - 45}" stroke="#25304a" stroke-width="1"/>')
        body.append(svg_text(x, 118, f"{tick}%", 12, "#7181a6", "middle"))
    for index, item in enumerate(strategies):
        y = 153 + index * row_height
        body.append(svg_text(left - 24, y + 7, item["name"], 15, "#ffffff", "end", 650))
        body.append(svg_text(left - 24, y + 28, f"{item['mean_effective_calls']:.1f} calls/problem", 12, "#8493b5", "end"))
        bar_y = y - 10
        for family in ("sequence", "constraint", "logic"):
            accuracy = item["families"][family]["accuracy"]
            body.append(f'<rect x="{left}" y="{bar_y}" width="{chart_width * accuracy:.1f}" height="13" rx="6" fill="{colors[family]}" opacity="0.88"/>')
            bar_y += 18
        overall_x = left + chart_width * item["accuracy"]
        body.append(f'<line x1="{overall_x:.1f}" y1="{y - 15}" x2="{overall_x:.1f}" y2="{y + 43}" stroke="#ffffff" stroke-width="3"/>')
        body.append(svg_text(left + chart_width + 18, y + 9, f"{100 * item['accuracy']:.1f}%", 20, "#ffffff", weight=750))
        net = item["helpful_interventions"] - item["harmful_interventions"]
        if item["review_count"]:
            body.append(svg_text(left + chart_width + 18, y + 31, f"review net {net:+d}", 12, "#9fb0d0"))
    body.append(svg_text(58, height - 20, "Cyan sequence   Gold planning   Violet logic   White marker pooled exact", 13, "#9fb0d0"))
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#080d1a"/>'
        f'<rect x="20" y="20" width="1240" height="{height - 40}" rx="26" fill="#0d1426" stroke="#273555" stroke-width="2"/>'
        + "".join(body) + "</svg>"
    )
    atomic_text(path, svg)


def render_history_plot(history: list[dict[str, Any]], path: Path) -> None:
    width, height = 1280, 520
    left, right, top, bottom = 100, 70, 110, 80
    chart_width, chart_height = width - left - right, height - top - bottom
    body: list[str] = [
        svg_text(54, 55, "Continuous orchestration search", 30, "#ffffff", weight=750),
        svg_text(54, 85, "Fresh-panel outcomes; difficulty varies, so compare pooled mechanisms", 16, "#9fb0d0"),
    ]
    for tick in range(0, 101, 10):
        y = top + chart_height * (1 - tick / 100)
        body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#26324c"/>')
        body.append(svg_text(left - 14, y + 4, f"{tick}%", 12, "#7181a6", "end"))
    if history:
        x_for = lambda index: left + (chart_width * index / max(1, len(history) - 1))
        series = [
            ("winner_accuracy", "#72f1b8", "Panel winner"),
            ("direct_accuracy", "#ff6b9d", "Direct one"),
            ("plurality5_accuracy", "#ffc857", "Five-vote plurality"),
        ]
        for key, color, label in series:
            points = []
            for index, record in enumerate(history):
                value = record.get(key)
                if isinstance(value, (int, float)):
                    points.append((x_for(index), top + chart_height * (1 - value)))
            if len(points) >= 2:
                coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
                body.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="4" stroke-linejoin="round"/>')
            for x, y in points:
                body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{color}"/>')
        for index, record in enumerate(history):
            x = x_for(index)
            body.append(svg_text(x, height - bottom + 28, f"I{record['iteration']}", 12, "#9fb0d0", "middle"))
        for index, (_, color, label) in enumerate(series):
            x = 350 + index * 270
            body.append(f'<circle cx="{x}" cy="{height - 28}" r="6" fill="{color}"/>')
            body.append(svg_text(x + 14, height - 23, label, 13, "#dce7ff"))
    else:
        body.append(svg_text(width / 2, height / 2, "No completed iteration yet", 22, "#9fb0d0", "middle"))
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#080d1a"/>'
        '<rect x="20" y="20" width="1240" height="480" rx="26" fill="#0d1426" stroke="#273555" stroke-width="2"/>'
        + "".join(body) + "</svg>"
    )
    atomic_text(path, svg)


def compact_history() -> list[dict[str, Any]]:
    if not STATE_PATH.is_file():
        return []
    state = load_json(STATE_PATH)
    history: list[dict[str, Any]] = []
    for number in state.get("completed_iterations", []):
        path = iteration_dir(number) / "results" / "summary.json"
        if not path.is_file():
            continue
        summary = load_json(path)
        lookup = {item["strategy_id"]: item for item in summary["strategies"]}
        history.append({
            "iteration": number,
            "cases": summary["cases"],
            "worker_calls": summary["worker_calls"],
            "winner_id": summary["winner"]["strategy_id"],
            "winner_name": summary["winner"]["name"],
            "winner_accuracy": summary["winner"]["accuracy"],
            "winner_worst_family": summary["winner"]["worst_family_accuracy"],
            "direct_accuracy": lookup.get("direct-1", {}).get("accuracy"),
            "plurality5_accuracy": lookup.get("plurality-5", {}).get("accuracy"),
        })
    return history


def operational_signature(strategy: dict[str, Any]) -> str:
    return canonical({
        key: strategy.get(key, "base_unique" if key == "candidate_source" else None)
        for key in OPERATIONAL_FIELDS
    })


def pooled_champion(extra_summary: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return the best replicated non-control mechanism, including a just-scored panel."""
    if not STATE_PATH.is_file():
        return None
    summaries: list[dict[str, Any]] = []
    seen_iterations: set[int] = set()
    for number in load_json(STATE_PATH).get("completed_iterations", []):
        path = iteration_dir(number) / "results" / "summary.json"
        if not path.is_file():
            continue
        summaries.append(load_json(path))
        seen_iterations.add(number)
    if extra_summary is not None:
        extra_number = int(extra_summary["iteration"])
        if extra_number not in seen_iterations:
            summaries.append(extra_summary)
    groups: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        for strategy in summary["strategies"]:
            if strategy["review_count"] == 0:
                continue
            key = operational_signature(strategy)
            group = groups.setdefault(key, {
                "panels": 0,
                "correct": 0,
                "total": 0,
                "partial_weight": 0.0,
                "call_weight": 0.0,
                "families": defaultdict(lambda: [0, 0]),
                "strategy": strategy,
            })
            group["panels"] += 1
            group["correct"] += strategy["correct"]
            group["total"] += strategy["total"]
            group["partial_weight"] += strategy["partial_accuracy"] * strategy["total"]
            group["call_weight"] += strategy["mean_effective_calls"] * strategy["total"]
            group["strategy"] = strategy
            for family, metrics in strategy["families"].items():
                group["families"][family][0] += metrics["correct"]
                group["families"][family][1] += metrics["total"]
    eligible: list[dict[str, Any]] = []
    for group in groups.values():
        if group["panels"] < 2:
            continue
        family_accuracy = {
            family: correct / total
            for family, (correct, total) in group["families"].items()
            if total
        }
        total = group["total"]
        group["accuracy"] = group["correct"] / total
        group["worst_family_accuracy"] = min(family_accuracy.values())
        group["partial_accuracy"] = group["partial_weight"] / total
        group["mean_effective_calls"] = group["call_weight"] / total
        group["family_accuracy"] = family_accuracy
        eligible.append(group)
    if not eligible:
        return None
    eligible.sort(key=lambda group: (
        group["worst_family_accuracy"],
        group["accuracy"],
        group["partial_accuracy"],
        -group["mean_effective_calls"],
    ), reverse=True)
    return eligible[0]


def write_status() -> None:
    if not STATE_PATH.is_file():
        return
    state = load_json(STATE_PATH)
    history = compact_history()
    lines = [
        "# Orchestration Auto-Research Status",
        "",
        f"- Status: **{state['status']}**",
        f"- Completed iterations: **{len(history)}**",
        f"- Next iteration: **{state['next_iteration']}**",
    ]
    best = state.get("best_observed", {})
    if best.get("strategy_id"):
        lines.append(f"- Best single-panel observation: **{best['name']}**, {100 * best['accuracy']:.1f}% exact on iteration {best['iteration']}")
    pooled = pooled_champion()
    if pooled is not None:
        lines.append(
            f"- Pooled replicated champion: **{pooled['strategy']['name']}**, "
            f"{pooled['correct']}/{pooled['total']} exact across {pooled['panels']} panels; "
            f"{100 * pooled['worst_family_accuracy']:.1f}% weakest family"
        )
    lines.extend(("", "| Iteration | Cases | Luna calls | Winner | Exact | Weakest family |", "| ---: | ---: | ---: | --- | ---: | ---: |"))
    for item in history:
        lines.append(
            f"| {item['iteration']} | {item['cases']} | {item['worker_calls']} | {item['winner_name']} | "
            f"{100 * item['winner_accuracy']:.1f}% | {100 * item['winner_worst_family']:.1f}% |"
        )
    lines.extend(("", "![Frontier](plots/frontier.svg)", ""))
    atomic_text(ROOT / "STATUS.md", "\n".join(lines))
    render_history_plot(history, PLOTS / "frontier.svg")


def strategy_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string"},
            "name": {"type": "string"},
            "hypothesis": {"type": "string"},
            "base_count": {"type": "integer", "enum": sorted(ODD_BASE_COUNTS)},
            "review_count": {"type": "integer", "enum": sorted(REVIEW_COUNTS)},
            "review_trigger": {"type": "string", "enum": sorted(REVIEW_TRIGGERS)},
            "review_mode": {"type": "string", "enum": sorted(REVIEW_MODES)},
            "review_style": {"type": "string", "enum": sorted(REVIEW_STYLES)},
            "candidate_limit": {"type": "integer", "minimum": 2, "maximum": 8},
            "candidate_source": {"type": "string", "enum": sorted(CANDIDATE_SOURCES)},
            "show_frequencies": {"type": "boolean"},
            "final_rule": {"type": "string", "enum": sorted(FINAL_RULES)},
        },
        "required": [
            "strategy_id", "name", "hypothesis", "base_count", "review_count",
            "review_trigger", "review_mode", "review_style", "candidate_limit",
            "candidate_source", "show_frequencies", "final_rule",
        ],
        "additionalProperties": False,
    }


def director_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "diagnosis": {"type": "string"},
            "most_important_evidence": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6},
            "research_hypotheses": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6},
            "proposals": {"type": "array", "items": strategy_json_schema(), "minItems": 4, "maxItems": 6},
            "extensions_requested": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "risk_note": {"type": "string"},
        },
        "required": [
            "diagnosis", "most_important_evidence", "research_hypotheses",
            "proposals", "extensions_requested", "risk_note",
        ],
        "additionalProperties": False,
    }


def normalize_director(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    required = {
        "diagnosis", "most_important_evidence", "research_hypotheses",
        "proposals", "extensions_requested", "risk_note",
    }
    if set(value) != required:
        return None
    if not all(isinstance(value[key], str) for key in ("diagnosis", "risk_note")):
        return None
    for key in ("most_important_evidence", "research_hypotheses", "extensions_requested"):
        if not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key]):
            return None
    if not isinstance(value["proposals"], list):
        return None
    for proposal in value["proposals"]:
        _, error = validate_strategy(proposal)
        if error:
            return None
    return value


def director_prompt(number: int, summary: dict[str, Any]) -> str:
    history = compact_history()
    lean_summary = {
        "iteration": number,
        "checkpoint": summary["checkpoint"],
        "cases": summary["cases"],
        "worker_calls": summary["worker_calls"],
        "status_counts": summary["status_counts"],
        "strategies": [
            {
                key: item.get(key, "base_unique" if key == "candidate_source" else None) for key in (
                    "strategy_id", "name", "hypothesis", "base_count", "review_count",
                    "review_trigger", "review_mode", "review_style", "candidate_limit",
                    "candidate_source", "show_frequencies", "final_rule", "correct", "total", "accuracy",
                    "partial_accuracy", "worst_family_accuracy", "families",
                    "base_oracle_correct", "helpful_interventions", "harmful_interventions",
                    "expanded_oracle_correct", "new_correct_generated",
                    "review_calls_used", "mean_effective_calls", "mean_unique_base_candidates",
                    "harm_rate_given_base_correct", "help_rate_given_base_wrong",
                )
            }
            for item in summary["strategies"]
        ],
    }
    next_cases = panel_spec(number + 1)["total_cases"]
    max_calls_key = "checkpoint_max_luna_calls" if next_cases > 12 else "ordinary_max_luna_calls"
    budget = protocol()["budgets"][max_calls_key]
    return (
        RESEARCHER_PATH.read_text(encoding="utf-8").strip()
        + "\n\n## Completed evidence\n\n"
        + json.dumps({"current": lean_summary, "history": history}, indent=2, sort_keys=True)
        + "\n\n## Design the next batch\n\n"
        + f"Iteration {number + 1} will use {next_cases} entirely fresh sealed cases and has a hard maximum of {budget} Luna calls. "
        + "Return 4 to 6 valid strategy proposals. The runner will mechanically retain Direct One, Five-Vote Plurality, "
        + "the pooled champion, and the current winner when distinct, then fill the remaining slots with your most distinct valid proposals. The maximum bank "
        + "cost is cases times the largest base_count. Each review strategy adds at most cases times review_count. Keep the "
        + "complete six-strategy batch within budget. Strategy IDs must contain only lowercase letters, digits, and hyphens. "
        + "Every proposal must explicitly set candidate_source to base_unique or committee_delegates. "
        + "A zero-review strategy must use review_mode none, trigger never, and base_plurality. An independent review strategy "
        + "must use review_plurality_fallback_base or review_plus_base_plurality. Cross-examination requires exactly three "
        + "reviewers, raw base candidates, and last_review_fallback_base. Blind regeneration requires exactly three "
        + "regenerators, review_style rederive, raw base candidates, candidate_limit equal to base_count, hidden frequencies, "
        + "and augmented_plurality. Base your diagnosis on the numbers, "
        + "especially weakest-family accuracy, candidate oracle gaps, and helpful versus harmful interventions."
    )


def run_single_registered_job(
    job: dict[str, Any],
    directory: Path,
    timeout_seconds: int,
    normalizer: Normalizer,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    request_path = directory / "request.json"
    freeze_json(request_path, {
        key: value for key, value in job.items() if key not in {"prompt", "output_schema"}
    } | {
        "registered_before_call": True,
        "registered_at": registered_at(request_path),
        "prompt_sha256": sha256_bytes(job["prompt"].encode("utf-8")),
        "schema_sha256": sha256_bytes(canonical(job["output_schema"]).encode("utf-8")),
    })
    binary = Path(shutil.which("codex") or "codex").resolve()
    if not binary.is_file():
        raise LabError("Codex CLI not found")
    return execute_job(
        binary,
        job,
        directory,
        timeout_seconds,
        int(protocol()["max_infrastructure_attempts"]),
        normalizer,
    )


def run_director(number: int, summary: dict[str, Any]) -> dict[str, Any] | None:
    config = protocol()
    prompt = director_prompt(number, summary)
    job = {
        "job_id": f"i{number:03d}-sol-director",
        "iteration": number,
        "stage": "director",
        "model": config["director_model"],
        "reasoning_effort": config["director_reasoning_effort"],
        "researcher_sha256": sha256_file(RESEARCHER_PATH),
        "prompt": prompt,
        "output_schema": director_schema(),
    }
    directory = iteration_dir(number) / "director"
    atomic_text(directory / "prompt.txt", prompt)
    result = run_single_registered_job(
        job,
        directory,
        int(config["director_timeout_seconds"]),
        normalize_director,
    )
    atomic_json(directory / "result_summary.json", result)
    return result.get("document") if result.get("outcome") == "valid_output" else None


def recover_director(number: int) -> None:
    """Rerun only a failed director stage without touching sealed worker evidence."""
    initialize()
    state = load_json(STATE_PATH)
    if number not in state.get("completed_iterations", []):
        raise LabError(f"Iteration {number} is not complete")
    next_directory = iteration_dir(number + 1)
    if next_directory.exists():
        raise LabError(f"Iteration {number + 1} has already started")
    summary = load_json(iteration_dir(number) / "results" / "summary.json")
    prompt = director_prompt(number, summary)
    recovery_root = iteration_dir(number) / "director-recoveries"
    recovery_index = len([path for path in recovery_root.glob("recovery-*") if path.is_dir()]) + 1
    directory = recovery_root / f"recovery-{recovery_index:02d}"
    config = protocol()
    job = {
        "job_id": f"i{number:03d}-sol-director-recovery-{recovery_index:02d}",
        "iteration": number,
        "stage": "director-recovery",
        "model": config["director_model"],
        "reasoning_effort": config["director_reasoning_effort"],
        "researcher_sha256": sha256_file(RESEARCHER_PATH),
        "prompt": prompt,
        "output_schema": director_schema(),
    }
    atomic_text(directory / "prompt.txt", prompt)
    result = run_single_registered_job(
        job,
        directory,
        int(config["director_timeout_seconds"]),
        normalize_director,
    )
    atomic_json(directory / "result_summary.json", result)
    director = result.get("document") if result.get("outcome") == "valid_output" else None
    if director is None:
        raise LabError(f"Director recovery failed: {result.get('outcome')}")
    next_path = strategy_path(number + 1)
    backup_path = directory / f"superseded-{next_path.name}"
    if next_path.exists():
        next_path.rename(backup_path)
    try:
        assemble_next_strategies(number, summary, director)
    except Exception:
        if backup_path.exists() and not next_path.exists():
            backup_path.rename(next_path)
        raise
    print(f"Director recovery PASS: iteration {number}; rebuilt {next_path.name}")


def fixed_strategy(strategy_id: str) -> dict[str, Any]:
    seed = load_json(strategy_path(1))["strategies"]
    return next(dict(item) for item in seed if item["strategy_id"] == strategy_id)


def assemble_next_strategies(
    number: int,
    summary: dict[str, Any],
    director: dict[str, Any] | None,
) -> dict[str, Any]:
    next_number = number + 1
    target_cases = panel_spec(next_number)["total_cases"]
    selected: list[dict[str, Any]] = []
    reasons: dict[str, str] = {}

    required_fields = {
        "strategy_id", "name", "hypothesis", "base_count", "review_count",
        "review_trigger", "review_mode", "review_style", "candidate_limit",
        "candidate_source", "show_frequencies", "final_rule",
    }

    def add(item: dict[str, Any], reason: str) -> None:
        definition = {key: item[key] for key in required_fields if key in item}
        normalized, error = validate_strategy(definition)
        if error or normalized is None:
            return
        if normalized["strategy_id"] in {entry["strategy_id"] for entry in selected}:
            return
        selected.append(normalized)
        reasons[normalized["strategy_id"]] = reason

    add(fixed_strategy("direct-1"), "fixed direct baseline")
    add(fixed_strategy("plurality-5"), "fixed five-vote baseline")
    pooled = pooled_champion(summary)
    if pooled is not None:
        add(
            pooled["strategy"],
            f"pooled champion across {pooled['panels']} fresh panels: "
            f"{pooled['correct']}/{pooled['total']} exact; "
            f"{100 * pooled['worst_family_accuracy']:.1f}% weakest family",
        )
        add(
            summary["winner"],
            "current panel winner retained unchanged for replication",
        )
    else:
        add(summary["winner"], "current winner retained because no mechanism has two panels yet")
    proposals = director.get("proposals", []) if director else []
    for proposal in proposals:
        if len(selected) >= int(protocol()["budgets"]["max_strategies"]):
            break
        candidate = selected + [proposal]
        try:
            validate_strategy_set({"strategies": candidate}, target_cases)
        except LabError:
            continue
        add(proposal, "Sol research-director proposal")
    for prior in summary["strategies"]:
        if len(selected) >= int(protocol()["budgets"]["max_strategies"]):
            break
        candidate = selected + [prior]
        try:
            validate_strategy_set({"strategies": candidate}, target_cases)
        except LabError:
            continue
        add(prior, "best available prior strategy used as a valid fallback")
    if len(selected) < 3:
        raise LabError("Could not assemble a viable next strategy batch")
    next_path = strategy_path(next_number)
    assembled_at = load_json(next_path).get("assembled_at") if next_path.is_file() else utc_now()
    document = {
        "iteration": next_number,
        "source": f"mechanical anchors plus iteration {number} Sol director",
        "registered_before_calls": True,
        "assembled_at": assembled_at,
        "selection_reasons": reasons,
        "director_available": director is not None,
        "strategies": selected,
    }
    validate_strategy_set(document, target_cases)
    freeze_json(next_path, document)
    return document


def update_state_after_iteration(number: int, summary: dict[str, Any]) -> None:
    state = load_json(STATE_PATH)
    completed = list(state.get("completed_iterations", []))
    if number not in completed:
        completed.append(number)
    best = state.get("best_observed") or null_record()
    winner = summary["winner"]
    if best.get("accuracy") is None or (
        winner["worst_family_accuracy"], winner["accuracy"]
    ) > (
        best["worst_family_accuracy"], best["accuracy"]
    ):
        best = {
            "iteration": number,
            "strategy_id": winner["strategy_id"],
            "name": winner["name"],
            "accuracy": winner["accuracy"],
            "worst_family_accuracy": winner["worst_family_accuracy"],
        }
    state.update({
        "status": "running",
        "updated_at": utc_now(),
        "next_iteration": number + 1,
        "completed_iterations": sorted(completed),
        "current_strategy_file": str(strategy_path(number + 1).relative_to(ROOT)),
        "best_observed": best,
    })
    atomic_json(STATE_PATH, state)
    write_status()


def preflight_normalizer(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {"status", "detail"}:
        return None
    if value["status"] != "ready" or not isinstance(value["detail"], str):
        return None
    return value


def preflight() -> None:
    initialize()
    config = protocol()
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": ["ready"]}, "detail": {"type": "string"}},
        "required": ["status", "detail"],
        "additionalProperties": False,
    }
    directory = ROOT / "preflight"
    checks = [
        ("luna-light", config["worker_model"], config["worker_reasoning_effort"], "Confirm readiness for exact independent reasoning in one short sentence."),
        ("sol-xhigh", config["director_model"], config["director_reasoning_effort"], "Confirm readiness to direct a rigorous orchestration search in one short sentence."),
    ]
    for check_id, model, effort, instruction in checks:
        job = {
            "job_id": f"preflight-{check_id}",
            "stage": "preflight",
            "model": model,
            "reasoning_effort": effort,
            "prompt": instruction + '\nReturn exactly {"status":"ready","detail":"..."}. Use no tools.',
            "output_schema": schema,
        }
        result = run_single_registered_job(job, directory / check_id, int(config["director_timeout_seconds"]), preflight_normalizer)
        if result["outcome"] != "valid_output":
            raise LabError(f"{check_id} preflight failed: {result['outcome']}")
        print(f"Preflight PASS: {check_id} ({model}, {effort})")
    atomic_json(directory / "status.json", {"completed_at": utc_now(), "luna_light": "pass", "sol_xhigh": "pass"})


def run_one(number: int | None = None) -> dict[str, Any]:
    initialize()
    state = load_json(STATE_PATH)
    number = int(number if number is not None else state["next_iteration"])
    if number != int(state["next_iteration"]):
        raise LabError(f"Expected iteration {state['next_iteration']}, not {number}")
    directory, cases, strategies = freeze_iteration(number)
    verify_iteration(number)
    state["status"] = "running"
    state["updated_at"] = utc_now()
    atomic_json(STATE_PATH, state)
    base_jobs, base_normalizers = freeze_base_jobs(number, cases, strategies)
    config = protocol()
    base_results = run_jobs(
        base_jobs,
        directory / "base",
        int(config["max_concurrency"]),
        int(config["timeout_seconds"]),
        base_normalizers,
    )
    base_documents = result_documents(base_jobs, base_results)
    review_jobs, review_normalizers = freeze_review_jobs(number, cases, strategies, base_documents)
    cross_plans = freeze_cross_exam_plan(number, cases, strategies, base_documents)
    planned_cross_calls = sum(plan["review_count"] for plan in cross_plans)
    maximum = len(base_jobs) + sum(len(cases) * item["review_count"] for item in strategies)
    actual = len(base_jobs) + len(review_jobs) + planned_cross_calls
    print(
        f"Iteration {number} review registry frozen: {len(review_jobs)} parallel and up to "
        f"{planned_cross_calls} sequential calls; planned total {actual}, registered maximum {maximum}.",
        flush=True,
    )
    review_results = run_jobs(
        review_jobs,
        directory / "review",
        int(config["max_concurrency"]),
        int(config["timeout_seconds"]),
        review_normalizers,
    )
    cross_jobs, cross_results = run_cross_exam_reviews(number, cases, strategies, cross_plans)
    review_jobs.extend(cross_jobs)
    review_results.update(cross_results)
    summary = score_iteration(number, cases, strategies, base_jobs, base_results, review_jobs, review_results)
    render_iteration_plot(summary, directory / "results" / "plot.svg")
    print(
        f"Iteration {number} scored: {summary['winner']['name']} won at "
        f"{100 * summary['winner']['accuracy']:.1f}% exact; weakest family "
        f"{100 * summary['winner']['worst_family_accuracy']:.1f}%.",
        flush=True,
    )
    director = run_director(number, summary)
    assemble_next_strategies(number, summary, director)
    update_state_after_iteration(number, summary)
    return summary


def loop(max_iterations: int | None = None) -> None:
    initialize()
    completed_here = 0
    while True:
        if STOP_REQUESTED or STOP_PATH.exists():
            state = load_json(STATE_PATH)
            state["status"] = "stopped"
            state["updated_at"] = utc_now()
            atomic_json(STATE_PATH, state)
            write_status()
            print("Clean stop acknowledged before the next iteration.")
            return
        if max_iterations is not None and completed_here >= max_iterations:
            print(f"Requested local limit reached after {completed_here} iteration(s).")
            return
        run_one()
        completed_here += 1


def show_status() -> None:
    initialize()
    write_status()
    state = load_json(STATE_PATH)
    print(json.dumps({
        "status": state["status"],
        "next_iteration": state["next_iteration"],
        "completed_iterations": state["completed_iterations"],
        "best_observed": state["best_observed"],
        "stop_file_present": STOP_PATH.exists(),
    }, indent=2))


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subparsers = value.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("preflight")
    one = subparsers.add_parser("one")
    one.add_argument("--iteration", type=int)
    continuous = subparsers.add_parser("loop")
    continuous.add_argument("--max-iterations", type=int)
    recovery = subparsers.add_parser("recover-director")
    recovery.add_argument("--iteration", type=int, required=True)
    subparsers.add_parser("status")
    return value


def main(argv: Iterable[str] | None = None) -> int:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            initialize()
        elif args.command == "preflight":
            preflight()
        elif args.command == "one":
            run_one(args.iteration)
        elif args.command == "loop":
            loop(args.max_iterations)
        elif args.command == "recover-director":
            recover_director(args.iteration)
        elif args.command == "status":
            show_status()
    except LabError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
