"""
pii.py
------
Scans a proposed action's arguments for things a human reviewer would
want flagged before approving: emails, phone numbers, card numbers,
government-id-shaped numbers, and API-key/secret-shaped strings.

Why regex-first instead of "just use Presidio": Microsoft Presidio is
free, mature, and the right long-term detection engine -- but it pulls
in spaCy and a language model, which is a heavy, slow dependency for a
package whose whole pitch is "drop this in and it just works." So:

- If `presidio_analyzer` is importable AND a model loads successfully,
  we use it for richer NLP-based name/location detection.
- Either way, fast deterministic regex checks always run too (emails,
  phone numbers, card numbers via Luhn, API-key-shaped tokens). These
  are the high-confidence, low-latency, easy-to-explain-to-an-auditor
  checks, and they require zero setup.

This mirrors how real PII-detection stacks are built in production:
regex for structured PII, ML for the fuzzy stuff, never ML alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class Finding:
    type: str
    value_masked: str
    field: str
    source: str  # "regex" | "presidio"


_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_PHONE_RE = re.compile(
    r"(?<!\d)(\+\d{1,3}[-.\s]?)?(\(?\d{2,5}\)?[-.\s]){1,4}\d{2,5}(?!\d)"
)
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_SECRET_RE = re.compile(
    r"(sk-[a-zA-Z0-9]{16,}|AKIA[0-9A-Z]{12,}|ghp_[a-zA-Z0-9]{20,}|"
    r"xox[baprs]-[a-zA-Z0-9-]{10,}|AIza[0-9A-Za-z\-_]{20,}|"
    r"bearer\s+[a-zA-Z0-9._-]{15,})",
    re.IGNORECASE,
)


def _luhn_ok(card_digits: str) -> bool:
    digits = [int(d) for d in card_digits]
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def _mask(value: str, keep: int = 2) -> str:
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]


def _regex_scan(field: str, text: str) -> list[Finding]:
    findings: list[Finding] = []

    for m in _EMAIL_RE.finditer(text):
        findings.append(Finding("email", _mask(m.group()), field, "regex"))

    for m in _SECRET_RE.finditer(text):
        findings.append(Finding("api_secret", _mask(m.group(), keep=3), field, "regex"))

    for m in _SSN_RE.finditer(text):
        findings.append(Finding("ssn_like", _mask(m.group()), field, "regex"))

    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"[ -]", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            findings.append(Finding("card_number", _mask(digits, keep=4), field, "regex"))

    for m in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 7 <= len(digits) <= 15:
            findings.append(Finding("phone", _mask(m.group()), field, "regex"))

    return findings


_presidio_analyzer = None
_presidio_unavailable = False


def _get_presidio():
    global _presidio_analyzer, _presidio_unavailable
    if _presidio_unavailable or _presidio_analyzer is not None:
        return _presidio_analyzer
    try:
        from presidio_analyzer import AnalyzerEngine  # type: ignore

        _presidio_analyzer = AnalyzerEngine()
    except Exception:
        _presidio_unavailable = True
        _presidio_analyzer = None
    return _presidio_analyzer


def _presidio_scan(field: str, text: str) -> list[Finding]:
    analyzer = _get_presidio()
    if analyzer is None:
        return []
    try:
        results = analyzer.analyze(text=text, language="en")
    except Exception:
        return []
    findings = []
    for r in results:
        snippet = text[r.start : r.end]
        findings.append(Finding(r.entity_type.lower(), _mask(snippet), field, "presidio"))
    return findings


def scan(args: dict[str, Any]) -> list[dict]:
    """Scan every string field in `args`. Returns plain dicts (JSON-friendly)."""
    all_findings: list[Finding] = []
    for field, value in args.items():
        if not isinstance(value, str) or not value:
            continue
        all_findings.extend(_regex_scan(field, value))
        all_findings.extend(_presidio_scan(field, value))

    # de-dupe identical (type, field, value_masked) triples from both engines
    seen = set()
    deduped = []
    for f in all_findings:
        key = (f.type, f.field, f.value_masked)
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return [asdict(f) for f in deduped]


def using_presidio() -> bool:
    return _get_presidio() is not None
