import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


UW = Actor("uw-1", "underwriter")
UW2 = Actor("uw-2", "underwriter")
CO = Actor("co-1", "claims_officer")
FIN = Actor("fin-1", "finance")
ADMIN = Actor("root", "admin")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def create_treaty(self, reference="TREATY-01", limit=1000.0, inception="2026-01-01"):
        return self.service.create_treaty(UW, reference, {"name": "火险超赔合约", "initial_limit": limit, "inception_date": inception})

    def confirm(self, treaty_id, date, direction, amount, reason="调整", actor=UW):
        endo = self.service.register_endorsement(actor, treaty_id, {
            "effective_date": date, "direction": direction, "amount": amount, "reason": reason,
        })
        return self.service.confirm_endorsement(actor, endo["id"])

    def test_create_treaty_initial_ledger(self):
        treaty = self.create_treaty()
        self.assertEqual(treaty["current_limit"], 1000.0)
        self.assertEqual(treaty["used_approved_recoveries"], 0.0)
        self.assertEqual(treaty["remaining_capacity"], 1000.0)
        self.assertEqual(len(treaty["snapshots"]), 1)
        self.assertEqual(treaty["snapshots"][0]["kind"], "initial")
        self.assertEqual(treaty["snapshots"][0]["limit_after"], 1000.0)

    def test_increase_decrease_recompute_versions_by_effective_date(self):
        treaty = self.create_treaty()
        treaty = self.confirm(treaty["id"], "2026-03-01", "increase", 200.0)
        self.assertEqual(treaty["current_limit"], 1200.0)
        treaty = self.confirm(treaty["id"], "2026-06-01", "decrease", 500.0)
        self.assertEqual(treaty["current_limit"], 700.0)
        versions = [(v["seq"], v["effective_date"], v["limit_after"]) for v in treaty["limit_versions"]]
        self.assertEqual(versions, [(1, "2026-03-01", 1200.0), (2, "2026-06-01", 700.0)])

    def test_backdated_endorsement_inserts_version_in_effective_order(self):
        treaty = self.create_treaty()
        self.confirm(treaty["id"], "2026-06-01", "decrease", 300.0)
        # 倒签加保：确认序号更晚，但生效日在前
        treaty = self.confirm(treaty["id"], "2026-02-01", "increase", 500.0)
        versions = [(v["seq"], v["effective_date"], v["limit_after"]) for v in treaty["limit_versions"]]
        self.assertEqual(versions, [(2, "2026-02-01", 1500.0), (1, "2026-06-01", 1200.0)])
        self.assertEqual(treaty["current_limit"], 1200.0)

    def test_decrease_below_approved_recoveries_is_blocked_with_details(self):
        treaty = self.create_treaty()
        self.service.register_claim(CO, treaty["id"], {
            "claim_number": "CLM-1", "approved_date": "2026-05-01", "recoverable_amount": 800.0,
        })
        endo = self.service.register_endorsement(UW, treaty["id"], {
            "effective_date": "2026-06-01", "direction": "decrease", "amount": 300.0, "reason": "减保",
        })
        with self.assertRaises(Conflict) as caught:
            self.service.confirm_endorsement(UW, endo["id"])
        details = caught.exception.details
        self.assertEqual(details["type"], "capacity_shortfall")
        self.assertEqual(details["treaty_reference"], "TREATY-01")
        self.assertEqual(details["shortfall"], 100.0)
        self.assertEqual(details["limit_after"], 700.0)
        self.assertEqual(details["used_approved_recoveries"], 800.0)
        self.assertEqual(len(details["occupancies"]), 1)
        self.assertEqual(details["occupancies"][0]["claim_number"], "CLM-1")
        # 受阻后批单仍待确认、无新增快照
        view = self.service.get_treaty(UW, treaty["id"])
        self.assertEqual([e["status"] for e in view["endorsements"]], ["pending"])
        self.assertEqual([s["kind"] for s in view["snapshots"]], ["initial", "claim_registered"])

    def test_claim_after_endorsement_effective_date_does_not_block(self):
        treaty = self.create_treaty()
        self.confirm(treaty["id"], "2026-06-01", "decrease", 300.0)
        self.service.register_claim(CO, treaty["id"], {
            "claim_number": "CLM-9", "approved_date": "2026-07-01", "recoverable_amount": 900.0,
        })
        # 再确认一个 2026-05-01 的减保：此时赔案尚未核定，不构成缺口
        treaty = self.confirm(treaty["id"], "2026-05-01", "decrease", 100.0)
        versions = [(v["effective_date"], v["limit_after"]) for v in treaty["limit_versions"]]
        self.assertEqual(versions, [("2026-05-01", 900.0), ("2026-06-01", 600.0)])

    def test_backdated_decrease_breach_against_earlier_claims_is_reported(self):
        treaty = self.create_treaty()
        self.confirm(treaty["id"], "2026-06-01", "increase", 200.0)
        self.service.register_claim(CO, treaty["id"], {
            "claim_number": "CLM-2", "approved_date": "2026-03-01", "recoverable_amount": 1100.0,
        })
        endo = self.service.register_endorsement(UW, treaty["id"], {
            "effective_date": "2026-02-01", "direction": "decrease", "amount": 500.0, "reason": "倒签减保",
        })
        with self.assertRaises(Conflict) as caught:
            self.service.confirm_endorsement(UW, endo["id"])
        # 缺口发生在 2026-02 版本（赔案 3 月才核定，不计入 2 月），但会在 6 月版本暴露：1100 > 700
        self.assertEqual(caught.exception.details["effective_date"], "2026-06-01")
        self.assertEqual(caught.exception.details["shortfall"], 400.0)

    def test_claim_registration_updates_used_and_snapshot(self):
        treaty = self.create_treaty()
        treaty = self.service.register_claim(CO, treaty["id"], {
            "claim_number": "CLM-3", "approved_date": "2026-04-01", "recoverable_amount": 250.5,
        })
        self.assertEqual(treaty["used_approved_recoveries"], 250.5)
        self.assertEqual(treaty["remaining_capacity"], 749.5)
        kinds = [s["kind"] for s in treaty["snapshots"]]
        self.assertEqual(kinds, ["initial", "claim_registered"])
        latest = treaty["snapshots"][-1]
        self.assertEqual(latest["limit_after"], 1000.0)
        self.assertEqual(latest["used_approved_recoveries"], 250.5)
        self.assertEqual(latest["remaining"], 749.5)

    def test_duplicate_claim_number_rejected(self):
        treaty = self.create_treaty()
        payload = {"claim_number": "CLM-DUP", "recoverable_amount": 10.0}
        self.service.register_claim(CO, treaty["id"], payload)
        with self.assertRaises(Conflict):
            self.service.register_claim(CO, treaty["id"], payload)

    def test_every_change_keeps_snapshots_and_reopen_view(self):
        treaty = self.create_treaty()
        treaty = self.confirm(treaty["id"], "2026-03-01", "increase", 100.0, reason="加保原因")
        treaty = self.service.register_claim(CO, treaty["id"], {"claim_number": "CLM-A", "recoverable_amount": 50.0})
        treaty = self.confirm(treaty["id"], "2026-05-01", "decrease", 200.0, reason="减保原因")
        # 重新读取（模拟重开页面）
        reopened = self.service.get_treaty(UW2, treaty["id"])
        self.assertEqual(reopened["current_limit"], 900.0)
        self.assertEqual(reopened["used_approved_recoveries"], 50.0)
        self.assertEqual(reopened["remaining_capacity"], 850.0)
        kinds = [s["kind"] for s in reopened["snapshots"]]
        # initial + 每次确认产生候选快照与全部版本快照 + 赔案快照
        self.assertIn("initial", kinds)
        self.assertIn("claim_registered", kinds)
        self.assertEqual(kinds.count("endorsement_confirmed"), 2)
        self.assertEqual(kinds.count("limit_version"), 3)

    def test_duplicate_confirm_rejected(self):
        treaty = self.create_treaty()
        endo = self.service.register_endorsement(UW, treaty["id"], {
            "effective_date": "2026-03-01", "direction": "increase", "amount": 100.0, "reason": "x",
        })
        self.service.confirm_endorsement(UW, endo["id"])
        with self.assertRaises(Conflict):
            self.service.confirm_endorsement(UW, endo["id"])

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.service.create_treaty(UW, "T-X", {"name": "X", "initial_limit": 0})
        treaty = self.create_treaty("T-X")
        with self.assertRaises(ValidationError):
            self.service.register_endorsement(UW, treaty["id"], {
                "effective_date": "2026-03-01", "direction": "sideways", "amount": 10.0, "reason": "r",
            })
        with self.assertRaises(ValidationError):
            self.service.register_endorsement(UW, treaty["id"], {
                "effective_date": "03/01/2026", "direction": "increase", "amount": 10.0, "reason": "r",
            })
        with self.assertRaises(ValidationError):
            self.service.register_claim(CO, treaty["id"], {"claim_number": "C", "recoverable_amount": -1})

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_treaty(FIN, "T-1", {"name": "X", "initial_limit": 100.0})
        treaty = self.create_treaty("T-1")
        with self.assertRaises(PermissionDenied):
            self.service.register_endorsement(FIN, treaty["id"], {
                "effective_date": "2026-03-01", "direction": "increase", "amount": 10.0, "reason": "r",
            })
        with self.assertRaises(PermissionDenied):
            self.service.register_claim(FIN, treaty["id"], {"claim_number": "C", "recoverable_amount": 10.0})
        # admin 放行
        self.service.register_endorsement(ADMIN, treaty["id"], {
            "effective_date": "2026-03-01", "direction": "increase", "amount": 10.0, "reason": "r",
        })

    def test_duplicate_treaty_reference(self):
        self.create_treaty("DUP")
        with self.assertRaises(Conflict):
            self.create_treaty("DUP")

    def test_list_treaties_shows_capacity_summary(self):
        self.create_treaty("T-1")
        items = self.service.list_treaties(UW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["remaining_capacity"], 1000.0)
        self.assertEqual(items[0]["pending_endorsements"], 0)


if __name__ == "__main__":
    unittest.main()
