import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app import build_service
from src.http_api import create_server


class HttpLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http.db"))
        self.server = create_server("127.0.0.1", 0, service, Path(__file__).resolve().parent.parent / "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def _request(self, method, path, body=None, role="admin"):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-User-Id": "tester", "X-Role": role},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_ledger_capacity_shortfall_http_details(self):
        status, treaty = self._request("POST", "/api/treaties", {
            "reference": "HTTP-01",
            "data": {"name": "HTTP合约", "initial_limit": 1000.0, "inception_date": "2026-01-01"},
        })
        self.assertEqual(status, 201)
        treaty_id = treaty["id"]

        status, claim = self._request("POST", "/api/treaties/%s/claims" % treaty_id, {
            "data": {"claim_number": "HCLM-1", "approved_date": "2026-05-01", "recoverable_amount": 800.0},
        }, role="claims_officer")
        self.assertEqual(status, 201)

        status, endo = self._request("POST", "/api/treaties/%s/endorsements" % treaty_id, {
            "data": {"effective_date": "2026-06-01", "direction": "decrease", "amount": 300.0, "reason": "减保"},
        })
        self.assertEqual(status, 201)

        status, error = self._request("POST", "/api/endorsements/%s/confirm" % endo["id"], {})
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "conflict")
        details = error["details"]
        self.assertEqual(details["type"], "capacity_shortfall")
        self.assertEqual(details["shortfall"], 100.0)
        self.assertEqual(details["occupancies"][0]["claim_number"], "HCLM-1")

        status, view = self._request("GET", "/api/treaties/%s" % treaty_id)
        self.assertEqual(status, 200)
        self.assertEqual(view["current_limit"], 1000.0)
        self.assertEqual(view["used_approved_recoveries"], 800.0)
        self.assertEqual(view["remaining_capacity"], 200.0)
        self.assertEqual(view["endorsements"][0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
