"""Four acceptance tests against real HTTP servers and temporary SQLite files."""

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import ROOT, Server, Store


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="traffic-light-test-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "warehouse.sqlite3"
        self.server = Server(("127.0.0.1", 0), Store(self.db))
        self.server.RequestHandlerClass.log_message = lambda *args: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def request(self, path, body=None, base=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request((base or self.base) + path, data=data,
                      headers=headers or ({"Content-Type": "application/json"} if data else {}))
        try:
            with urlopen(req, timeout=20) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            with response:
                return response.code, json.load(response)

    def state(self, base=None):
        status, payload = self.request("/api/state", base=base)
        self.assertEqual(status, 200)
        return payload["state"]

    @staticmethod
    def proof(task):
        return dict(expected_revision=task["revision"], expected_fingerprint=task["fingerprint"])

    def act(self, action, task, extras=None, base=None):
        body = self.proof(task)
        body.update(extras or {})
        return self.request(f"/api/proposals/{task['id']}/{action}", body, base=base)

    def assert_unmoved(self, state):
        self.assertEqual(state["warehouse"]["bins"], {"A": 1, "B": 0})
        self.assertEqual(state["warehouse"]["quantity"], 1)
        self.assertEqual(state["movement_count"], 0)

    def start_process(self):
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "app.py"), "--db", str(self.db), "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )

        def stop():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            process.stdout.close()

        self.addCleanup(stop)
        first_line = process.stdout.readline().strip()
        self.assertTrue(first_line.startswith("Agent Traffic Light: http://"), first_line)
        return process, first_line.removeprefix("Agent Traffic Light: ")

    def test_01_unapproved_and_rejected_never_move(self):
        """Pending and rejected tasks cannot execute, even through direct API calls."""
        task = self.state()["tasks"][0]
        self.assertIn("非即時 AI", task["origin"])
        self.assertTrue(task["id"])
        status, result = self.act("execute", task)
        self.assertEqual(status, 409)
        self.assertEqual(result["result"]["code"], "NOT_APPROVED")
        self.assert_unmoved(result["state"])
        status, result = self.act("reject", task)
        self.assertEqual(status, 200)
        self.assertEqual(result["state"]["tasks"][0]["status"], "rejected")
        self.assert_unmoved(result["state"])
        for action in ("execute", "approve", "reject"):
            with self.subTest(action=action):
                status, result = self.act(action, task)
                self.assertEqual(status, 409)
                self.assert_unmoved(result["state"])
        status, result = self.act("edit", task, dict(source="B", destination="A", item="BOX-001", quantity=1))
        self.assertEqual(status, 409)
        self.assert_unmoved(result["state"])
        event = next(e for e in self.state()["events"] if e["action"] == "rejected")
        self.assertEqual((event["before_slot"], event["after_slot"]), ("A", "A"))
        status, result = self.request("/api/proposals", {})
        self.assertEqual(status, 200)
        self.assertNotEqual(result["result"]["task_id"], task["id"])

    def test_02_approval_binds_exact_content_and_revision(self):
        """Editing invalidates approval; stale clients and changed-back versions are blocked."""
        original = self.state()["tasks"][0]
        status, payload = self.act("approve", original)
        self.assertEqual(status, 200)
        self.assert_unmoved(payload["state"])
        status, edited = self.act("edit", original, dict(source="B", destination="A", item="BOX-001", quantity=1))
        self.assertEqual(status, 200)
        v2 = edited["state"]["tasks"][0]
        self.assertEqual(v2["id"], original["id"])
        self.assertEqual(v2["revision"], 2)
        self.assertEqual(v2["status"], "pending")
        self.assertIsNone(v2["approved_fingerprint"])
        self.assertNotEqual(v2["fingerprint"], original["fingerprint"])
        for action in ("approve", "execute", "reject"):
            status, result = self.act(action, original)
            self.assertEqual(status, 409)
            self.assertEqual(result["result"]["code"], "STALE_CONTENT")
        self.assertEqual(self.act("execute", v2)[1]["result"]["code"], "NOT_APPROVED")
        status, result = self.act("edit", v2, dict(source="A", destination="B", item="BOX-001", quantity=1))
        self.assertEqual(status, 200)
        v3 = result["state"]["tasks"][0]
        self.assertEqual(v3["revision"], 3)
        self.assertNotEqual(v3["fingerprint"], original["fingerprint"])
        self.assertEqual(self.act("execute", original)[0], 409)
        self.assertEqual(self.act("execute", v3)[0], 409)
        for extra in ({"quantity": 2}, {"source": "B"}, {"item": "OTHER"}):
            self.assertEqual(self.act("approve", v3, extra)[0], 400)
        for quantity in (0, 2, True, 1.0):
            status, _ = self.act("edit", v3, dict(source="B", destination="A", item="BOX-001", quantity=quantity))
            self.assertEqual(status, 422)
        self.assert_unmoved(self.state())
        self.assertEqual(self.act("approve", v3)[0], 200)
        status, result = self.act("execute", v3)
        self.assertEqual(status, 200)
        self.assertTrue(result["result"]["moved"])
        self.assertEqual(result["state"]["movement_count"], 1)

    def test_03_execute_checks_inventory_and_persists_result(self):
        """Approval does not move; execution moves once and stale inventory cannot move."""
        first = self.state()["tasks"][0]
        _, created = self.request("/api/proposals", {})
        second = created["state"]["tasks"][0]
        for task in (first, second):
            self.assertEqual(self.act("approve", task)[0], 200)
        self.assert_unmoved(self.state())
        status, result = self.act("execute", first)
        self.assertEqual(status, 200)
        self.assertEqual(result["state"]["warehouse"]["bins"], {"A": 0, "B": 1})
        self.assertEqual(result["state"]["movement_count"], 1)
        status, blocked = self.act("execute", second)
        self.assertEqual(status, 409)
        self.assertEqual(blocked["result"]["code"], "SOURCE_EMPTY")
        self.assertEqual(blocked["state"]["movement_count"], 1)
        persisted = Store(self.db).snapshot()
        self.assertEqual(persisted["warehouse"], blocked["state"]["warehouse"])
        self.assertEqual(persisted["events"], blocked["state"]["events"])
        self.assertEqual(self.act("edit", first, dict(source="B", destination="A", item="BOX-001", quantity=1))[0], 409)
        receipt = next(t for t in persisted["tasks"] if t["id"] == first["id"])["movement"]
        self.assertEqual(receipt["fingerprint"], first["fingerprint"])
        self.assertEqual(receipt["quantity"], 1)
        # An external website cannot submit a local mutation.
        status, _ = self.request("/api/proposals", {}, headers={"Content-Type": "application/json", "Origin": "https://example.com"})
        self.assertEqual(status, 403)

    def test_04_parallel_replays_across_processes_and_restart(self):
        """20 concurrent HTTP requests across two processes yield exactly one movement."""
        task = self.state()["tasks"][0]
        self.assertEqual(self.act("approve", task)[0], 200)
        process, other_base = self.start_process()
        barrier = threading.Barrier(20)

        def execute(index):
            barrier.wait(timeout=15)
            return self.act("execute", task, base=self.base if index % 2 else other_base)

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(execute, range(20)))
        self.assertTrue(all(status == 200 for status, _ in responses))
        self.assertEqual(sum(result["result"]["moved"] for _, result in responses), 1)
        self.assertEqual(len({r["result"]["movement"]["id"] for _, r in responses}), 1)
        state = self.state()
        self.assertEqual(state["movement_count"], 1)
        self.assertEqual(state["warehouse"]["bins"], {"A": 0, "B": 1})
        self.assertEqual(sum(e["action"] == "executed" for e in state["events"]), 1)
        process.terminate()
        process.wait(timeout=10)
        _, restarted_base = self.start_process()
        self.assertEqual(self.state(restarted_base)["events"], state["events"])
        status, replay = self.act("execute", task, base=restarted_base)
        self.assertEqual(status, 200)
        self.assertEqual(replay["result"]["code"], "ALREADY_PROCESSED")
        self.assertFalse(replay["result"]["moved"])
        self.assertEqual(replay["state"]["movement_count"], 1)
        self.assertEqual(replay["state"]["warehouse"]["bins"], {"A": 0, "B": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
