"""Deterministic, in-process evidence fixtures; no commands or providers run."""

from dataclasses import asdict
from sim import Interlock, Memory, Receipt


def evaluate_scenarios():
    rows = []

    def add(name, gate, decision, expected):
        rows.append({"task": name, "expected_allowed": expected, "decision": asdict(decision),
                     "executed_receipts": gate.executed, "successful_receipts": gate.succeeded,
                     "passed": decision.allowed == expected})

    gate = Interlock()
    gate.turn(parse_error=True)
    add("parse-error", gate, gate.turn(complete=True), False)
    gate = Interlock()
    gate.turn(discarded=True)
    add("discarded-command", gate, gate.turn(complete=True), False)
    gate = Interlock()
    gate.turn([Receipt("sum", True, sum([1, 2]) == 3)])
    add("one-operation", gate, gate.turn(complete=True), False)
    gate = Interlock()
    failed = Receipt("deliberate-check", True, 2 + 2 == 5)
    add("terminal-failure", gate, gate.turn([failed], complete=True, terminal_error=True), False)
    gate = Interlock()
    gate.turn([Receipt("sum", True, sum([1, 2]) == 3),
               Receipt("sort", True, sorted([2, 1]) == [1, 2])])
    add("verified-operations", gate, gate.turn(complete=True), True)
    gate = Interlock()
    for _ in range(3):
        gate.turn(complete=True)
    add("cap-without-evidence", gate, gate.turn(complete=True), False)
    return rows


def run_demo():
    """Publish each prediction snapshot before the corresponding evaluator call."""
    memory = Memory()
    tasks = ["parse-error", "discarded-command", "one-operation", "terminal-failure",
             "verified-operations", "cap-without-evidence"]
    kwargs = dict(cause="premature completion without execution evidence", files=["sim.py"],
                  domain="local-contracts", tasks=tasks, predicted=tasks)
    identity = memory.propose(round=1, sample="source-attempt", model="fixture-source",
                              benchmark="execution-contracts-v1",
                              what="Track execution receipts and gate premature completion", **kwargs)
    predictions = [memory.snapshot()]
    rows = evaluate_scenarios()
    memory.evaluate(identity, {r["task"]: r["passed"] for r in rows}, accepted=True)
    memory = memory.transfer("source-run", "local-contracts", "target-run")
    target_tasks = tasks + ["independent-verifier"]
    identity = memory.propose(round=2, sample="target-attempt", model="fixture-target",
                              benchmark="execution-contracts-v2",
                              what="Reuse the gate under an additional independent correctness requirement",
                              **{**kwargs, "tasks": target_tasks, "predicted": target_tasks})
    predictions.append(memory.snapshot())
    target_outcomes = {r["task"]: r["passed"] for r in evaluate_scenarios()}
    # A retained calculation runs correctly but solves the old input (2, 2).
    # The target task asks for the sum of (2, 3); execution alone cannot check it.
    candidate = sum([2, 2])
    gate = Interlock()
    gate.turn([Receipt("retained-calculation", True, candidate == 4),
               Receipt("integer-output", True, isinstance(candidate, int))])
    decision = gate.turn(complete=True)
    target_outcomes["independent-verifier"] = decision.allowed and candidate == sum([2, 3])
    memory.evaluate(identity, target_outcomes, accepted=False)
    return {"predictions_before_evaluation": predictions, "scenarios": rows,
            "memory": memory.snapshot()}
