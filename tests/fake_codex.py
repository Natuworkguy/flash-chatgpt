"""A stand-in for `codex app-server`, for the tests.

It answers what provider.py asks, as the real one does, from a scenario
in the JSON file FAKE_CODEX names, and writes every message it gets to
the file FAKE_CODEX_LOG, a line each.

Scenario keys: account, limits, models, events (sent once a turn starts:
notifications, or requests of its own when they carry an "id").
"""

import json
import os
import sys

with open(os.environ["FAKE_CODEX"], encoding="utf-8") as given:
    scenario = json.load(given)
log = open(os.environ["FAKE_CODEX_LOG"], "a", encoding="utf-8")  # noqa: SIM115


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def turn_completed(status="completed", error=None):
    send({"method": "turn/completed", "params": {
        "threadId": "t1",
        "turn": {"id": "u1", "items": [], "status": status,
                 "error": error}}})


for line in sys.stdin:
    message = json.loads(line)
    log.write(json.dumps(message) + "\n")
    log.flush()
    method = message.get("method")
    if "id" not in message or not method:
        continue  # a notification, or an answer to a request of ours
    answer = {
        "initialize": {"userAgent": "fake", "codexHome": "/tmp"},
        "account/read": {"account": scenario.get("account"),
                         "requiresOpenaiAuth": True},
        "account/rateLimits/read": scenario.get("limits") or {
            "rateLimits": {}, "ordinaryUsageAllowed": True},
        "model/list": {"data": scenario.get("models") or [],
                       "nextCursor": None},
        "thread/start": {"thread": {"id": "t1"}, "model": "m"},
        "thread/inject_items": {},
        "turn/start": {"turn": {"id": "u1", "items": [],
                                "status": "inProgress"}},
        "turn/interrupt": {},
    }.get(method)
    if answer is None:
        send({"id": message["id"],
              "error": {"code": -32601, "message": f"no {method}"}})
        continue
    send({"id": message["id"], "result": answer})
    if method == "turn/start":
        send({"method": "turn/started", "params": {
            "threadId": "t1", "turn": {"id": "u1", "items": [],
                                       "status": "inProgress"}}})
        for event in scenario.get("events") or []:
            send(event)
    elif method == "turn/interrupt":
        turn_completed("interrupted")
