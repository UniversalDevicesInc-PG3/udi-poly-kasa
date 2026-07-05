"""Tests for add-node queue pacing."""

from __future__ import annotations

import threading
import time

from conftest import make_controller_stub


def test_wait_for_node_done_matches_specific_address():
    ctrl = make_controller_stub()
    ctrl.n_queue.append('192.168.1.10')

    assert ctrl.wait_for_node_done('192.168.1.10', timeout_sec=0.5) is True
    assert ctrl.n_queue == []


def test_wait_for_node_done_any_address():
    ctrl = make_controller_stub()
    ctrl.n_queue.append('abc')

    assert ctrl.wait_for_node_done(timeout_sec=0.5) is True
    assert ctrl.n_queue == []


def test_wait_for_node_done_times_out():
    ctrl = make_controller_stub()

    start = time.monotonic()
    assert ctrl.wait_for_node_done('192.168.1.99', timeout_sec=0.2) is False
    assert time.monotonic() - start >= 0.15


def test_wait_for_node_done_wakes_when_queue_updated():
    ctrl = make_controller_stub()

    def enqueue():
        time.sleep(0.05)
        ctrl.n_queue.append('192.168.1.20')

    threading.Thread(target=enqueue, daemon=True).start()
    assert ctrl.wait_for_node_done('192.168.1.20', timeout_sec=1.0) is True
