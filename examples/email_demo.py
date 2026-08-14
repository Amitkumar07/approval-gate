"""
email_demo.py
---------------
Shows EmailBackend end to end: a proposed action sends a real SMTP
email (to a tiny local fake SMTP server, so this runs with no mail
credentials) containing signed approve/reject links, and clicking the
approve link's confirm button resolves the pending action.

The fake SMTP server below is a minimal stand-in for python's removed
`smtpd` module (gone since 3.12) -- it just accepts the handful of
commands smtplib.SMTP sends for a plaintext send (EHLO/MAIL/RCPT/DATA/
QUIT) and prints the message body, which is where you'll find the
approve/reject links to click. Point EmailBackend at a real SMTP
provider (smtp_host/port/user/password) to actually send mail.

Run:
    python examples/email_demo.py
Then copy the "Approve:" link the fake server prints into a browser --
it's a real HTTP link served by EmailBackend's own listener.
"""

import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from approval_gate import ApprovalGate
from approval_gate.backends import EmailBackend

DB_PATH = str(Path(__file__).resolve().parent / "demo_audit.db")


class _FakeSMTPHandler(socketserver.StreamRequestHandler):
    """Just enough SMTP to accept a plaintext send from smtplib and
    print the message body -- not a real mail transfer agent."""

    def handle(self):
        self.wfile.write(b"220 localhost fake-smtp\r\n")
        in_data = False
        lines: list[bytes] = []
        while True:
            line = self.rfile.readline()
            if not line:
                break
            if in_data:
                if line.strip() == b".":
                    in_data = False
                    print("\n[fake SMTP server] message received:")
                    print(b"".join(lines).decode("utf-8", errors="replace"))
                    self.wfile.write(b"250 OK\r\n")
                    lines = []
                else:
                    lines.append(line)
                continue

            cmd = line.split(b" ")[0].strip().upper()
            if cmd == b"EHLO" or cmd == b"HELO":
                self.wfile.write(b"250 localhost\r\n")
            elif cmd in (b"MAIL", b"RCPT"):
                self.wfile.write(b"250 OK\r\n")
            elif cmd == b"DATA":
                self.wfile.write(b"354 End with <CRLF>.<CRLF>\r\n")
                in_data = True
            elif cmd == b"QUIT":
                self.wfile.write(b"221 Bye\r\n")
                break
            else:
                self.wfile.write(b"250 OK\r\n")


def start_fake_smtp():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeSMTPHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], server


if __name__ == "__main__":
    smtp_port, smtp_server = start_fake_smtp()

    backend = EmailBackend(
        smtp_host="127.0.0.1",
        smtp_port=smtp_port,
        smtp_user="",
        smtp_password="",
        from_addr="approval-gate@example.com",
        to_addr="reviewer@example.com",
        secret="demo-secret-change-me",
        public_base_url="http://127.0.0.1:8644",
        port=8644,
        use_tls=False,
    )
    gate = ApprovalGate(db_path=DB_PATH, backend=backend)

    print(f"EmailBackend listening at: {backend.url}")
    print("Sending a review request email now (to the fake SMTP server above)...\n")

    decision = gate.request_approval(
        action_name="delete_records",
        args={"table": "support_tickets", "filter": "status = 'closed' AND age_days > 365"},
        risk="high",
    )
    print(f"\nFinal decision: {'approved' if decision.approved else 'blocked'}")

    backend.shutdown()
    smtp_server.shutdown()
