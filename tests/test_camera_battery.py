"""Tests for solar/battery camera model detection."""
from __future__ import annotations

from camera_helpers import (
    camera_model_has_battery,
    camera_nodedef_id,
    dev_has_battery,
)


class _ShellCamera:
    model = 'C675D'
    modules = {}


def test_camera_model_has_battery_c675d():
    assert camera_model_has_battery('C675D') is True
    assert camera_model_has_battery('c675d') is True
    assert camera_model_has_battery('C260') is False


def test_dev_has_battery_from_model_when_modules_missing():
    assert dev_has_battery(_ShellCamera()) is True


def test_camera_nodedef_id_uses_model_from_cfg():
    assert (
        camera_nodedef_id(cfg={'model': 'C675D'})
        == 'SmartCamera_B'
    )
