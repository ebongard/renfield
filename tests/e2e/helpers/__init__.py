"""Shared helpers for per-area E2E smoke tests.

See tests/e2e/areas/ for consumers. conftest.py already provides the
Playwright page fixture and session-scoped browser; this package adds
area-agnostic page/assert/route helpers so each area test can stay
short and declarative.
"""
