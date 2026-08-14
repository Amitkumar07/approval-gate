"""
notifier_demo.py
------------------
Shows WebBackend pinging a notifier the moment an action needs review,
instead of relying on someone having the inbox open. Uses a real
SlackNotifier pointed at a fake local "Slack" server (prints whatever
it receives) so this is runnable with no real webhook configured --
swap `fake_slack_url` for a real https://hooks.slack.com/... URL to
actually post to Slack.

Run:
    python examples/notifier_demo.py
Then open the printed inbox URL and approve/reject the action -- watch
the terminal for the "incoming Slack message" the fake server prints
the instant the action is queued, before you've touched the browser.
"""

import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import WebBackend
from approval_gate.notifiers import SlackNotifier

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")


def start_fake_slack():
    """A stand-in for a real Slack incoming webhook -- just prints what it gets."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            print("\n[fake Slack webhook received]")
            print(payload["text"])
            print()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/webhook", server


if __name__ == "__main__":
    fake_slack_url, slack_server = start_fake_slack()

    backend = WebBackend(port=8642, notifier=SlackNotifier(webhook_url=fake_slack_url))
    gate = ApprovalGate(db_path=DB_PATH, backend=backend)

    print(f"Review inbox running at: {backend.url}")
    print("(Using a fake local Slack webhook for this demo -- see the source")
    print(" to point SlackNotifier at a real https://hooks.slack.com/... URL.)\n")
    try:
        webbrowser.open(backend.url)
    except Exception:
        pass

    decision = gate.request_approval(
        action_name="delete_records",
        args={"table": "support_tickets", "filter": "status = 'closed' AND age_days > 365"},
        risk="high",
    )
    print("Decision:", "approved" if decision.approved else f"blocked ({decision.reason})")

    backend.shutdown()
    slack_server.shutdown()
