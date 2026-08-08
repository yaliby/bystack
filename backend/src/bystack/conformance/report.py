"""One checklist format, for every conformance run.

The three runs -- `bystack.conformance`, `.fleet` and `.local` -- verify
different claims and print the same thing, because the output is read by
whoever is fixing an agent and a format that varied per run would be one more
thing to learn before the failure could be understood.

A failing check prints *why it matters*. That line is not decoration: several
of these exist for defects that are invisible at runtime, where the agent
works perfectly and the topology looks right, and a bare `FAIL burst
coalescing` would be indistinguishable from a flaky assertion.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Check:
    name: str
    why: str
    passed: bool
    detail: str = ""


def report(title: str, checks: list[Check]) -> int:
    """Print the checklist and return an exit status."""
    print(f"\n--- {title} ---")
    failed = 0
    for check in checks:
        mark = "PASS" if check.passed else "FAIL"
        print(f"  [{mark}] {check.name}")
        if check.detail:
            print(f"         {check.detail}")
        if not check.passed:
            failed += 1
            print(f"         why it matters: {check.why}")

    total = len(checks)
    print(f"\n{total - failed}/{total} checks passed")
    return 1 if failed else 0
