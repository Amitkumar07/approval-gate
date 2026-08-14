"""
webhook_demo.py
-----------------
Shows WebhookBackend end to end: a proposed action gets POSTed to a
"your system" endpoint (here, a tiny stand-in that just prints what it
receives and immediately posts back an approval, simulating an
internal admin tool), and the decision flows back through POST /decide.

In a real integration, `your_system_receiver` would hand the payload to
whatever you already use (a ticket queue, an internal dashboard) and a
human would decide there -- possibly minutes or hours later, from a
completely different process. The call to `callback_url` is the only
thing that has to happen for approval-gate to resume; the demo does it
inline for a runnable example.

Run:
    python examples/webhook_demo.py
"""

import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import WebhookBackend

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")


def start_your_system():
    """Stands in for whatever internal tool you'd actually wire up --
    receives the pending action, decides (here: always approves after
    printing it), and calls back with the decision."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            pending = json.loads(self.rfile.read(length))
            print("[your-system] received pending action:")
            print(f"  action: {pending['action']}   risk: {pending['risk']}")
            print(f"  args: {pending['args']}")
            self.send_response(200)
            self.end_headers()

            print("[your-system] auto-approving for this demo, calling back...")
            body = json.dumps({"audit_id": pending["audit_id"], "decision": "approve", "by": "internal-admin-tool"}).encode()
            req = urllib.request.Request(
                pending["callback_url"], data=body, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req).read()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/incoming", server


if __name__ == "__main__":
    your_system_url, your_system_server = start_your_system()

    backend = WebhookBackend(notify_url=your_system_url, port=8643)
    gate = ApprovalGate(db_path=DB_PATH, backend=backend)

    print(f"WebhookBackend listening at: {backend.url}")
    print(f"Notifying 'your system' at: {your_system_url}\n")

    decision = gate.request_approval(
        action_name="rotate_api_key",
        args={"service": "payments-gateway", "current_key": "sk-liveAAAAAAAAAAAAAAAAAAAAAAAA"},
        risk="high",
    )
    print(f"\nFinal decision: {'approved' if decision.approved else 'blocked'} (by webhook callback)")

    backend.shutdown()
    your_system_server.shutdown()
