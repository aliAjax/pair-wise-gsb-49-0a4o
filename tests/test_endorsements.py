import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'event_id': 'CAT-2026-01', 'attachment': 1000000.0, 'limit': 5000000.0, 'cession_pct': 0.4, 'loss_amount': 3000000.0, 'reinstatement_pct': 0.15, 'aggregate_prior': 0.0}
UW = Actor("uw-1", "underwriter")
CLAIMS = Actor("clm-1", "claims_officer")


class EndorsementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, data=None, reference="RI-25001"):
        return self.service.create(UW, reference, data or CREATE_DATA)

    def _approved_record(self):
        record = self._create()
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-8"})
        record = self.service.act(CLAIMS, record["id"], record["version"], "submit_claim", {"claim_number": "CLM-88", "event_id": "CAT-2026-01"})
        record = self.service.act(CLAIMS, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        return record

    def test_increase_updates_limit_and_ledger(self):
        record = self._create()
        ledger = self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-02-01", "direction": "increase", "amount": 500000.0, "reason": "标的扩容"})
        self.assertEqual(ledger["current_limit"], 5500000.0)
        self.assertEqual(ledger["capacity"], 1800000.0)
        self.assertEqual(ledger["used"], 0.0)
        self.assertEqual(ledger["remaining"], 1800000.0)
        self.assertEqual(len(ledger["endorsements"]), 1)
        entry = ledger["endorsements"][0]
        self.assertEqual(entry["resulting_limit"], 5500000.0)
        self.assertEqual(entry["reason"], "标的扩容")

    def test_versions_recomputed_by_effective_date(self):
        record = self._create()
        self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-03-01", "direction": "increase", "amount": 500000.0, "reason": "加保"})
        ledger = self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-02-01", "direction": "decrease", "amount": 200000.0, "reason": "回溯减保"})
        versions = [(e["effective_date"], e["resulting_limit"]) for e in ledger["endorsements"]]
        self.assertEqual(versions, [("2026-02-01", 4800000.0), ("2026-03-01", 5300000.0)])
        self.assertEqual(ledger["current_limit"], 5300000.0)
        self.assertEqual(len(ledger["snapshots"]), 2)

    def test_decrease_below_approved_is_blocked(self):
        record = self._approved_record()
        with self.assertRaises(Conflict) as ctx:
            self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-04-01", "direction": "decrease", "amount": 3000000.0, "reason": "分保人缩减"})
        details = ctx.exception.details
        self.assertEqual(details["contract"], "RI-25001")
        self.assertEqual(details["gap"], 320000.0)
        self.assertEqual(details["used"], 720000.0)
        kinds = [item["kind"] for item in details["occupancy"]]
        self.assertIn("claim", kinds)
        ledger = self.service.contract_ledger(UW, record["id"])
        self.assertEqual(ledger["endorsements"], [])
        self.assertEqual(ledger["current_limit"], 5000000.0)
        self.assertEqual(ledger["used"], 720000.0)
        self.assertEqual(ledger["remaining"], 880000.0)

    def test_decrease_within_headroom_and_snapshot_kept(self):
        record = self._approved_record()
        ledger = self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-04-01", "direction": "decrease", "amount": 500000.0, "reason": "部分解约"})
        self.assertEqual(ledger["current_limit"], 4500000.0)
        self.assertEqual(ledger["used"], 720000.0)
        self.assertEqual(ledger["remaining"], 680000.0)
        self.assertEqual(len(ledger["snapshots"]), 1)
        snapshot = ledger["snapshots"][0]
        self.assertEqual(snapshot["current_limit"], 4500000.0)
        self.assertEqual(snapshot["used_amount"], 720000.0)
        self.assertEqual(snapshot["remaining_amount"], 680000.0)
        timeline = self.service.timeline(UW, record["id"])
        self.assertEqual(timeline[-1]["action"], "endorsement_registered")

    def test_decrease_below_attachment_is_blocked(self):
        record = self._create()
        with self.assertRaises(Conflict):
            self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-01-01", "direction": "decrease", "amount": 4500000.0, "reason": "过度减保"})

    def test_aggregate_prior_counts_as_used(self):
        data = dict(CREATE_DATA)
        data["aggregate_prior"] = 200000.0
        record = self._create(data, "RI-25002")
        ledger = self.service.contract_ledger(UW, record["id"])
        self.assertEqual(ledger["used"], 200000.0)
        self.assertEqual(ledger["remaining"], 1400000.0)
        self.assertEqual(ledger["occupancy"][0]["kind"], "aggregate_prior")

    def test_ledger_survives_reopen(self):
        record = self._approved_record()
        self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-04-01", "direction": "decrease", "amount": 500000.0, "reason": "部分解约"})
        reopened = build_service(self.db_path)
        ledger = reopened.contract_ledger(UW, record["id"])
        self.assertEqual(ledger["current_limit"], 4500000.0)
        self.assertEqual(ledger["used"], 720000.0)
        self.assertEqual(ledger["remaining"], 680000.0)
        self.assertEqual(len(ledger["endorsements"]), 1)
        self.assertEqual(len(ledger["snapshots"]), 1)

    def test_validation_and_permission(self):
        record = self._create()
        with self.assertRaises(ValidationError):
            self.service.register_endorsement(UW, record["id"], {"effective_date": "not-a-date", "direction": "increase", "amount": 1.0, "reason": "x"})
        with self.assertRaises(ValidationError):
            self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-01-01", "direction": "increase", "amount": 0, "reason": "x"})
        with self.assertRaises(ValidationError):
            self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-01-01", "direction": "increase", "amount": 1.0, "reason": ""})
        with self.assertRaises(ValidationError):
            self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-01-01", "direction": "sideways", "amount": 1.0, "reason": "x"})
        with self.assertRaises(PermissionDenied):
            self.service.register_endorsement(CLAIMS, record["id"], {"effective_date": "2026-01-01", "direction": "increase", "amount": 1.0, "reason": "x"})

    def test_rejected_record_cannot_endorse(self):
        record = self._create()
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-8"})
        record = self.service.act(CLAIMS, record["id"], record["version"], "submit_claim", {"claim_number": "CLM-88", "event_id": "CAT-2026-01"})
        record = self.service.act(CLAIMS, record["id"], record["version"], "reject", {"reject_reason": "不属于保障范围"})
        with self.assertRaises(Conflict):
            self.service.register_endorsement(UW, record["id"], {"effective_date": "2026-01-01", "direction": "increase", "amount": 1.0, "reason": "x"})
