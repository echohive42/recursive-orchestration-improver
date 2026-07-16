#!/usr/bin/env python3
"""Generate disposable, exact, high-difficulty calibration panels."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import random
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


MACHINES = ("W", "X", "Y", "Z")


def matrix_rank(rows: list[list[int]]) -> int:
    matrix = [[Fraction(value) for value in row] for row in rows]
    rank = 0
    columns = len(matrix[0]) if matrix else 0
    for column in range(columns):
        pivot = next((index for index in range(rank, len(matrix)) if matrix[index][column]), None)
        if pivot is None:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        scale = matrix[rank][column]
        matrix[rank] = [value / scale for value in matrix[rank]]
        for index, row in enumerate(matrix):
            if index == rank or not row[column]:
                continue
            factor = row[column]
            matrix[index] = [value - factor * base for value, base in zip(row, matrix[rank], strict=True)]
        rank += 1
    return rank


def bounded(values: list[int], limit: int = 200_000_000) -> bool:
    return max(abs(value) for value in values) <= limit


def sequence_order_four(rng: random.Random, visible_count: int, future_count: int) -> tuple[str, list[int], list[int]]:
    for _ in range(10_000):
        initial = [rng.randint(-8, 8) for _ in range(4)]
        coefficients = [rng.randint(-2, 2) for _ in range(4)]
        forcing = [rng.randint(-4, 4) for _ in range(3)]
        if sum(value != 0 for value in coefficients) < 3 or forcing[0] == 0:
            continue
        values = initial[:]
        for n in range(5, visible_count + future_count + 1):
            recurrence = sum(coefficients[index] * values[-index - 1] for index in range(4))
            values.append(recurrence + forcing[0] * n * n + forcing[1] * n + forcing[2])
        rows = [
            [values[n - 2], values[n - 3], values[n - 4], values[n - 5], n * n, n, 1]
            for n in range(5, visible_count + 1)
        ]
        if bounded(values) and matrix_rank(rows) == 7:
            prompt = (
                "For n >= 5, this integer sequence obeys exactly one recurrence of the form "
                "a[n] = p1*a[n-1] + p2*a[n-2] + p3*a[n-3] + p4*a[n-4] + u*n^2 + v*n + w. "
                "Each p coefficient is an integer from -2 through 2, and u, v, w are integers "
                "from -4 through 4. Indexing begins at n=1. Infer the unique coefficients, verify "
                f"them against every visible term, and return the next {future_count} terms.\n\n"
                f"Sequence: {', '.join(str(value) for value in values[:visible_count])}"
            )
            return prompt, values[:visible_count], values[visible_count:]
    raise RuntimeError("Could not generate an order-four sequence")


def sequence_periodic(rng: random.Random, visible_count: int, future_count: int) -> tuple[str, list[int], list[int]]:
    for _ in range(10_000):
        initial = rng.randint(-8, 8)
        periodic = [rng.choice([-2, -1, 1, 2]) for _ in range(3)]
        forcing = [rng.randint(-3, 3) for _ in range(3)]
        if forcing[0] == 0:
            continue
        values = [initial]
        for n in range(2, visible_count + future_count + 1):
            values.append(periodic[n % 3] * values[-1] + forcing[0] * n * n + forcing[1] * n + forcing[2])
        rows: list[list[int]] = []
        for n in range(2, visible_count + 1):
            periodic_columns = [0, 0, 0]
            periodic_columns[n % 3] = values[n - 2]
            rows.append(periodic_columns + [n * n, n, 1])
        if bounded(values) and matrix_rank(rows) == 6:
            prompt = (
                "For n >= 2, this integer sequence obeys exactly one periodic recurrence "
                "a[n] = c[n mod 3]*a[n-1] + u*n^2 + v*n + w. The three coefficients c[0], "
                "c[1], c[2] are nonzero integers from -2 through 2. The coefficients u, v, w "
                "are integers from -3 through 3. Indexing begins at n=1 and `n mod 3` is the "
                f"ordinary remainder 0, 1, or 2. Infer and verify the coefficients, then return the next {future_count} terms.\n\n"
                f"Sequence: {', '.join(str(value) for value in values[:visible_count])}"
            )
            return prompt, values[:visible_count], values[visible_count:]
    raise RuntimeError("Could not generate a periodic sequence")


def sequence_coupled(rng: random.Random, visible_count: int, future_count: int) -> tuple[str, list[int], list[int]]:
    for _ in range(10_000):
        x_values = [rng.randint(-6, 6)]
        y_values = [rng.randint(-6, 6)]
        first = [rng.randint(-2, 2) for _ in range(2)] + [rng.randint(-3, 3) for _ in range(2)]
        second = [rng.randint(-2, 2) for _ in range(2)] + [rng.randint(-3, 3) for _ in range(2)]
        if sum(value != 0 for value in first[:2] + second[:2]) < 3:
            continue
        pair_count = math.ceil((visible_count + future_count) / 2)
        for m in range(1, pair_count):
            x_new = first[0] * x_values[-1] + first[1] * y_values[-1] + first[2] * m + first[3]
            y_new = second[0] * y_values[-1] + second[1] * x_new + second[2] * m + second[3]
            x_values.append(x_new)
            y_values.append(y_new)
        values = [value for pair in zip(x_values, y_values, strict=True) for value in pair]
        visible_pairs = visible_count // 2
        x_rows = [[x_values[m - 1], y_values[m - 1], m, 1] for m in range(1, visible_pairs)]
        y_rows = [[y_values[m - 1], x_values[m], m, 1] for m in range(1, visible_pairs)]
        if bounded(values) and matrix_rank(x_rows) == 4 and matrix_rank(y_rows) == 4:
            prompt = (
                "The displayed sequence interleaves two integer sequences as x[0], y[0], x[1], y[1], and so on. "
                "For m >= 1 they obey exactly one coupled system: "
                "x[m] = p*x[m-1] + q*y[m-1] + u*m + v; "
                "y[m] = r*y[m-1] + s*x[m] + t*m + w. "
                "The coefficients p, q, r, s are integers from -2 through 2, while u, v, t, w "
                f"are integers from -3 through 3. Infer and verify all coefficients, then return the next {future_count} interleaved terms.\n\n"
                f"Sequence: {', '.join(str(value) for value in values[:visible_count])}"
            )
            return prompt, values[:visible_count], values[visible_count:visible_count + future_count]
    raise RuntimeError("Could not generate a coupled sequence")


def sequence_order_five_cubic(rng: random.Random, visible_count: int, future_count: int) -> tuple[str, list[int], list[int]]:
    for _ in range(20_000):
        initial = [rng.randint(-6, 6) for _ in range(5)]
        coefficients = [rng.randint(-2, 2) for _ in range(5)]
        forcing = [rng.randint(-2, 2) for _ in range(4)]
        if sum(value != 0 for value in coefficients) < 4 or forcing[0] == 0:
            continue
        values = initial[:]
        for n in range(6, visible_count + future_count + 1):
            recurrence = sum(coefficients[index] * values[-index - 1] for index in range(5))
            values.append(
                recurrence + forcing[0] * n ** 3 + forcing[1] * n * n + forcing[2] * n + forcing[3]
            )
        rows = [
            [values[n - offset - 1] for offset in range(1, 6)] + [n ** 3, n * n, n, 1]
            for n in range(6, visible_count + 1)
        ]
        if bounded(values) and matrix_rank(rows) == 9:
            prompt = (
                "For n >= 6, this integer sequence obeys exactly one recurrence of the form "
                "a[n] = p1*a[n-1] + p2*a[n-2] + p3*a[n-3] + p4*a[n-4] + p5*a[n-5] "
                "+ u*n^3 + v*n^2 + w*n + z. Each p coefficient is an integer from -2 through 2, "
                "and u, v, w, z are integers from -2 through 2. Indexing begins at n=1. Infer and "
                f"verify the unique coefficients, then return the next {future_count} terms.\n\n"
                f"Sequence: {', '.join(str(value) for value in values[:visible_count])}"
            )
            return prompt, values[:visible_count], values[visible_count:]
    raise RuntimeError("Could not generate an order-five cubic sequence")


def sequence_periodic_second_order(rng: random.Random, visible_count: int, future_count: int) -> tuple[str, list[int], list[int]]:
    for _ in range(20_000):
        values = [rng.randint(-6, 6), rng.randint(-6, 6)]
        first = [rng.choice((-2, -1, 1, 2)) for _ in range(3)]
        second = [rng.choice((-2, -1, 1, 2)) for _ in range(3)]
        forcing = [rng.randint(-2, 2) for _ in range(3)]
        if forcing[0] == 0:
            continue
        for n in range(3, visible_count + future_count + 1):
            remainder = n % 3
            values.append(
                first[remainder] * values[-1] + second[remainder] * values[-2]
                + forcing[0] * n * n + forcing[1] * n + forcing[2]
            )
        rows: list[list[int]] = []
        for n in range(3, visible_count + 1):
            first_columns = [0, 0, 0]
            second_columns = [0, 0, 0]
            first_columns[n % 3] = values[n - 2]
            second_columns[n % 3] = values[n - 3]
            rows.append(first_columns + second_columns + [n * n, n, 1])
        if bounded(values) and matrix_rank(rows) == 9:
            prompt = (
                "For n >= 3, this integer sequence obeys exactly one periodic second-order recurrence: "
                "a[n] = c[n mod 3]*a[n-1] + d[n mod 3]*a[n-2] + u*n^2 + v*n + w. "
                "All six periodic coefficients c[0..2], d[0..2] are nonzero integers from -2 through 2. "
                "The coefficients u, v, w are integers from -2 through 2. Indexing begins at n=1. "
                f"Infer and verify all coefficients, then return the next {future_count} terms.\n\n"
                f"Sequence: {', '.join(str(value) for value in values[:visible_count])}"
            )
            return prompt, values[:visible_count], values[visible_count:]
    raise RuntimeError("Could not generate a periodic second-order sequence")


def sequence_coupled_quadratic(rng: random.Random, visible_count: int, future_count: int) -> tuple[str, list[int], list[int]]:
    for _ in range(20_000):
        x_values = [rng.randint(-5, 5)]
        y_values = [rng.randint(-5, 5)]
        first = [rng.randint(-2, 2) for _ in range(2)] + [rng.randint(-2, 2) for _ in range(3)]
        second = [rng.randint(-2, 2) for _ in range(2)] + [rng.randint(-2, 2) for _ in range(3)]
        if sum(value != 0 for value in first[:2] + second[:2]) < 3 or first[2] == 0 or second[2] == 0:
            continue
        pair_count = math.ceil((visible_count + future_count) / 2)
        for m in range(1, pair_count):
            x_new = (
                first[0] * x_values[-1] + first[1] * y_values[-1]
                + first[2] * m * m + first[3] * m + first[4]
            )
            y_new = (
                second[0] * y_values[-1] + second[1] * x_new
                + second[2] * m * m + second[3] * m + second[4]
            )
            x_values.append(x_new)
            y_values.append(y_new)
        values = [value for pair in zip(x_values, y_values, strict=True) for value in pair]
        visible_pairs = visible_count // 2
        x_rows = [[x_values[m - 1], y_values[m - 1], m * m, m, 1] for m in range(1, visible_pairs)]
        y_rows = [[y_values[m - 1], x_values[m], m * m, m, 1] for m in range(1, visible_pairs)]
        if bounded(values) and matrix_rank(x_rows) == 5 and matrix_rank(y_rows) == 5:
            prompt = (
                "The displayed sequence interleaves x[0], y[0], x[1], y[1], and so on. For m >= 1: "
                "x[m] = p*x[m-1] + q*y[m-1] + a*m^2 + b*m + c; "
                "y[m] = r*y[m-1] + s*x[m] + d*m^2 + e*m + f. "
                "The coefficients p, q, r, s and a through f are integers from -2 through 2. "
                f"Infer and verify the unique coefficients, then return the next {future_count} interleaved terms.\n\n"
                f"Sequence: {', '.join(str(value) for value in values[:visible_count])}"
            )
            return prompt, values[:visible_count], values[visible_count:visible_count + future_count]
    raise RuntimeError("Could not generate a coupled quadratic sequence")


def modular_linear_status(matrix: list[list[int]], rhs: list[int], modulus: int) -> str:
    augmented = [
        [value % modulus for value in row] + [answer % modulus]
        for row, answer in zip(matrix, rhs, strict=True)
    ]
    columns = len(matrix[0])
    rank = 0
    for column in range(columns):
        pivot = next((index for index in range(rank, len(augmented)) if augmented[index][column]), None)
        if pivot is None:
            continue
        augmented[rank], augmented[pivot] = augmented[pivot], augmented[rank]
        inverse = pow(augmented[rank][column], -1, modulus)
        augmented[rank] = [(value * inverse) % modulus for value in augmented[rank]]
        for index, row in enumerate(augmented):
            if index == rank or not row[column]:
                continue
            factor = row[column]
            augmented[index] = [
                (value - factor * base) % modulus
                for value, base in zip(row, augmented[rank], strict=True)
            ]
        rank += 1
    if any(not any(row[:columns]) and row[columns] for row in augmented):
        return "inconsistent"
    return "unique" if rank == columns else "multiple"


def sequence_modular_periodic(
    rng: random.Random, visible_count: int, future_count: int, period: int = 4,
) -> tuple[str, list[int], list[int]]:
    moduli = (97, 101, 103)
    periodic_choices = (-2, -1, 1, 2)
    forcing_choices = tuple(value for value in range(-4, 5) if value != 0)
    for _ in range(200):
        modulus = rng.choice(moduli)
        first = [rng.choice(periodic_choices) for _ in range(period)]
        second = [rng.choice(periodic_choices) for _ in range(period)]
        third = rng.choice(periodic_choices)
        linear = rng.choice(forcing_choices)
        constant = rng.randint(-4, 4)
        values = [rng.randrange(modulus) for _ in range(3)]
        for n in range(4, visible_count + future_count + 1):
            remainder = n % period
            values.append(
                (
                    first[remainder] * values[-1] + second[remainder] * values[-2]
                    + third * values[-3] + linear * n + constant
                )
                % modulus
            )

        statuses: dict[int, str] = {}
        for candidate_modulus in moduli:
            if max(values[:visible_count]) >= candidate_modulus:
                statuses[candidate_modulus] = "inconsistent"
                continue
            matrix: list[list[int]] = []
            rhs: list[int] = []
            for n in range(4, visible_count + 1):
                first_columns = [0] * period
                second_columns = [0] * period
                first_columns[n % period] = values[n - 2]
                second_columns[n % period] = values[n - 3]
                matrix.append(first_columns + second_columns + [values[n - 4], n, 1])
                rhs.append(values[n - 1])
            statuses[candidate_modulus] = modular_linear_status(matrix, rhs, candidate_modulus)
        if statuses.get(modulus) != "unique" or any(
            status != "inconsistent" for candidate, status in statuses.items() if candidate != modulus
        ):
            continue
        prompt = (
            "This sequence is generated by a periodic recurrence over modular arithmetic. For n >= 4: "
            f"a[n] = (p[n mod {period}]*a[n-1] + q[n mod {period}]*a[n-2] + t*a[n-3] + r*n + s) mod M. "
            "The unknown modulus M is exactly one of 97, 101, or 103. "
            f"The {2 * period} periodic coefficients p[0..{period - 1}], q[0..{period - 1}] are each "
            "one of -2, -1, 1, or 2. The global coefficient t is also one of those four values. The coefficient r is a "
            "nonzero integer from -4 through 4, and s is an integer from -4 through 4. Remainders "
            "are reported in the range 0 through M-1. Indexing begins at n=1. Infer and verify the "
            f"unique system, then return the next {future_count} terms.\n\n"
            f"Sequence: {', '.join(str(value) for value in values[:visible_count])}"
        )
        return prompt, values[:visible_count], values[visible_count:]
    raise RuntimeError("Could not generate a unique modular periodic sequence")


def make_sequence_case(rng: random.Random, case_id: str, index: int, tier: int) -> tuple[dict[str, Any], dict[str, Any]]:
    # Tier 4 keeps the same 11-parameter system, but exposes three redundant
    # verification equations. The previous one-equation margin produced too
    # many cases that no call could solve at all.
    visible_count = 17 if tier == 4 else 18
    future_count = {2: 5, 3: 6, 4: 3}[tier]
    if tier == 4:
        prompt, _, future = sequence_modular_periodic(rng, visible_count, future_count, 4)
        return {
            "case_id": case_id,
            "family": "sequence",
            "prompt": prompt,
            "answer_schema": {"answer": ["integer"] * future_count},
        }, {"answer": future}
    builders = (
        (sequence_order_four, sequence_periodic, sequence_coupled)
        if tier == 2 else
        (sequence_order_five_cubic, sequence_periodic_second_order, sequence_coupled_quadratic)
        if tier == 3 else
        ()
    )
    prompt, _, future = builders[index % len(builders)](rng, visible_count, future_count)
    return {
        "case_id": case_id,
        "family": "sequence",
        "prompt": prompt,
        "answer_schema": {"answer": ["integer"] * future_count},
    }, {"answer": future}


def balanced_assignments(job_count: int) -> Iterable[tuple[int, ...]]:
    if job_count != 12:
        raise ValueError("Extreme calibration currently uses exactly 12 jobs")
    indexes = set(range(job_count))
    for w_jobs in itertools.combinations(range(job_count), 3):
        after_w = sorted(indexes - set(w_jobs))
        for x_jobs in itertools.combinations(after_w, 3):
            after_x = sorted(set(after_w) - set(x_jobs))
            for y_jobs in itertools.combinations(after_x, 3):
                assignment = [3] * job_count
                for index in w_jobs:
                    assignment[index] = 0
                for index in x_jobs:
                    assignment[index] = 1
                for index in y_jobs:
                    assignment[index] = 2
                yield tuple(assignment)


def constraint_valid(values: tuple[int, ...], rules: list[dict[str, Any]]) -> bool:
    for rule in rules:
        kind = rule["kind"]
        if kind == "different" and values[rule["a"]] == values[rule["b"]]:
            return False
        if kind == "same" and values[rule["a"]] != values[rule["b"]]:
            return False
        if kind == "allowed" and values[rule["job"]] not in rule["machines"]:
            return False
        if kind == "forbidden" and values[rule["job"]] == rule["machine"]:
            return False
        if kind == "conditional" and values[rule["if_job"]] == rule["if_machine"] and values[rule["then_job"]] == rule["not_machine"]:
            return False
        if kind == "exactly_one" and sum(values[job] == rule["machine"] for job in rule["jobs"]) != 1:
            return False
    return True


def constraint_text(rule: dict[str, Any]) -> str:
    name = lambda index: f"J{index + 1}"
    machine = lambda index: MACHINES[index]
    kind = rule["kind"]
    if kind == "different":
        return f"{name(rule['a'])} and {name(rule['b'])} must use different machines."
    if kind == "same":
        return f"{name(rule['a'])} and {name(rule['b'])} must use the same machine."
    if kind == "allowed":
        return f"{name(rule['job'])} may use only {' or '.join(machine(value) for value in rule['machines'])}."
    if kind == "forbidden":
        return f"{name(rule['job'])} may not use {machine(rule['machine'])}."
    if kind == "conditional":
        return (
            f"If {name(rule['if_job'])} uses {machine(rule['if_machine'])}, then "
            f"{name(rule['then_job'])} may not use {machine(rule['not_machine'])}."
        )
    return (
        f"Exactly one of {name(rule['jobs'][0])}, {name(rule['jobs'][1])}, and "
        f"{name(rule['jobs'][2])} must use {machine(rule['machine'])}."
    )


def make_constraint_case(rng: random.Random, case_id: str, tier: int) -> tuple[dict[str, Any], dict[str, Any]]:
    job_count = 12
    for _ in range(100):
        target = list(range(4)) * 3
        rng.shuffle(target)
        pairs = list(itertools.combinations(range(job_count), 2))
        same_pairs = [pair for pair in pairs if target[pair[0]] == target[pair[1]]]
        different_pairs = [pair for pair in pairs if target[pair[0]] != target[pair[1]]]
        different = rng.sample(different_pairs, 3)
        same = rng.choice(same_pairs)
        allowed_jobs = rng.sample(range(job_count), 2)
        forbidden_jobs = rng.sample([job for job in range(job_count) if job not in allowed_jobs], 2)
        conditional_pool = [job for job in range(job_count) if job not in allowed_jobs + forbidden_jobs]
        # Tier 4 uses a denser interaction graph. Every generated rule must
        # still have an independent nonredundancy witness below.
        conditional_count = 8 if tier == 4 else 2 if tier == 3 else 4
        conditional_jobs = rng.sample(conditional_pool, conditional_count)
        exactly_rules: list[dict[str, Any]] = []
        exact_machines = rng.sample(range(4), 2 if tier == 4 else 1)
        for exact_machine in exact_machines:
            on_machine = [job for job in range(job_count) if target[job] == exact_machine]
            off_machine = [job for job in range(job_count) if target[job] != exact_machine]
            exactly_jobs = [rng.choice(on_machine)] + rng.sample(off_machine, 2)
            rng.shuffle(exactly_jobs)
            exactly_rules.append({"kind": "exactly_one", "jobs": exactly_jobs, "machine": exact_machine})
        rules: list[dict[str, Any]] = [
            *({"kind": "different", "a": left, "b": right} for left, right in different),
            {"kind": "same", "a": same[0], "b": same[1]},
        ]
        for job in allowed_jobs:
            alternate = rng.choice([machine for machine in range(4) if machine != target[job]])
            rules.append({"kind": "allowed", "job": job, "machines": sorted([target[job], alternate])})
        for job in forbidden_jobs:
            rules.append({"kind": "forbidden", "job": job, "machine": rng.choice([machine for machine in range(4) if machine != target[job]])})
        for offset in range(0, conditional_count, 2):
            if_job, then_job = conditional_jobs[offset:offset + 2]
            rules.append({
                "kind": "conditional", "if_job": if_job, "if_machine": target[if_job],
                "then_job": then_job,
                "not_machine": rng.choice([machine for machine in range(4) if machine != target[then_job]]),
            })
        rules.extend(exactly_rules)

        feasible: list[tuple[int, ...]] = []
        nonredundant = [False] * len(rules)
        relaxation_witnesses: list[list[tuple[int, ...]]] = [[] for _ in rules]
        for values in balanced_assignments(job_count):
            satisfied = [constraint_valid(values, [rule]) for rule in rules]
            failures = [index for index, valid in enumerate(satisfied) if not valid]
            if not failures:
                feasible.append(values)
            elif len(failures) == 1:
                failed_rule = failures[0]
                nonredundant[failed_rule] = True
                relaxation_witnesses[failed_rule].append(values)
        if not 40 <= len(feasible) <= 8_000 or not all(nonredundant):
            continue
        for _ in range(40):
            costs = [[rng.randint(1, 35) for _ in range(4)] for _ in range(job_count)]
            ranked = sorted(
                (sum(costs[index][machine] for index, machine in enumerate(values)), values)
                for values in feasible
            )
            best_cost, best_values = ranked[0]
            if tier == 3:
                minimum_gap, maximum_gap, close_window = 3, 9, 12
            elif tier == 4:
                minimum_gap, maximum_gap, close_window = 1, 4, 8
            else:
                minimum_gap, maximum_gap, close_window = 1, 5, 5
            if not minimum_gap <= ranked[1][0] - best_cost <= maximum_gap:
                continue
            required_close = 5 if tier == 4 else 3
            if sum(cost <= best_cost + close_window for cost, _ in ranked) < required_close:
                continue
            non_greedy = sum(
                costs[index][machine] > min(costs[index]) for index, machine in enumerate(best_values)
            )
            required_non_greedy = 6 if tier == 4 else 4
            if non_greedy < required_non_greedy:
                continue
            binding_rules = 0
            for witnesses in relaxation_witnesses:
                if any(
                    sum(costs[index][machine] for index, machine in enumerate(values)) <= best_cost
                    for values in witnesses
                ):
                    binding_rules += 1
            if tier == 4 and binding_rules < 4:
                continue
            rows = ["Job | W | X | Y | Z", "--- | ---: | ---: | ---: | ---:"]
            rows.extend(
                f"J{index + 1} | {row[0]} | {row[1]} | {row[2]} | {row[3]}"
                for index, row in enumerate(costs)
            )
            prompt = (
                "Assign twelve compute jobs to machines W, X, Y, and Z. Each machine must receive "
                "exactly three jobs. Minimize total cost while satisfying every interaction. The optimum "
                "is unique, but several feasible assignments are close in cost. Return the complete "
                "assignment and exact total cost.\n\n" + "\n".join(rows) + "\n\nConstraints:\n- "
                + "\n- ".join(constraint_text(rule) for rule in rules)
            )
            mapping = {f"J{index + 1}": MACHINES[machine] for index, machine in enumerate(best_values)}
            case = {
                "case_id": case_id,
                "family": "constraint",
                "prompt": prompt,
                "answer_schema": {"answer": {f"J{i + 1}": "W|X|Y|Z" for i in range(job_count)}, "total_cost": "integer"},
            }
            return case, {"answer": mapping, "total_cost": best_cost}
    raise RuntimeError(f"Could not generate meaningful constraint case {case_id}")


Formula = dict[str, Any]


def formula_refs(formula: Formula) -> set[int]:
    if formula["kind"] == "atom":
        return {formula["person"]}
    refs: set[int] = set()
    for child in formula["children"]:
        refs.update(formula_refs(child))
    return refs


def eval_formula(formula: Formula, world: tuple[bool, ...]) -> bool:
    kind = formula["kind"]
    if kind == "atom":
        return world[formula["person"]]
    values = [eval_formula(child, world) for child in formula["children"]]
    if kind == "not":
        return not values[0]
    if kind == "and":
        return all(values)
    if kind == "or":
        return any(values)
    if kind == "xor":
        return sum(values) % 2 == 1
    if kind == "iff":
        return values[0] == values[1]
    if kind == "implies":
        return (not values[0]) or values[1]
    if kind == "exactly":
        return sum(values) == formula["count"]
    raise ValueError(kind)


def formula_text(formula: Formula, names: list[str]) -> str:
    kind = formula["kind"]
    if kind == "atom":
        return f"T({names[formula['person']]})"
    children = [formula_text(child, names) for child in formula["children"]]
    if kind == "not":
        return f"NOT({children[0]})"
    if kind == "and":
        return f"AND({children[0]}, {children[1]})"
    if kind == "or":
        return f"OR({children[0]}, {children[1]})"
    if kind == "xor":
        return f"XOR({children[0]}, {children[1]})"
    if kind == "iff":
        return f"IFF({children[0]}, {children[1]})"
    if kind == "implies":
        return f"IMPLIES({children[0]}, {children[1]})"
    return f"EXACTLY({formula['count']}; {'; '.join(children)})"


def random_subformula(rng: random.Random, others: list[int]) -> Formula:
    choice = rng.choice(("atom", "not", "and", "or", "xor", "exactly"))
    if choice == "atom":
        return {"kind": "atom", "person": rng.choice(others)}
    if choice == "not":
        return {"kind": "not", "children": [{"kind": "atom", "person": rng.choice(others)}]}
    if choice == "exactly":
        refs = rng.sample(others, 3)
        return {
            "kind": "exactly", "count": rng.choice((1, 2)),
            "children": [{"kind": "atom", "person": person} for person in refs],
        }
    refs = rng.sample(others, 2)
    return {
        "kind": choice,
        "children": [{"kind": "atom", "person": person} for person in refs],
    }


def random_nested_formula(rng: random.Random, others: list[int], depth: int = 2) -> Formula:
    if depth <= 1:
        return random_subformula(rng, others)
    left = random_nested_formula(rng, others, depth - 1)
    right = random_nested_formula(rng, others, depth - 1)
    return {"kind": rng.choice(("and", "or", "xor", "iff", "implies")), "children": [left, right]}


def strongly_connected(formulas: list[Formula], count: int) -> bool:
    graph = [formula_refs(formula) for formula in formulas]
    for start in range(count):
        seen = {start}
        frontier = [start]
        while frontier:
            node = frontier.pop()
            for neighbor in graph[node]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    frontier.append(neighbor)
        if len(seen) != count:
            return False
    return True


def make_logic_case(rng: random.Random, case_id: str, tier: int) -> tuple[dict[str, Any], dict[str, Any]]:
    count = {2: 14, 3: 16, 4: 17}[tier]
    names = [chr(ord("A") + index) for index in range(count)]
    for _ in range(8_000):
        target = tuple(rng.choice((False, True)) for _ in range(count))
        if not count // 3 <= sum(target) <= count - count // 3:
            continue
        deep_speakers = set(rng.sample(range(count), 11)) if tier == 4 else set()
        formulas: list[Formula] = []
        for speaker in range(count):
            others = [person for person in range(count) if person != speaker]
            formula_depth = 3 if speaker in deep_speakers else 2
            for _ in range(200):
                formula = random_nested_formula(rng, others, formula_depth)
                if eval_formula(formula, target) == target[speaker] and len(formula_refs(formula)) >= 3:
                    formulas.append(formula)
                    break
            else:
                break
        if len(formulas) != count or not strongly_connected(formulas, count):
            continue
        solutions: list[tuple[bool, ...]] = []
        near_solutions = 0
        near_target_solutions = 0
        formula_values = [set() for _ in range(count)]
        for world in itertools.product((False, True), repeat=count):
            evaluations = [eval_formula(formula, world) for formula in formulas]
            for speaker, value in enumerate(evaluations):
                formula_values[speaker].add(value)
            satisfied = sum(world[speaker] == evaluations[speaker] for speaker in range(count))
            if satisfied == count:
                solutions.append(world)
                if len(solutions) > 1:
                    break
            elif satisfied == count - 1:
                near_solutions += 1
                distance = sum(actual != expected for actual, expected in zip(world, target, strict=True))
                if distance <= 4:
                    near_target_solutions += 1
        required_near_solutions = {2: 4, 3: 8, 4: 12}[tier]
        required_near_target = 3 if tier == 4 else 0
        if (
            len(solutions) != 1
            or near_solutions < required_near_solutions
            or near_target_solutions < required_near_target
            or any(len(values) < 2 for values in formula_values)
        ):
            continue
        statements = "\n".join(
            f"- {names[speaker]} says: \"{formula_text(formulas[speaker], names)}.\""
            for speaker in range(count)
        )
        prompt = (
            "Each person is either truthful or a liar. A truthful person's entire nested statement "
            "is true, while a liar's entire statement is false. T(X) means X is truthful. AND, OR, "
            "NOT, XOR, IFF, and IMPLIES have their standard Boolean meanings; OR is inclusive. "
            "EXACTLY(k; ...) is true when exactly k listed expressions are true. Determine the unique "
            "type of every person.\n\n" + statements
        )
        case = {
            "case_id": case_id,
            "family": "logic",
            "prompt": prompt,
            "answer_schema": {"answer": {name: "truthful|liar" for name in names}},
        }
        answer = {"answer": {name: ("truthful" if value else "liar") for name, value in zip(names, solutions[0], strict=True)}}
        return case, answer
    raise RuntimeError(f"Could not generate unique nested logic case {case_id}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", type=int, choices=(2, 3, 4), default=2)
    parser.add_argument("--seed", type=int, default=910_001)
    parser.add_argument("--cases-per-family", type=int, default=8)
    parser.add_argument("--families", default="sequence,constraint,logic")
    parser.add_argument(
        "--phase",
        choices=("disposable-calibration", "frozen-baseline"),
        default="disposable-calibration",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    allowed_families = ("sequence", "constraint", "logic")
    selected_families = [family.strip() for family in args.families.split(",") if family.strip()]
    if not selected_families or any(family not in allowed_families for family in selected_families):
        raise SystemExit(f"Invalid families: {args.families}")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit(f"Refusing to overwrite nonempty frozen panel: {args.output_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    cases: list[dict[str, Any]] = []
    answers: dict[str, Any] = {}
    if "sequence" in selected_families:
        for index in range(args.cases_per_family):
            case, answer = make_sequence_case(rng, f"cal-t{args.tier}-sequence-{index + 1:02d}", index, args.tier)
            cases.append(case)
            answers[case["case_id"]] = answer
    if "constraint" in selected_families:
        for index in range(args.cases_per_family):
            case, answer = make_constraint_case(rng, f"cal-t{args.tier}-constraint-{index + 1:02d}", args.tier)
            cases.append(case)
            answers[case["case_id"]] = answer
    if "logic" in selected_families:
        for index in range(args.cases_per_family):
            case, answer = make_logic_case(rng, f"cal-t{args.tier}-logic-{index + 1:02d}", args.tier)
            cases.append(case)
            answers[case["case_id"]] = answer

    metadata = {
        "benchmark": f"extreme-calibration-t{args.tier}-s{args.seed}",
        "difficulty": f"extreme-tier-{args.tier}",
        "phase": args.phase,
        "generator_version": 16,
        "seed": args.seed,
        "tier": args.tier,
        "cases_per_family": args.cases_per_family,
        "families": selected_families,
        "sequence_visible_terms": 17 if args.tier == 4 else 18,
        "sequence_future_terms": {2: 5, 3: 6, 4: 3}[args.tier],
        "constraint_jobs": 12,
        "logic_people": {2: 14, 3: 16, 4: 17}[args.tier],
        "construction": f"{len(cases)} exact cases across " + ", ".join({
            "sequence": "minimal-evidence recurrence inference",
            "constraint": "12-job constrained optimization",
            "logic": f"{ {2: 14, 3: 16, 4: 17}[args.tier] }-person nested logic",
        }[family] for family in selected_families),
    }
    public = {**metadata, "cases": cases}
    sealed = {"benchmark": metadata["benchmark"], "answers": answers}
    public_path = args.output_dir / "public_cases.json"
    sealed_path = args.output_dir / "sealed_answers.json"
    public_path.write_text(json.dumps(public, indent=2) + "\n")
    sealed_path.write_text(json.dumps(sealed, indent=2) + "\n")
    hashes = {
        "registered_before_calls": True,
        "public_cases_sha256": hashlib.sha256(public_path.read_bytes()).hexdigest(),
        "sealed_answers_sha256": hashlib.sha256(sealed_path.read_bytes()).hexdigest(),
    }
    (args.output_dir / "panel_hashes.json").write_text(json.dumps(hashes, indent=2) + "\n")
    print(json.dumps({
        "benchmark": metadata["benchmark"], "cases": len(cases),
        "families": {family: sum(case["family"] == family for case in cases) for family in ("sequence", "constraint", "logic")},
        "output_dir": str(args.output_dir),
    }, indent=2))


if __name__ == "__main__":
    main()
