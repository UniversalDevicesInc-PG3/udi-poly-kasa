"""Tests for coalesced Tapo hub updates and protocol-aware backoff."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

from conftest import make_controller_stub


def test_hub_node_update_a_coalesces_within_window():
    ctrl = make_controller_stub()
    ctrl.hub_update_coalesce_secs = 25.0
    ctrl._hub_update_locks = {}
    ctrl._hub_update_cache = {}

    hub_node = MagicMock()
    hub_node.address = 'ccbabd1606d8'
    hub_node.update_device_a = AsyncMock(return_value=True)

    async def _run():
        assert await ctrl.hub_node_update_a(hub_node) is True
        assert await ctrl.hub_node_update_a(hub_node) is True

    asyncio.run(_run())
    hub_node.update_device_a.assert_awaited_once()


def test_hub_node_update_a_refreshes_after_coalesce_window():
    ctrl = make_controller_stub()
    ctrl.hub_update_coalesce_secs = 0.01
    ctrl._hub_update_locks = {}
    ctrl._hub_update_cache = {}

    hub_node = MagicMock()
    hub_node.address = 'ccbabd1606d8'
    hub_node.update_device_a = AsyncMock(side_effect=[True, False])

    async def _run():
        assert await ctrl.hub_node_update_a(hub_node) is True
        await asyncio.sleep(0.02)
        assert await ctrl.hub_node_update_a(hub_node) is False

    asyncio.run(_run())
    assert hub_node.update_device_a.await_count == 2


def test_host_hub_protocol_degraded_after_protocol_failure():
    ctrl = make_controller_stub()
    host = '192.168.1.150'

    ctrl.host_record_failure(host, protocol_fail=True)
    assert ctrl.host_hub_protocol_degraded(host) is True

    ctrl.host_record_success(host)
    assert ctrl.host_hub_protocol_degraded(host) is False


def test_quick_probe_does_not_reset_breaker_after_protocol_failure():
    ctrl = make_controller_stub()
    ctrl.host_quick_probe_interval = 30.0
    host = '192.168.1.150'

    for _ in range(3):
        ctrl.host_record_failure(host, protocol_fail=True)
    assert ctrl.host_should_skip(host) is True

    async def _fake_open_connection(*_args, **_kwargs):
        reader = MagicMock()
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        return reader, writer

    async def _run():
        original = asyncio.open_connection
        asyncio.open_connection = _fake_open_connection
        try:
            return await ctrl.host_quick_probe(host, port=443, timeout=0.1)
        finally:
            asyncio.open_connection = original

    assert asyncio.run(_run()) is True
    assert ctrl.host_should_skip(host) is True
    ctrl.clear_device_notice.assert_not_called()


def test_host_should_quick_probe_false_when_protocol_fail():
    ctrl = make_controller_stub()
    host = '192.168.1.150'

    for _ in range(3):
        ctrl.host_record_failure(host, protocol_fail=True)

    assert ctrl.host_should_quick_probe(host) is False
    ctrl._host_state[host]['next_try'] = time.monotonic() - 1.0
    assert ctrl.host_should_quick_probe(host) is False
