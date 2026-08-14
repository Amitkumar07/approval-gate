"""
web.py
------
A real review inbox in a browser tab instead of a blocking terminal
`input()` prompt -- the single biggest gap between "works" and "something
a stranger would actually use in production."

Design: `wait_for_decision` has to block the calling thread synchronously
(same contract as every other backend), while a human reviews and decides
from a browser, possibly minutes later. So this runs a tiny HTTP server
(stdlib only -- no new dependency) on a background thread, and bridges
"browser submits a decision" back to "the blocked Python thread" with a
per-pending `queue.Queue`. The page itself polls `/api/pending` every
couple seconds; there's no websocket/SSE machinery because a polling
loop is simpler, has nothing to reconnect, and the latency doesn't
matter for a human clicking a button.

Usage:

    from approval_gate import ApprovalGate
    from approval_gate.backends import WebBackend

    backend = WebBackend(port=8642)
    gate = ApprovalGate(db_path="audit.db", backend=backend)
    print(f"Review inbox running at {backend.url}")
    ...
    backend.shutdown()  # when your program is done

Multiple pending actions queue up and are all shown in the inbox at
once; each is decided independently by its own audit_id.

Pass `notifier=` to get pinged (Slack, email, anything) the moment an
action needs review, instead of relying on someone to have the inbox
open. See notifiers.py.

    from approval_gate.notifiers import SlackNotifier

    backend = WebBackend(port=8642, notifier=SlackNotifier(webhook_url="https://hooks.slack.com/..."))

The page itself (_PAGE below) is a self-contained HTML string -- no
build step, no external assets, so it renders instantly over a raw
socket and has nothing that can go stale between the Python version and
the frontend. It supports light/dark automatically (prefers-color-scheme)
plus an explicit override via `data-theme`. Risk is encoded both as a
color and as a left-edge stripe on each card, not color alone, and a
"reviewing as" field replaces what used to be a hardcoded "web-reviewer"
string in every decision.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ..notifiers import Notifier, safe_notify

from .base import Backend
from ._queue_base import _PendingQueueBackend

_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>approval-gate &middot; control</title>
<style>
  :root {
    --bg: #f1f1ee;
    --surface: #ffffff;
    --surface-sunken: #e8e8e4;
    --border: #dbdbd5;
    --border-strong: #c6c6be;
    --text: #17181a;
    --text-muted: #5c5d5f;
    --text-faint: #93938e;
    --accent: #ff4d1c;
    --accent-text: #17181a;
    --accent-dim: #ffe4d8;
    --focus-ring: #ff4d1c;

    --risk-low-fg: #3d7a4f;
    --risk-low-bg: #e2eee3;
    --risk-medium-fg: #9a6a11;
    --risk-medium-bg: #f4e8d3;
    --risk-high-fg: #b0392b;
    --risk-high-bg: #f5ddd7;

    --danger: #b0392b;
    --danger-bg: #f5ddd7;
    --danger-text: #ffffff;

    --font-display: -apple-system, "SF Pro Display", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    --font-ui: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --font-mono: ui-monospace, "SF Mono", "Cascadia Code", "Roboto Mono", Menlo, Consolas, monospace;
  }

  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0c0d0e;
      --surface: #17181a;
      --surface-sunken: #1e1f22;
      --border: #2b2c2f;
      --border-strong: #3c3d40;
      --text: #eeeeec;
      --text-muted: #a3a3a0;
      --text-faint: #6d6e6c;
      --accent: #ff6a3d;
      --accent-text: #0c0d0e;
      --accent-dim: #3a1f14;
      --focus-ring: #ff6a3d;

      --risk-low-fg: #79c78d;
      --risk-low-bg: #16261b;
      --risk-medium-fg: #e0ac4f;
      --risk-medium-bg: #2c2210;
      --risk-high-fg: #ea8a78;
      --risk-high-bg: #2e1a16;

      --danger: #ea8a78;
      --danger-bg: #2e1a16;
      --danger-text: #17100e;
    }
  }

  :root[data-theme="dark"] {
    --bg: #0c0d0e;
    --surface: #17181a;
    --surface-sunken: #1e1f22;
    --border: #2b2c2f;
    --border-strong: #3c3d40;
    --text: #eeeeec;
    --text-muted: #a3a3a0;
    --text-faint: #6d6e6c;
    --accent: #ff6a3d;
    --accent-text: #0c0d0e;
    --accent-dim: #3a1f14;
    --focus-ring: #ff6a3d;

    --risk-low-fg: #79c78d;
    --risk-low-bg: #16261b;
    --risk-medium-fg: #e0ac4f;
    --risk-medium-bg: #2c2210;
    --risk-high-fg: #ea8a78;
    --risk-high-bg: #2e1a16;

    --danger: #ea8a78;
    --danger-bg: #2e1a16;
    --danger-text: #17100e;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 14.5px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  .shell {
    max-width: 720px;
    margin: 0 auto;
    padding: 0 1.25rem 6rem;
  }

  /* ---- status hero: the page's thesis is "something is paused, waiting on you" ---- */

  .hero {
    padding: 2.25rem 0 1.75rem;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--border);
    margin-bottom: 1.5rem;
  }

  .hero-left {
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }

  .live-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .live-dot {
    position: relative;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--text-faint);
    flex: none;
  }

  .live-dot.active {
    background: var(--accent);
  }

  .live-dot.active::after {
    content: "";
    position: absolute;
    inset: -5px;
    border-radius: 50%;
    border: 1px solid var(--accent);
    animation: pulse 2s ease-out infinite;
  }

  @keyframes pulse {
    0% { transform: scale(0.6); opacity: 0.7; }
    100% { transform: scale(1.9); opacity: 0; }
  }

  .live-label {
    font-family: var(--font-mono);
    font-size: 0.72rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-faint);
  }

  .live-label.active { color: var(--accent); }

  .hero-count {
    font-family: var(--font-display);
    font-weight: 700;
    font-size: 2.6rem;
    line-height: 1;
    letter-spacing: -0.03em;
    font-variant-numeric: tabular-nums;
    text-wrap: balance;
  }

  .hero-count .unit {
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text-muted);
    margin-left: 0.5rem;
    letter-spacing: -0.01em;
  }

  .hero-sub {
    color: var(--text-muted);
    font-size: 0.85rem;
    max-width: 30ch;
  }

  .hero-right {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 0.6rem;
    flex: none;
  }

  .brand {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--text-faint);
  }

  .brand .glyph {
    color: var(--accent);
    font-weight: 700;
  }

  .reviewer-field {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.7rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
  }

  .reviewer-field label {
    font-size: 0.7rem;
    color: var(--text-faint);
    white-space: nowrap;
  }

  .reviewer-field input {
    width: 8rem;
    border: none;
    background: transparent;
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 0.78rem;
    padding: 0.1rem 0;
    text-align: right;
  }

  .reviewer-field input:focus { outline: none; }

  /* ---- queue: a tabular row list, expand-in-place like an email inbox ---- */

  .list {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }

  .list-head {
    display: grid;
    grid-template-columns: 20px minmax(0, 1fr) 84px 90px 64px;
    gap: 0.6rem;
    padding: 0.5rem 0.9rem;
    background: var(--surface-sunken);
    border-bottom: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: 0.65rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-faint);
  }

  .list-head span:nth-child(3),
  .list-head span:nth-child(4) { text-align: right; }

  .empty {
    text-align: center;
    padding: 4.5rem 1rem 3rem;
    color: var(--text-faint);
  }

  .empty .glyph {
    font-family: var(--font-mono);
    font-size: 1.8rem;
    color: var(--risk-low-fg);
    margin-bottom: 0.9rem;
  }

  .empty .title {
    font-family: var(--font-display);
    color: var(--text);
    font-size: 1.05rem;
    font-weight: 700;
    margin-bottom: 0.3rem;
    letter-spacing: -0.01em;
  }

  .empty .hint {
    font-size: 0.82rem;
    max-width: 32ch;
    margin: 0 auto;
  }

  .row-wrap { border-bottom: 1px solid var(--border); }
  .row-wrap:last-child { border-bottom: none; }

  /* button.row (not just .row) so this beats the generic `button {}`
     rule declared later in the cascade -- both are single-class
     specificity, so source order would otherwise let button {} win. */
  button.row {
    display: grid;
    grid-template-columns: 20px minmax(0, 1fr) 84px 90px 64px;
    align-items: center;
    gap: 0.6rem;
    width: 100%;
    padding: 0.65rem 0.9rem;
    background: none;
    border: none;
    border-radius: 0;
    text-align: left;
    cursor: pointer;
    font-family: inherit;
    font-size: inherit;
    font-weight: inherit;
    color: inherit;
    transition: background-color 0.1s ease;
  }

  button.row:hover { background: var(--surface-sunken); filter: none; }
  button.row:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
  .row-wrap.expanded button.row { background: var(--surface-sunken); }

  .chevron {
    font-size: 0.6rem;
    color: var(--text-faint);
    transition: transform 0.15s ease;
    flex: none;
  }

  .row-wrap.expanded .chevron { transform: rotate(90deg); color: var(--accent); }

  .row-main { min-width: 0; display: flex; align-items: baseline; gap: 0.6rem; }

  .row-action {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .row-preview {
    font-size: 0.78rem;
    color: var(--text-faint);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }

  .row-findings {
    text-align: right;
    font-family: var(--font-mono);
    font-size: 0.72rem;
    white-space: nowrap;
  }

  .row-findings.has-findings { color: var(--risk-high-fg); font-weight: 600; }
  .row-findings.none { color: var(--text-faint); }

  .row-waiting {
    text-align: right;
    font-family: var(--font-mono);
    font-size: 0.72rem;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .row-risk { display: flex; justify-content: flex-end; }

  .badge {
    display: inline-flex;
    align-items: center;
    font-family: var(--font-mono);
    font-size: 0.62rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.15rem 0.45rem;
    border-radius: 4px;
    white-space: nowrap;
  }

  .badge.risk-low { background: var(--risk-low-bg); color: var(--risk-low-fg); }
  .badge.risk-medium { background: var(--risk-medium-bg); color: var(--risk-medium-fg); }
  .badge.risk-high { background: var(--risk-high-bg); color: var(--risk-high-fg); }

  .detail {
    padding: 0.2rem 0.9rem 1.1rem 2.35rem;
    display: none;
  }

  .row-wrap.expanded .detail { display: block; }

  .route-tag {
    display: inline-block;
    font-family: var(--font-mono);
    font-size: 0.68rem;
    color: var(--text-muted);
    background: var(--surface-sunken);
    border: 1px solid var(--border);
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    margin-bottom: 0.7rem;
  }

  .fields {
    display: grid;
    grid-template-columns: minmax(0, 26%) 1fr;
    gap: 0.45rem 0.65rem;
    margin-bottom: 0.85rem;
  }

  .field-key {
    font-family: var(--font-mono);
    font-size: 0.74rem;
    color: var(--text-muted);
    padding-top: 0.5rem;
    word-break: break-word;
  }

  .field-value textarea {
    width: 100%;
    box-sizing: border-box;
    resize: vertical;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    line-height: 1.45;
    padding: 0.4rem 0.55rem;
  }

  .field-value textarea:focus {
    outline: 2px solid var(--focus-ring);
    outline-offset: -1px;
  }

  .findings {
    background: var(--risk-high-bg);
    color: var(--risk-high-fg);
    border-radius: 8px;
    padding: 0.6rem 0.75rem;
    font-size: 0.78rem;
    margin-bottom: 0.85rem;
  }

  .findings .findings-title {
    font-weight: 650;
    margin-bottom: 0.3rem;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .findings ul { margin: 0; padding-left: 1.1rem; }
  .findings li { margin: 0.15rem 0; }
  .findings code { font-family: var(--font-mono); font-size: 0.75rem; }

  .actions {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    flex-wrap: wrap;
  }

  button {
    font-family: var(--font-ui);
    font-size: 0.82rem;
    font-weight: 600;
    padding: 0.45rem 0.85rem;
    border-radius: 7px;
    border: 1px solid var(--border-strong);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
    transition: filter 0.1s ease, transform 0.05s ease, border-color 0.1s ease;
  }

  button:hover { filter: brightness(0.97); }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) button:hover { filter: brightness(1.2); }
  }
  :root[data-theme="dark"] button:hover { filter: brightness(1.2); }

  button:active { transform: translateY(1px); }

  button:focus-visible {
    outline: 2px solid var(--focus-ring);
    outline-offset: 2px;
  }

  button.primary {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--accent-text);
  }

  button.quiet { background: transparent; }

  button.danger-text {
    background: transparent;
    border-color: transparent;
    color: var(--danger);
    margin-left: auto;
  }

  .reject-panel {
    margin-top: 0.7rem;
    padding: 0.7rem;
    background: var(--danger-bg);
    border-radius: 8px;
    display: none;
  }

  .reject-panel.open { display: block; }

  .reject-panel label {
    display: block;
    font-size: 0.72rem;
    font-weight: 650;
    color: var(--danger);
    margin-bottom: 0.4rem;
  }

  .reject-panel textarea {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--surface);
    color: var(--text);
    font-family: var(--font-ui);
    font-size: 0.82rem;
    padding: 0.45rem 0.55rem;
    margin-bottom: 0.55rem;
    resize: vertical;
  }

  .reject-panel .reject-actions { display: flex; gap: 0.45rem; }

  button.confirm-reject {
    background: var(--danger);
    border-color: var(--danger);
    color: var(--danger-text);
  }

  .toast {
    position: fixed;
    bottom: 1.5rem;
    left: 50%;
    transform: translateX(-50%) translateY(8px);
    background: var(--text);
    color: var(--bg);
    padding: 0.5rem 0.95rem;
    border-radius: 7px;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    font-weight: 600;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.15s ease, transform 0.15s ease;
  }

  .toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }

  .row-enter {
    animation: rise 0.25s ease-out;
  }

  @keyframes rise {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .detail-enter {
    animation: expand 0.15s ease-out;
  }

  @keyframes expand {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  @media (prefers-reduced-motion: reduce) {
    * { transition: none !important; animation: none !important; }
  }
</style>
</head>
<body>
<div class="shell">
  <div class="hero">
    <div class="hero-left">
      <div class="live-row">
        <span class="live-dot" id="live-dot"></span>
        <span class="live-label" id="live-label">idle</span>
      </div>
      <div class="hero-count" id="hero-count">0<span class="unit">pending</span></div>
      <div class="hero-sub" id="hero-sub">Nothing is waiting on you right now.</div>
    </div>
    <div class="hero-right">
      <div class="brand"><span class="glyph">&#9679;</span>approval-gate</div>
      <div class="reviewer-field">
        <label for="reviewer">as</label>
        <input id="reviewer" type="text" value="reviewer" autocomplete="off" spellcheck="false">
      </div>
    </div>
  </div>

  <div id="root"></div>
</div>

<div class="toast" id="toast"></div>

<script>
const REVIEWER_KEY = "approval-gate:reviewer-name";
const reviewerInput = document.getElementById("reviewer");
reviewerInput.value = localStorage.getItem(REVIEWER_KEY) || "reviewer";
reviewerInput.addEventListener("change", () => {
  localStorage.setItem(REVIEWER_KEY, reviewerInput.value.trim() || "reviewer");
});

function reviewerName() {
  return reviewerInput.value.trim() || "reviewer";
}

function showToast(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => toast.classList.remove("show"), 1800);
}

function timeAgo(seconds) {
  if (seconds < 60) return "just now";
  const m = Math.floor(seconds / 60);
  if (m < 60) return m + "m ago";
  const h = Math.floor(m / 60);
  return h + "h ago";
}

let knownIds = new Set();
let expandedId = null;

function fieldPreview(args) {
  const parts = Object.entries(args).map(([k, v]) => `${k}: ${v}`);
  return parts.join("  ·  ") || "(no arguments)";
}

async function refresh() {
  let items;
  try {
    const res = await fetch("/api/pending");
    items = await res.json();
  } catch (e) {
    return; // transient fetch failure -- next poll will retry
  }

  const root = document.getElementById("root");
  const dot = document.getElementById("live-dot");
  const label = document.getElementById("live-label");
  const heroCount = document.getElementById("hero-count");
  const heroSub = document.getElementById("hero-sub");

  const n = items.length;
  dot.className = "live-dot" + (n > 0 ? " active" : "");
  label.className = "live-label" + (n > 0 ? " active" : "");
  label.textContent = n > 0 ? "paused" : "idle";
  heroCount.innerHTML = n + '<span class="unit">' + (n === 1 ? "action pending" : "actions pending") + '</span>';
  heroSub.textContent = n > 0
    ? "An agent is frozen mid-action, waiting on a decision."
    : "Nothing is waiting on you right now.";

  if (n === 0) {
    root.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.innerHTML = '<div class="glyph">&#10003;</div>' +
      '<div class="title">Queue clear</div>' +
      '<div class="hint">New actions land here the instant an agent needs a decision.</div>';
    root.appendChild(empty);
    knownIds = new Set();
    expandedId = null;
    return;
  }

  if (expandedId && !items.some((it) => it.audit_id === expandedId)) {
    expandedId = null; // the expanded item was just decided elsewhere
  }

  const rejectOpenIds = new Set(
    Array.from(root.querySelectorAll(".reject-panel.open")).map((el) => el.dataset.auditId)
  );

  const list = document.createElement("div");
  list.className = "list";

  const head = document.createElement("div");
  head.className = "list-head";
  head.innerHTML = "<span></span><span>Action</span><span>Findings</span><span>Waiting</span><span>Risk</span>";
  list.appendChild(head);

  const nextIds = new Set(items.map((it) => it.audit_id));
  for (const item of items) {
    const wrap = renderRow(item, item.audit_id === expandedId, rejectOpenIds.has(item.audit_id));
    if (!knownIds.has(item.audit_id)) wrap.classList.add("row-enter");
    list.appendChild(wrap);
  }
  knownIds = nextIds;

  root.innerHTML = "";
  root.appendChild(list);
}

function renderRow(item, isExpanded, rejectOpen) {
  const wrap = document.createElement("div");
  wrap.className = "row-wrap" + (isExpanded ? " expanded" : "");
  wrap.dataset.auditId = item.audit_id;

  const row = document.createElement("button");
  row.type = "button";
  row.className = "row";
  row.setAttribute("aria-expanded", String(isExpanded));
  row.onclick = () => {
    expandedId = isExpanded ? null : item.audit_id;
    refresh();
  };

  const chevron = document.createElement("span");
  chevron.className = "chevron";
  chevron.textContent = "▸";
  row.appendChild(chevron);

  const main = document.createElement("span");
  main.className = "row-main";
  const name = document.createElement("span");
  name.className = "row-action";
  name.textContent = item.action;
  const preview = document.createElement("span");
  preview.className = "row-preview";
  preview.textContent = fieldPreview(item.args);
  main.appendChild(name);
  main.appendChild(preview);
  row.appendChild(main);

  const findingsCell = document.createElement("span");
  const count = (item.pii_findings || []).length;
  findingsCell.className = "row-findings " + (count > 0 ? "has-findings" : "none");
  findingsCell.textContent = count > 0 ? `⚠ ${count}` : "—";
  row.appendChild(findingsCell);

  const waitingCell = document.createElement("span");
  waitingCell.className = "row-waiting";
  waitingCell.textContent = typeof item._first_seen === "number"
    ? timeAgo((Date.now() / 1000) - item._first_seen)
    : "";
  row.appendChild(waitingCell);

  const riskCell = document.createElement("span");
  riskCell.className = "row-risk";
  const badge = document.createElement("span");
  badge.className = "badge risk-" + item.risk;
  badge.textContent = item.risk;
  riskCell.appendChild(badge);
  row.appendChild(riskCell);

  wrap.appendChild(row);
  wrap.appendChild(renderDetail(item, rejectOpen));
  return wrap;
}

function renderDetail(item, rejectOpen) {
  const detail = document.createElement("div");
  detail.className = "detail detail-enter";

  if (item.route_to) {
    const route = document.createElement("div");
    route.className = "route-tag";
    route.textContent = "→ " + item.route_to;
    detail.appendChild(route);
  }

  if (item.pii_findings && item.pii_findings.length > 0) {
    const findings = document.createElement("div");
    findings.className = "findings";
    const title = document.createElement("div");
    title.className = "findings-title";
    title.textContent = "⚠ Sensitive data detected";
    findings.appendChild(title);
    const ul = document.createElement("ul");
    for (const f of item.pii_findings) {
      const li = document.createElement("li");
      li.innerHTML = `<code>${f.type}</code> in <code>${f.field}</code> &mdash; ${f.value_masked}`;
      ul.appendChild(li);
    }
    findings.appendChild(ul);
    detail.appendChild(findings);
  }

  const fields = document.createElement("div");
  fields.className = "fields";
  const inputs = {};
  for (const [k, v] of Object.entries(item.args)) {
    const key = document.createElement("div");
    key.className = "field-key";
    key.textContent = k;
    const val = document.createElement("div");
    val.className = "field-value";
    const textarea = document.createElement("textarea");
    textarea.rows = String(v).length > 70 ? 3 : 1;
    textarea.value = v;
    inputs[k] = textarea;
    val.appendChild(textarea);
    fields.appendChild(key);
    fields.appendChild(val);
  }
  detail.appendChild(fields);

  const actions = document.createElement("div");
  actions.className = "actions";

  const approveBtn = document.createElement("button");
  approveBtn.className = "primary";
  approveBtn.textContent = "Approve";
  approveBtn.onclick = () => decide(item.audit_id, "approve", inputs);
  actions.appendChild(approveBtn);

  const editBtn = document.createElement("button");
  editBtn.className = "quiet";
  editBtn.textContent = "Approve with edits";
  editBtn.onclick = () => decide(item.audit_id, "edit", inputs);
  actions.appendChild(editBtn);

  const rejectBtn = document.createElement("button");
  rejectBtn.className = "danger-text";
  rejectBtn.textContent = "Reject";
  rejectBtn.onclick = () => rejectPanel.classList.toggle("open");
  actions.appendChild(rejectBtn);

  detail.appendChild(actions);

  const rejectPanel = document.createElement("div");
  rejectPanel.className = "reject-panel" + (rejectOpen ? " open" : "");
  rejectPanel.dataset.auditId = item.audit_id;
  rejectPanel.innerHTML = '<label>Reason for rejecting (optional)</label>';
  const reasonBox = document.createElement("textarea");
  reasonBox.rows = 2;
  reasonBox.placeholder = "e.g. wrong recipient, needs a smaller scope, not needed yet...";
  rejectPanel.appendChild(reasonBox);
  const rejectActions = document.createElement("div");
  rejectActions.className = "reject-actions";
  const confirmBtn = document.createElement("button");
  confirmBtn.className = "confirm-reject";
  confirmBtn.textContent = "Confirm reject";
  confirmBtn.onclick = () => decide(item.audit_id, "reject", inputs, reasonBox.value);
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "quiet";
  cancelBtn.textContent = "Cancel";
  cancelBtn.onclick = () => rejectPanel.classList.remove("open");
  rejectActions.appendChild(confirmBtn);
  rejectActions.appendChild(cancelBtn);
  rejectPanel.appendChild(rejectActions);
  detail.appendChild(rejectPanel);

  return detail;
}

async function decide(auditId, decision, inputs, reason) {
  const args = {};
  for (const [k, el] of Object.entries(inputs)) args[k] = el.value;
  await fetch("/api/decide", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      audit_id: auditId,
      decision,
      args,
      reason: reason || "",
      by: reviewerName(),
    }),
  });
  showToast(decision === "approve" ? "Approved" : decision === "edit" ? "Approved with edits" : "Rejected");
  refresh();
}

refresh();
setInterval(refresh, 2000);
</script>
</body>
</html>
"""


class WebBackend(_PendingQueueBackend, Backend):
    def __init__(self, host: str = "127.0.0.1", port: int = 8642, notifier: Optional[Notifier] = None):
        super().__init__()
        self.notifier = notifier

        handler = _make_handler(self)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self.url = f"http://{self.host}:{self.port}"

    def wait_for_decision(self, pending: dict[str, Any]) -> dict[str, Any]:
        audit_id = pending["audit_id"]
        # _first_seen is UI-only (drives the "waiting Xm" indicator), so it's
        # added to a copy rather than the payload handed to the notifier.
        display_pending = {**pending, "_first_seen": time.time()}
        result_queue = self._register(audit_id, display_pending)

        if self.notifier is not None:
            safe_notify(self.notifier, pending, self.url)

        try:
            return result_queue.get()  # blocks until the browser POSTs a decision
        finally:
            self._unregister(audit_id)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_handler(backend: WebBackend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence default request logging
            pass

        def _send_json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/pending":
                self._send_json(backend._list_pending())
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/api/decide":
                self.send_response(404)
                self.end_headers()
                return

            # A malformed request here shouldn't crash the request thread
            # with an unhandled traceback -- it's a client mistake (bad
            # JSON, wrong shape), not a server bug, so it gets a 400 like
            # any other API.
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length > 0 else b"{}"
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("expected a JSON object")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "malformed request body"}, status=400)
                return

            audit_id = body.get("audit_id")
            decision = {
                "decision": body.get("decision", "reject"),
                "by": body.get("by", "web-reviewer"),
                "reason": body.get("reason", ""),
                "args": body.get("args"),
            }
            if not backend.resolve(audit_id, decision):
                self._send_json({"error": "unknown or already-decided audit_id"}, status=404)
                return
            self._send_json({"ok": True})

    return Handler
