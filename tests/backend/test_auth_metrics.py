"""Tests for the auth observability metrics (#696).

login_failure_total{reason} and authz_denied_total{permission} — no-op when
metrics are disabled, and increment the right labeled counter when enabled.
"""
from unittest.mock import MagicMock

import pytest


class TestAuthMetricsNoop:
    @pytest.mark.unit
    def test_record_functions_noop_when_uninitialized(self):
        import utils.metrics as m
        m._metrics_initialized = False
        # Must not raise even though the counters are None.
        m.record_login_failure("bad_credentials")
        m.record_authz_denied("chat.write")


class TestAuthMetricsEnabled:
    @pytest.mark.unit
    def test_record_login_failure_increments_labeled_counter(self):
        import utils.metrics as m
        counter = MagicMock()
        m._metrics_initialized = True
        m._login_failure_total = counter
        try:
            m.record_login_failure("locked_out")
            counter.labels.assert_called_once_with(reason="locked_out")
            counter.labels.return_value.inc.assert_called_once()
        finally:
            m._metrics_initialized = False
            m._login_failure_total = None

    @pytest.mark.unit
    def test_record_authz_denied_increments_labeled_counter(self):
        import utils.metrics as m
        counter = MagicMock()
        m._metrics_initialized = True
        m._authz_denied_total = counter
        try:
            m.record_authz_denied("password_change_required")
            counter.labels.assert_called_once_with(permission="password_change_required")
            counter.labels.return_value.inc.assert_called_once()
        finally:
            m._metrics_initialized = False
            m._authz_denied_total = None
