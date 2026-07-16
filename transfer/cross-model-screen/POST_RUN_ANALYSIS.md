# What the Cross-Model Screen Taught Us

## Result

Exact accuracy on the same 12 answer-blind selected cases, with four sequence, four constraint, and four logic problems:

| Organization | Luna Light, historical | Terra Low | Luna Medium |
| --- | ---: | ---: | ---: |
| One direct solver | 1/12, 8.3% | 7/12, 58.3% | 3/12, 25.0% |
| Five-solver plurality | 1/12, 8.3% | 10/12, 83.3% | 5/12, 41.7% |
| Five solvers plus one repairer | **5/12, 41.7%** | **11/12, 91.7%** | 9/12, 75.0% |
| Five solvers plus three parallel repairers | 4/12, 33.3% | **11/12, 91.7%** | 10/12, 83.3% |
| Five solvers plus three-step cross-examination | 1/12, 8.3% | **11/12, 91.7%** | **11/12, 91.7%** |

## Central finding

The value of deeper orchestration depended on the capability of the agents doing the judging and on how much recoverable headroom remained.

- **Luna Light:** one repairer helped, but adding more layers made the result worse. Cross-examination fixed one plurality miss and overturned one plurality success, producing no net gain.
- **Luna Medium:** performance improved progressively as the organization deepened: 5/12 plurality, 9/12 with one repairer, 10/12 with three parallel repairers, and 11/12 with sequential cross-examination.
- **Terra Low:** the five-agent bank was already strong at 10/12. Every review design fixed the same one remaining selection error and then reached the same 11/12 ceiling.

For Luna Medium, sequential cross-examination converted six plurality misses into correct answers and harmed none. It selected the correct answer on all 11 cases where at least one of the five base agents had found it. The exact paired sign test is 6 helpful versus 0 harmful, two-sided unadjusted p = 0.031. This is useful directional evidence, not a universal threshold estimate.

The pattern supports this working hypothesis:

> Orchestration depth is useful only when the evaluators are capable enough to recognize and repair mistakes. Weak evaluators can propagate errors. Strong base agents may leave too little headroom for elaborate review to justify its cost.

## Efficiency

Compared with five-vote plurality:

| Condition | Organization | Added review calls | Net additional correct | Calls per net gain |
| --- | --- | ---: | ---: | ---: |
| Luna Light | One repairer | 12 | 4 | **3.0** |
| Luna Light | Three repairers | 36 | 3 | 12.0 |
| Luna Light | Cross-examination | 36 | 0 | no gain |
| Terra Low | One repairer | 10 | 1 | **10.0** |
| Terra Low | Three repairers | 30 | 1 | 30.0 |
| Terra Low | Cross-examination | 30 | 1 | 30.0 |
| Luna Medium | One repairer | 12 | 4 | **3.0** |
| Luna Medium | Three repairers | 36 | 5 | 7.2 |
| Luna Medium | Cross-examination | 36 | 6 | 6.0 |

One repairer remains the best economical default in this screen. Cross-examination produced the highest Medium ceiling, but it used three times as many review calls.

## Model comparison

Terra Low was numerically stronger before review: 7/12 direct and 10/12 plurality, versus Luna Medium at 3/12 and 5/12. After sequential cross-examination, both reached 11/12. On these cases, orchestration closed the observed five-case plurality gap.

That does not establish that Terra is generally stronger or that the models are equivalent after orchestration. The paired sample is too small. The CLI also recorded the requested aliases and efforts but did not independently report the served backend model identity.

## Family diagnostic

For Luna Medium, plurality to cross-examination changed:

- Sequence: 2/4 to 4/4
- Constraint planning: 0/4 to 3/4
- Logic: 3/4 to 4/4

All best systems failed the same constraint-planning case. With only four cases per family, these are diagnostic observations rather than stable family estimates.

## What we cannot claim

- **91.7% is not validated general performance.** Eleven correct of twelve has a wide Wilson 95% interval of about 64.6% to 98.5%.
- The cases are historical instances from the same three-family generator used during development. This tests model and reasoning-effort transfer, not unrelated-domain generalization.
- Luna Light is a non-contemporaneous historical reference. Runtime drift is therefore a possible confound.
- The screen has one replicate. Small differences such as 10/12 versus 11/12 are not reliable rankings.

## Integrity record

- 274 registered logical calls completed with valid outputs.
- 276 transport attempts were made. Two Luna Medium calls timed out before producing answers and were each retried once under the frozen infrastructure-only policy.
- No valid, wrong, or malformed answer was selectively rerun.
- Answers were parsed only after the terminal result-hash manifest was frozen.
- The post-run audit independently reconstructed all 180 condition-method-case rows and matched the stored summary exactly.

## Practical implication

A general adaptive orchestrator should not always add more reviewers. It should estimate two things first:

1. **Evaluator capability:** can this model reliably compare and repair competing proposals?
2. **Recoverable headroom:** does the answer bank contain useful diversity that the current selection rule is failing to convert?

The simplest promising policy from this screen is:

- start with five independent answers;
- use one falsifying repairer as the efficient default when they disagree;
- escalate to sequential cross-examination only when a calibrated model has shown it can benefit from the extra depth.

This is a hypothesis for fresh validation, not a finalized universal routing rule.
