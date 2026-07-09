"""Tests for per-device ERR driver classification."""

from __future__ import annotations

from unittest.mock import MagicMock

from conftest import make_controller_stub
from device_errors import (
    ERR_AUTH,
    ERR_CIRCUIT,
    ERR_COMM,
    ERR_DISCOVER,
    ERR_NO_CREDS,
    ERR_NOT_READY,
    ERR_OK,
    ERR_UNREACHABLE,
    ERR_UNKNOWN,
    err_code_for_connect_message,
    err_code_for_kasa_exception,
)
from kasa.exceptions import KasaException
from nodes.Controller import Controller


def test_err_code_for_kasa_exception_unreachable():
    assert err_code_for_kasa_exception(KasaException('Host is down')) == ERR_UNREACHABLE


def test_err_code_for_kasa_exception_timeout():
    assert err_code_for_kasa_exception(KasaException('Timed out waiting')) == ERR_COMM


def test_err_code_for_kasa_exception_sleeping_camera_shell():
    assert (
        err_code_for_kasa_exception(
            KasaException('getDeviceInfo not found in {}')
        )
        == ERR_NOT_READY
    )


def test_err_code_for_connect_message_discover():
    assert err_code_for_connect_message('Unable to discover 192.168.1.5') == ERR_DISCOVER
    assert err_code_for_connect_message('failed updating, see log') is None


def test_set_host_device_err_updates_nodes_for_host():
    ctrl = make_controller_stub()
    host = '192.168.1.60'
    node = MagicMock()
    node.host = host
    ctrl.nodes_by_mac = {'a': node}
    ctrl.poly.getNodes.return_value = []

    Controller._set_host_device_err(ctrl, host, ERR_AUTH)
    node.setDriver.assert_called_once_with('ERR', ERR_AUTH)

    node.setDriver.reset_mock()
    Controller._set_host_device_err(ctrl, host, ERR_OK)
    node.setDriver.assert_called_once_with('ERR', ERR_OK)


def test_err_indices_are_stable():
    assert ERR_OK == 0
    assert ERR_AUTH == 1
    assert ERR_NO_CREDS == 2
    assert ERR_UNREACHABLE == 3
    assert ERR_COMM == 4
    assert ERR_DISCOVER == 5
    assert ERR_CIRCUIT == 6
    assert ERR_UNKNOWN == 7
    assert ERR_NOT_READY == 8
