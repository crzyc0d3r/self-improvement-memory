"""Validated improvement history and a bounded completion interlock."""

from copy import deepcopy
from dataclasses import dataclass


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value


def _integer(value, field, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")


def _names(value, field):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{field} must be a list of nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} contains duplicates")


def validate_record(record):
    """Reject ambiguous or internally inconsistent pending/completed records."""
    if not isinstance(record, dict) or set(record) != {"next_id", "carried_from", "aliases", "problems"}:
        raise ValueError("invalid memory fields")
    _integer(record["next_id"], "next_id", 1)
    if record["aliases"] != {}:
        raise ValueError("this lab supports exact-root-cause merging only")
    lineage = record["carried_from"]
    if lineage is not None:
        if not isinstance(lineage, dict) or set(lineage) != {"arm", "domain", "into"}:
            raise ValueError("invalid transfer provenance")
        for key, value in lineage.items():
            _text(value, key)
    if not isinstance(record["problems"], list):
        raise ValueError("problems must be a list")
    ids, names = set(), set()
    attempt_fields = {"round", "sample", "accepted", "backfilled", "what", "files", "predicted",
                      "fixed", "missed", "passed", "total", "domain", "model", "benchmark",
                      "status", "evaluation_tasks", "outcomes"}
    for problem in record["problems"]:
        if not isinstance(problem, dict) or set(problem) != {"id", "name", "opened_round", "attempts"}:
            raise ValueError("invalid problem fields")
        pid = _text(problem["id"], "id")
        if not pid.startswith("P") or not pid[1:].isdigit() or str(int(pid[1:])) != pid[1:]:
            raise ValueError("problem IDs must be P followed by a positive integer")
        if int(pid[1:]) < 1 or int(pid[1:]) >= record["next_id"] or pid in ids:
            raise ValueError("invalid or duplicate problem ID")
        name = _text(problem["name"], "name")
        if name in names:
            raise ValueError("identical root causes must share one problem")
        ids.add(pid)
        names.add(name)
        _integer(problem["opened_round"], "opened_round", 1)
        if not isinstance(problem["attempts"], list) or not problem["attempts"]:
            raise ValueError("a problem needs an attempt")
        keys = set()
        for attempt in problem["attempts"]:
            if not isinstance(attempt, dict) or set(attempt) != attempt_fields:
                raise ValueError("invalid attempt fields")
            _integer(attempt["round"], "round", problem["opened_round"])
            for key in ("sample", "what", "domain", "model", "benchmark"):
                _text(attempt[key], key)
            identity = (attempt["round"], attempt["sample"])
            if identity in keys:
                raise ValueError("duplicate round/sample")
            keys.add(identity)
            for key in ("files", "predicted", "fixed", "missed", "evaluation_tasks"):
                _names(attempt[key], key)
            tasks, predicted = attempt["evaluation_tasks"], attempt["predicted"]
            if not tasks or not set(predicted) <= set(tasks):
                raise ValueError("predictions must refer to the planned evaluation tasks")
            if attempt["backfilled"] is not False:
                raise ValueError("predictions may not be backfilled")
            if attempt["status"] == "pending":
                if any(attempt[k] is not None for k in ("accepted", "passed", "total", "outcomes")):
                    raise ValueError("pending attempts cannot contain evaluation results")
                if attempt["fixed"] or attempt["missed"]:
                    raise ValueError("pending attempts cannot contain outcomes")
            elif attempt["status"] == "evaluated":
                outcomes = attempt["outcomes"]
                if not isinstance(outcomes, dict) or set(outcomes) != set(tasks):
                    raise ValueError("outcomes must cover every planned task exactly")
                if any(type(x) is not bool for x in outcomes.values()) or type(attempt["accepted"]) is not bool:
                    raise ValueError("outcomes and accepted must be booleans")
                _integer(attempt["passed"], "passed")
                _integer(attempt["total"], "total", 1)
                if attempt["total"] != len(tasks) or attempt["passed"] != sum(outcomes.values()):
                    raise ValueError("score does not match the outcomes")
                if attempt["fixed"] != [x for x in predicted if outcomes[x]]:
                    raise ValueError("fixed tasks disagree with predictions and outcomes")
                if attempt["missed"] != [x for x in predicted if not outcomes[x]]:
                    raise ValueError("missed tasks disagree with predictions and outcomes")
            else:
                raise ValueError("invalid attempt status")
    return deepcopy(record)


class Memory:
    """Only propose() can set predictions; evaluate() can only add outcomes."""

    def __init__(self, record=None):
        self._record = validate_record(record) if record is not None else {
            "next_id": 1, "carried_from": None, "aliases": {}, "problems": []}

    def snapshot(self):
        return validate_record(self._record)

    def transfer(self, source_run, domain, target_run):
        result = self.snapshot()
        result["carried_from"] = {"arm": source_run, "domain": domain, "into": target_run}
        return Memory(result)

    def propose(self, *, cause, round, sample, what, files, predicted, tasks, domain, model, benchmark):
        result = self.snapshot()
        problem = next((p for p in result["problems"] if p["name"] == cause), None)
        if problem is None:
            problem = {"id": f"P{result['next_id']}", "name": cause,
                       "opened_round": round, "attempts": []}
            result["next_id"] += 1
            result["problems"].append(problem)
        attempt = {"round": round, "sample": sample, "accepted": None, "backfilled": False,
                   "what": what, "files": deepcopy(files), "predicted": deepcopy(predicted),
                   "fixed": [], "missed": [], "passed": None, "total": None, "domain": domain,
                   "model": model, "benchmark": benchmark, "status": "pending",
                   "evaluation_tasks": deepcopy(tasks), "outcomes": None}
        problem["attempts"].append(attempt)
        self._record = validate_record(result)
        return problem["id"], round, sample

    def evaluate(self, identity, outcomes, *, accepted):
        result = self.snapshot()
        try:
            pid, round, sample = identity
            problem = next(p for p in result["problems"] if p["id"] == pid)
            attempt = next(a for a in problem["attempts"] if (a["round"], a["sample"]) == (round, sample))
        except (ValueError, TypeError, StopIteration):
            raise ValueError("unknown attempt") from None
        if attempt["status"] != "pending":
            raise ValueError("an attempt can be evaluated only once")
        if not isinstance(outcomes, dict) or set(outcomes) != set(attempt["evaluation_tasks"]):
            raise ValueError("provide outcomes for exactly the predeclared tasks")
        if any(type(x) is not bool for x in outcomes.values()) or type(accepted) is not bool:
            raise ValueError("evaluation values must be booleans")
        attempt.update(status="evaluated", accepted=accepted, outcomes=deepcopy(outcomes),
                       passed=sum(outcomes.values()), total=len(outcomes),
                       fixed=[x for x in attempt["predicted"] if outcomes[x]],
                       missed=[x for x in attempt["predicted"] if not outcomes[x]])
        self._record = validate_record(result)


@dataclass(frozen=True)
class Receipt:
    name: str
    executed: bool
    succeeded: bool | None

    def __post_init__(self):
        _text(self.name, "receipt name")
        if type(self.executed) is not bool:
            raise ValueError("executed must be boolean")
        if self.executed and type(self.succeeded) is not bool:
            raise ValueError("executed receipts require a success/failure outcome")
        if not self.executed and self.succeeded is not None:
            raise ValueError("unexecuted receipts have no outcome")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    cap_bypass: bool = False


class Interlock:
    """Reproduce the paper's simplified branch order using explicit receipts.

    A receipt proves an in-process operation ran, not that its result is correct.
    The cap and pending-confirmation paths deliberately retain their limitations.
    """

    def __init__(self, max_rejections=3):
        _integer(max_rejections, "max_rejections", 1)
        self.max_rejections = max_rejections
        self.rejections = 0
        self.executed = 0
        self.succeeded = 0
        self.last_parse_error = False
        self.last_discarded = False

    def turn(self, receipts=(), *, complete=False, parse_error=False,
             discarded=False, terminal_error=False, pending=False):
        for flag in (complete, parse_error, discarded, terminal_error, pending):
            if type(flag) is not bool:
                raise ValueError("turn flags must be booleans")
        receipts = tuple(receipts)
        if any(not isinstance(r, Receipt) for r in receipts):
            raise ValueError("receipts must be Receipt objects")
        ran = any(r.executed for r in receipts)
        self.executed += sum(r.executed for r in receipts)
        self.succeeded += sum(r.succeeded is True for r in receipts)
        if not complete:
            decision = Decision(False, "completion not requested")
        elif self.rejections >= self.max_rejections:
            decision = Decision(True, "rejection cap reached", True)
        elif terminal_error:
            decision = Decision(False, "terminal failure")
        elif pending:
            decision = Decision(True, "pending confirmation")
        elif self.last_parse_error and not ran:
            decision = Decision(False, "previous parse error without execution")
        elif self.last_discarded and not ran:
            decision = Decision(False, "discarded commands without execution")
        elif self.executed <= 1 and not ran:
            decision = Decision(False, "insufficient execution")
        else:
            decision = Decision(True, "interlock checks passed")
        if complete and not decision.allowed:
            self.rejections += 1
        self.last_parse_error = parse_error
        self.last_discarded = discarded
        return decision
