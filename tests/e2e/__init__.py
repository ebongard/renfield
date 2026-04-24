"""Browser-based E2E tests for Renfield.

Sub-packages:
  - helpers/ : navigation, console, API, and Paperless helpers used
    across per-area test files.
  - areas/   : one test file per navigation area; each drives the full
    user-facing flow AND asserts the downstream backend state.
"""
