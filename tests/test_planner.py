"""Regression tests for the planner rules, service and A2A endpoint.

Run:  python -m unittest discover -s tests -v
(stdlib only - no pytest/httpx needed)
"""
import asyncio
import base64
import json
import unittest

from app.main import app, planner, repo
from app.planner import intents, rules


def call(method: str, path: str, body=None) -> tuple[int, bytes]:
    """Invoke the ASGI app in-process."""
    raw = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or b"")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "path": path, "raw_path": path.encode(), "query_string": b"", "scheme": "http",
             "headers": [(b"content-type", b"application/json")], "server": ("test", 80), "client": ("t", 1)}
    out = {"status": 0, "body": b""}
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
        elif msg["type"] == "http.response.body":
            out["body"] += msg.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["body"]


def rpc(method: str, params: dict, req_id=1) -> dict:
    _, body = call("POST", "/a2a/planner", {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
    return json.loads(body)


def text_message(text: str) -> dict:
    return {"message": {"kind": "message", "role": "user", "messageId": "t", "parts": [{"kind": "text", "text": text}]}}


class RuleTests(unittest.TestCase):
    def test_aging_threshold_is_exclusive(self):
        self.assertFalse(rules.is_aged(14, 14))
        self.assertTrue(rules.is_aged(15, 14))

    def test_material_status(self):
        self.assertEqual(rules.material_status(10, None)[0], rules.STATUS_NON_CONTROLLED)
        self.assertEqual(rules.material_status(10, 10), (rules.STATUS_AVAILABLE, 0))
        # Tech spec §8 sample: 1.72 unrestricted vs 679.075 controlled -> -677.355 over-control
        self.assertEqual(rules.material_status(1.72, 679.075), (rules.STATUS_OVER_CONTROL, -677.355))

    def test_over_issue_classification(self):
        base = {"RemainingIssueQty": -19.27, "ExcludeReason": "", "NetGI_Other": 50}
        self.assertEqual(rules.over_issued_qty(base), 19.27)
        self.assertTrue(rules.classify_over_issue(base).startswith("Over-control"))
        self.assertEqual(rules.classify_over_issue({**base, "NetGI_Other": 0}), "Over-issued")
        self.assertEqual(rules.over_issued_qty({**base, "ExcludeReason": "Bulk material"}), 0)


class ServiceTests(unittest.TestCase):
    def test_work_order_121060001(self):
        s = planner.service.work_order_shortage("121060001")
        short = {c["material"] for c in s.data["components"] if c["isShort"]}
        self.assertEqual(short, {"MPAZ68040AP-B", "MTINS29300"})
        mpaz = next(c for c in s.data["components"] if c["material"] == "MPAZ68040AP-B")
        self.assertIn("expedite", mpaz["rootCause"])
        self.assertTrue(mpaz["isAged"])

    def test_unknown_order(self):
        self.assertEqual(planner.service.work_order_shortage("999999999").result_type, "not_found")

    def test_aged_only_above_threshold(self):
        s = planner.service.aged_materials()
        self.assertTrue(s.rows)
        self.assertTrue(all(r["AgingDays"] > 14 for r in s.rows))

    def test_shortage_overview_counts_match_source(self):
        expected = sum(1 for wo in repo.shortage_by_order.values()
                       if any(c["ShortageQty"] > 0 for c in wo["to_ComponentShortage"]["results"]))
        self.assertEqual(len(planner.service.shortage_overview().rows), expected)


class IntentTests(unittest.TestCase):
    def test_entities(self):
        q = intents.parse("What is short for work order 121060001, and what is aged? MRP ZMA in A301", repo)
        self.assertEqual(q.work_orders, ["121060001"])
        self.assertEqual(q.mrp_controllers, ["ZMA"])
        self.assertEqual(q.storage_locations, ["A301"])
        self.assertIn("shortage", q.intents)
        self.assertIn("aged", q.intents)

    def test_missing_parts_question(self):
        q = intents.parse("Is anything missing for 121060003?", repo)
        self.assertEqual(q.work_orders, ["121060003"])
        self.assertIn("shortage", q.intents)


class A2ATests(unittest.TestCase):
    def test_agent_card(self):
        status, body = call("GET", "/a2a/planner/.well-known/agent-card.json")
        card = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(card["url"].rsplit("/", 2)[-2:], ["a2a", "planner"])
        self.assertIn("work_order_shortage", [s["id"] for s in card["skills"]])

    def test_message_send_returns_task_with_backend_data(self):
        res = rpc("message/send", text_message("What is short for work order 121060001, and what is aged? csv"))["result"]
        self.assertEqual(res["kind"], "task")
        self.assertEqual(res["status"]["state"], "completed")
        text = res["status"]["message"]["parts"][0]["text"]
        self.assertIn("MTINS29300", text)
        self.assertIn("Data as of 2026-10-29", text)
        data = res["artifacts"][1]["parts"][0]["data"]
        self.assertEqual(data["results"][0]["skill"], "work_order_shortage")
        csv_file = res["artifacts"][2]["parts"][0]["file"]
        self.assertTrue(base64.b64decode(csv_file["bytes"]).decode("utf-8-sig").startswith("WorkOrder,"))

        got = rpc("tasks/get", {"id": res["id"]})["result"]
        self.assertEqual(got["id"], res["id"])

    def test_structured_skill_call(self):
        params = {"message": {"role": "user", "messageId": "d", "parts": [
            {"kind": "data", "data": {"skill": "production_order", "workOrder": "121060010"}}]}}
        res = rpc("message/send", params)["result"]
        self.assertEqual(res["artifacts"][1]["parts"][0]["data"]["results"][0]["data"]["header"]["orderType"], "ZRWC")

    def test_errors(self):
        self.assertEqual(rpc("nope", {})["error"]["code"], -32601)
        self.assertEqual(rpc("message/send", {})["error"]["code"], -32602)
        self.assertEqual(rpc("tasks/get", {"id": "missing"})["error"]["code"], -32001)
        _, body = call("POST", "/a2a/planner", b"not json")
        self.assertEqual(json.loads(body)["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
