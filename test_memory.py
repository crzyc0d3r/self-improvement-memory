"""Regression tests for memory invariants, evidence contracts, and HTTP output."""

import json
import threading
import unittest
from copy import deepcopy
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

from app import Handler, render
from scenarios import evaluate_scenarios, run_demo
from sim import Interlock, Memory, Receipt, validate_record


class MemoryTests(unittest.TestCase):
    def propose(self, memory, **changes):
        args = dict(cause="premature completion", round=1, sample="a", what="add an execution gate",
                    files=["sim.py"], predicted=["missing", "failed"], tasks=["missing", "failed", "ok"],
                    domain="local", model="fixture-source", benchmark="contract-v1")
        args.update(changes)
        return memory.propose(**args)

    def test_predictions_are_pending_before_evaluation(self):
        memory = Memory()
        identity = self.propose(memory)
        before = memory.snapshot()
        attempt = before["problems"][0]["attempts"][0]
        self.assertEqual(attempt["predicted"], ["missing", "failed"])
        self.assertIsNone(attempt["outcomes"])
        memory.evaluate(identity, {"missing": True, "failed": False, "ok": True}, accepted=True)
        after = memory.snapshot()["problems"][0]["attempts"][0]
        self.assertEqual((after["fixed"], after["missed"], after["passed"], after["total"]),
                         (["missing"], ["failed"], 2, 3))
        self.assertIsNone(attempt["outcomes"])

    def test_failures_do_not_imply_rejection_by_selector(self):
        memory = Memory()
        identity = self.propose(memory)
        memory.evaluate(identity, {"missing": False, "failed": False, "ok": False}, accepted=True)
        attempt = memory.snapshot()["problems"][0]["attempts"][0]
        self.assertTrue(attempt["accepted"])
        self.assertEqual(attempt["passed"], 0)

    def test_bad_outcomes_are_atomic_and_do_not_rewrite_predictions(self):
        for outcomes in ({"missing": True}, {"missing": True, "failed": 1, "ok": True},
                         {"missing": True, "failed": True, "ok": True, "extra": True}):
            memory = Memory()
            identity = self.propose(memory)
            before = memory.snapshot()
            with self.assertRaises(ValueError):
                memory.evaluate(identity, outcomes, accepted=False)
            self.assertEqual(memory.snapshot(), before)

    def test_evaluation_is_once_only(self):
        memory = Memory()
        identity = self.propose(memory)
        memory.evaluate(identity, dict.fromkeys(["missing", "failed", "ok"], False), accepted=False)
        with self.assertRaises(ValueError):
            memory.evaluate(identity, dict.fromkeys(["missing", "failed", "ok"], True), accepted=True)

    def test_snapshot_and_input_lists_do_not_mutate_memory(self):
        predicted = ["missing", "failed"]
        memory = Memory()
        self.propose(memory, predicted=predicted)
        predicted.clear()
        snapshot = memory.snapshot()
        snapshot["problems"][0]["attempts"][0]["predicted"].clear()
        self.assertEqual(memory.snapshot()["problems"][0]["attempts"][0]["predicted"], ["missing", "failed"])

    def test_exact_root_cause_merging_and_distinct_text(self):
        memory = Memory()
        self.propose(memory)
        self.propose(memory, round=2)
        self.propose(memory, cause="different diagnosis", round=3)
        problems = memory.snapshot()["problems"]
        self.assertEqual([len(p["attempts"]) for p in problems], [2, 1])
        self.assertEqual([p["id"] for p in problems], ["P1", "P2"])

    def test_transfer_preserves_history_and_model_context(self):
        memory = Memory()
        self.propose(memory)
        target = memory.transfer("source", "local", "target")
        self.propose(target, round=2, model="fixture-target", benchmark="contract-v2")
        self.assertIsNone(memory.snapshot()["carried_from"])
        record = target.snapshot()
        self.assertEqual(record["carried_from"]["into"], "target")
        attempts = record["problems"][0]["attempts"]
        self.assertEqual([a["model"] for a in attempts], ["fixture-source", "fixture-target"])
        self.assertEqual([a["benchmark"] for a in attempts], ["contract-v1", "contract-v2"])

    def test_duplicate_attempt_is_atomic(self):
        memory = Memory()
        self.propose(memory)
        before = memory.snapshot()
        with self.assertRaises(ValueError):
            self.propose(memory)
        self.assertEqual(before, memory.snapshot())

    def test_invalid_proposals(self):
        cases = [{"cause": " "}, {"round": True}, {"round": 0}, {"files": ["x", "x"]},
                 {"predicted": ["not-planned"]}, {"tasks": []}, {"model": ""}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.propose(Memory(), **changes)

    def test_roundtrip_and_corrupt_records(self):
        original = run_demo()["memory"]
        self.assertEqual(Memory(json.loads(json.dumps(original))).snapshot(), original)
        for key, value in [("passed", 99), ("total", 99), ("fixed", []), ("missed", []),
                           ("backfilled", True), ("accepted", 1), ("status", "pending")]:
            bad = deepcopy(original)
            bad["problems"][0]["attempts"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_record(bad)
        for mutator in [lambda r: r.update(next_id=1), lambda r: r.update(unknown=True),
                        lambda r: r["problems"].append(deepcopy(r["problems"][0])),
                        lambda r: r.update(carried_from={"arm": "x"})]:
            bad = deepcopy(original)
            mutator(bad)
            with self.assertRaises(ValueError):
                validate_record(bad)


class InterlockTests(unittest.TestCase):
    def test_previous_parse_error_and_discarded_branches(self):
        for flags, reason in [({"parse_error": True}, "previous parse error without execution"),
                              ({"discarded": True}, "discarded commands without execution")]:
            gate = Interlock()
            gate.turn(**flags)
            decision = gate.turn(complete=True)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, reason)

    def test_receipt_on_current_turn_recovers_previous_parse_error(self):
        gate = Interlock()
        gate.turn(parse_error=True)
        self.assertTrue(gate.turn([Receipt("local-operation", True, True)], complete=True).allowed)

    def test_zero_or_one_prior_execution_rejected(self):
        for receipts in ([], [Receipt("one", True, True)]):
            gate = Interlock()
            gate.turn(receipts)
            self.assertEqual(gate.turn(complete=True).reason, "insufficient execution")

    def test_unexecuted_receipt_does_not_count(self):
        gate = Interlock()
        decision = gate.turn([Receipt("not-run", False, None)], complete=True)
        self.assertFalse(decision.allowed)
        self.assertEqual(gate.executed, 0)

    def test_failed_execution_is_not_a_successful_receipt(self):
        gate = Interlock()
        decision = gate.turn([Receipt("failed", True, False)], complete=True, terminal_error=True)
        self.assertEqual((gate.executed, gate.succeeded), (1, 0))
        self.assertEqual(decision.reason, "terminal failure")

    def test_two_operations_allow_later_completion(self):
        gate = Interlock()
        gate.turn([Receipt("one", True, True), Receipt("two", True, True)])
        self.assertTrue(gate.turn(complete=True).allowed)

    def test_pending_bypasses_low_execution_but_not_terminal_error(self):
        self.assertTrue(Interlock().turn(complete=True, pending=True).allowed)
        decision = Interlock().turn(complete=True, pending=True, terminal_error=True)
        self.assertFalse(decision.allowed)

    def test_cap_bypasses_errors_without_proving_success(self):
        gate = Interlock()
        self.assertFalse(gate.turn(complete=True).allowed)
        self.assertFalse(gate.turn(complete=True).allowed)
        self.assertFalse(gate.turn(complete=True).allowed)
        decision = gate.turn(complete=True, terminal_error=True)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.cap_bypass)
        self.assertEqual((gate.executed, gate.succeeded), (0, 0))

    def test_invalid_receipts_and_flags(self):
        for args in [("x", False, True), ("x", True, None), ("x", 1, True), ("", True, True)]:
            with self.assertRaises(ValueError):
                Receipt(*args)
        for kwargs in [{"complete": 1}, {"pending": None}]:
            with self.assertRaises(ValueError):
                Interlock().turn(**kwargs)
        with self.assertRaises(ValueError):
            Interlock().turn([{"executed": True}])

    def test_scenario_scores_and_predictions(self):
        report = run_demo()
        self.assertEqual(sum(r["passed"] for r in evaluate_scenarios()), 5)
        attempts = report["memory"]["problems"][0]["attempts"]
        self.assertEqual([(a["passed"], a["total"]) for a in attempts], [(5, 6), (5, 7)])
        self.assertEqual(attempts[1]["missed"], ["cap-without-evidence", "independent-verifier"])
        for snapshot in report["predictions_before_evaluation"]:
            self.assertEqual(snapshot["problems"][0]["attempts"][-1]["status"], "pending")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def test_routes(self):
        with urlopen(self.url + "/") as response:
            body = response.read().decode()
            self.assertEqual(response.status, 200)
            self.assertIn("Completion evidence", body)
            self.assertIn("cap-without-evidence", body)
            self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
            self.assertNotIn("<script", body)
        with urlopen(self.url + "/record.json") as response:
            self.assertEqual(json.load(response), run_demo())
        with urlopen(self.url + "/health") as response:
            self.assertEqual(json.load(response), {"status": "ok"})
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + "/unknown")
        self.assertEqual(error.exception.code, 404)

    def test_html_escapes_record_content(self):
        report = run_demo()
        report["memory"]["problems"][0]["attempts"][0]["model"] = '<img src="bad">'
        body = render(report)
        self.assertNotIn('<img src="bad">', body)
        self.assertIn("&lt;img", body)


if __name__ == "__main__":
    unittest.main()
