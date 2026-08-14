"""
cli.py
------
The v1 "approval inbox." Not pretty, but it's the fastest path to a
fully working end-to-end loop, and it proves the core mechanic before
any web UI is built. Swap this out for server.py's web inbox later --
both ultimately just need to produce a `{"decision": ..., "by": ...}`
dict and call `graph.invoke(Command(resume=...), config)`.
"""

from __future__ import annotations

from langgraph.types import Command


def _print_pending(pending: dict) -> None:
    print("\n" + "=" * 60)
    print(f"  APPROVAL NEEDED   action: {pending['action']}   risk: {pending['risk']}")
    print("=" * 60)
    print("  Proposed arguments:")
    for k, v in pending["args"].items():
        print(f"    {k}: {v}")
    if pending["pii_findings"]:
        print("\n  ⚠ Sensitive data detected in this action:")
        for f in pending["pii_findings"]:
            print(f"    - {f['type']} in field '{f['field']}': {f['value_masked']}  (via {f['source']})")
    else:
        print("\n  No sensitive data detected.")
    print("-" * 60)


def run_with_cli_approval(graph, initial_state: dict, config: dict, reviewer: str = "you") -> dict:
    """Drive a compiled graph to completion, prompting in the terminal
    every time it pauses on an ApprovalGate interrupt."""

    result = graph.invoke(initial_state, config)

    while "__interrupt__" in result:
        pending = result["__interrupt__"][0].value
        _print_pending(pending)

        choice = input("  Approve / Reject / Edit? [a/r/e]: ").strip().lower()

        if choice == "a":
            decision = {"decision": "approve", "by": reviewer}
        elif choice == "e":
            new_args = dict(pending["args"])
            print("  Press Enter to keep the current value for each field.")
            for k in new_args:
                new_val = input(f"    {k} [{new_args[k]}]: ").strip()
                if new_val:
                    new_args[k] = new_val
            decision = {"decision": "edit", "args": new_args, "by": reviewer}
        else:
            reason = input("  Reason for rejecting: ").strip()
            decision = {"decision": "reject", "by": reviewer, "reason": reason or "rejected by reviewer"}

        result = graph.invoke(Command(resume=decision), config)

    return result
