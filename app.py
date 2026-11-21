"""CLI and a read-only local preview for the deterministic memory experiment."""

import argparse
import json
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from scenarios import run_demo
from sim import validate_record


def render(report):
    attempts = report["memory"]["problems"][0]["attempts"]
    rows = "".join(f"<tr><td>{escape(r['task'])}</td><td>{escape(r['decision']['reason'])}</td>"
                   f"<td>{r['executed_receipts']} / {r['successful_receipts']}</td>"
                   f"<td>{'PASS' if r['passed'] else 'FAIL'}</td></tr>" for r in report["scenarios"])
    scores = "".join(f"<li>{escape(a['model'])} / {escape(a['benchmark'])}: "
                     f"<strong>{a['passed']}/{a['total']}</strong>; "
                     f"missed: {escape(', '.join(a['missed']))}; selected: {a['accepted']}</li>"
                     for a in attempts)
    record = escape(json.dumps(report["memory"], indent=2))
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Self-Improvement Memory</title><style>
body{{font:17px/1.55 system-ui;max-width:1080px;margin:36px auto;padding:0 24px;color:#182635;background:#f5f8fb}}
h1{{font-size:2.3rem;margin-bottom:8px}}p{{max-width:850px}}table{{border-collapse:collapse;width:100%;background:white}}
th,td{{padding:12px;text-align:left;border-bottom:1px solid #ccd6e0}}th{{background:#173b5a;color:white}}
pre{{background:#102b43;color:#e2f4ff;padding:20px;overflow:auto;font-size:13px}}a{{color:#12639c}}
.flow{{padding:18px;background:#dfedf6;border-left:5px solid #13668b}}@media(max-width:700px){{table{{font-size:13px}}th,td{{padding:5px}}}}
</style><h1>Self-Improvement Memory</h1>
<p>Keep the reason for a harness edit, its predictions, and its measured outcomes together when the model or benchmark changes.</p>
<p class="flow">Propose edit → save predictions → evaluate local fixtures → record successes and failures → carry history into a new setting</p>
<h2>Completion evidence</h2><p>Receipts count operations that actually ran; successful receipts count passing operations. The bounded gate intentionally permits completion after three rejections. That bypass fails the evidence contract.</p>
<table><thead><tr><th>Scenario</th><th>Gate decision</th><th>Executed / successful</th><th>Contract</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Source and target attempts</h2><ul>{scores}</ul>
<p>The target fixture adds a semantic correctness requirement the inherited gate cannot verify. Failure stays in the history; selection is recorded separately from the score. These are deterministic fixtures, not model benchmarks.</p>
<p><a href="/record.json">Complete experiment JSON</a> · <a href="https://arxiv.org/html/2611.18856v1#A4">Paper record format</a></p>
<details><summary>Inspect the validated memory record</summary><pre>{record}</pre></details></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            payload, kind, status = b'{"status":"ok"}', "application/json", 200
        elif path in ("/", "/record.json"):
            report = run_demo()
            payload = (render(report) if path == "/" else json.dumps(report, indent=2)).encode("utf-8")
            kind, status = ("text/html; charset=utf-8" if path == "/" else "application/json"), 200
        elif path == "/favicon.ico":
            payload, kind, status = b"", "image/x-icon", 204
        else:
            payload, kind, status = b"Not found", "text/plain", 404
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser(description="Run an execution-evidence and improvement-memory lab")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="Print the experiment as JSON")
    validate = sub.add_parser("validate", help="Validate a saved memory object (not the whole report)")
    validate.add_argument("path")
    serve = sub.add_parser("serve", help="Start the loopback-only read-only preview")
    serve.add_argument("--port", type=int, default=8883)
    args = parser.parse_args()
    if args.command == "demo":
        print(json.dumps(run_demo(), indent=2))
    elif args.command == "validate":
        try:
            with open(args.path, encoding="utf-8") as handle:
                record = validate_record(json.load(handle))
        except (ValueError, OSError) as exc:
            parser.exit(2, f"Invalid record: {exc}\n")
        print(f"Valid: {len(record['problems'])} problem(s)")
    else:
        if not 1 <= args.port <= 65535:
            parser.error("port must be in 1..65535")
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(f"Preview: http://127.0.0.1:{args.port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
