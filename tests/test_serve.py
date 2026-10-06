import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_events", ROOT / "fetch-events.py")
fetch_events = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_events)
# Keep the sync cache out of the real ~/.local/state, and the keyring off:
# a test must never write to the desktop keyring.
fetch_events.SYNC_CACHE_DIR = tempfile.mkdtemp(prefix="chronica-test-cache-")
fetch_events._secret_tool = lambda args, value=None: None


class ServeTests(unittest.TestCase):
    def run_serve(self, *lines):
        out = io.StringIO()
        with mock.patch.object(fetch_events, "ensure_config_exists"), \
             mock.patch.object(fetch_events.os, "makedirs"):
            fetch_events.serve(io.StringIO("".join(line + "\n" for line in lines)), out)
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_answers_each_request_in_order_with_its_id(self):
        calls = []
        with mock.patch.dict(fetch_events.SERVE_COMMANDS, {
            "sync": lambda payload: calls.append("sync") or {"status": "success", "totalEvents": 3},
            "create-event": fetch_events._event_command(lambda p: calls.append(p["title"]) or {"status": "success"}),
        }):
            answers = self.run_serve(
                json.dumps({"id": 1, "cmd": "create-event", "payload": {"title": "A"}}),
                json.dumps({"id": 2, "cmd": "sync"}),
            )
        self.assertEqual(calls, ["A", "sync"])
        self.assertEqual([a["id"] for a in answers], [1, 2])
        self.assertEqual(answers[1]["result"]["totalEvents"], 3)

    def test_a_bad_request_gets_an_error_and_the_loop_goes_on(self):
        with mock.patch.dict(fetch_events.SERVE_COMMANDS, {"sync": lambda payload: {"status": "success"}}):
            answers = self.run_serve(
                "not json",
                json.dumps({"id": 2, "cmd": "nope"}),
                json.dumps({"id": 3, "cmd": "create-event", "payload": ["not", "a", "dict"]}),
                json.dumps({"id": 4, "cmd": "sync"}),
            )
        self.assertEqual([a["result"]["status"] for a in answers], ["error", "error", "error", "success"])
        self.assertIn("Unknown command", answers[1]["result"]["message"])
        self.assertEqual(answers[3]["id"], 4)


if __name__ == "__main__":
    unittest.main()
