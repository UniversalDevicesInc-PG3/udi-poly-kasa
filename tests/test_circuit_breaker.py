"""Tests for per-host circuit breaker backoff."""

from __future__ import annotations

import time

from conftest import make_controller_stub


def test_host_should_skip_after_threshold_failures():
    ctrl = make_controller_stub()
    host = '192.168.222.80'

    for _ in range(3):
        ctrl.host_record_failure(host)

    assert ctrl.host_should_skip(host) is True
    ctrl.set_device_notice.assert_called_once()


def test_host_should_skip_clears_after_success():
    ctrl = make_controller_stub()
    host = '192.168.222.80'

    for _ in range(3):
        ctrl.host_record_failure(host)
    assert ctrl.host_should_skip(host) is True

    ctrl.host_record_success(host)
    assert ctrl.host_should_skip(host) is False
    ctrl.clear_device_notice.assert_called_once()


def test_host_should_skip_resumes_after_backoff_expires():
    ctrl = make_controller_stub()
    host = '192.168.222.18'

    for _ in range(3):
        ctrl.host_record_failure(host)

    ctrl._host_state[host]['next_try'] = time.monotonic() - 1.0
    assert ctrl.host_should_skip(host) is False
