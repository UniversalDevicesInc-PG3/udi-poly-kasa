"""python-kasa 0.10+ exception aliases for the Kasa plugin."""

from kasa.exceptions import AuthenticationError, DeviceError, KasaException

# Renamed in python-kasa 0.10: SmartDeviceException -> DeviceError
SmartDeviceException = DeviceError
