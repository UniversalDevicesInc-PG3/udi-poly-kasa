"""Per-device ERR driver indices (uom 25) for the Kasa plugin."""

ERR_OK = 0
ERR_AUTH = 1
ERR_NO_CREDS = 2
ERR_UNREACHABLE = 3
ERR_COMM = 4
ERR_DISCOVER = 5
ERR_CIRCUIT = 6
ERR_UNKNOWN = 7
# Camera/hub child reachable on LAN but getDeviceInfo empty (asleep / not ready).
ERR_NOT_READY = 8


def err_code_for_kasa_exception(ex):
    """Map a python-kasa exception to an ERR driver index."""
    text = f'{type(ex).__name__}: {ex}'.lower()
    if 'host is down' in text or 'connection refused' in text:
        return ERR_UNREACHABLE
    if 'getdeviceinfo not found' in text or 'not found in {}' in text:
        # Sleeping solar/hub-paired cameras often return an empty shell.
        # Host was reached — do not use ERR_UNREACHABLE (Host unreachable).
        return ERR_NOT_READY
    if 'timed out' in text or 'timeout' in text:
        return ERR_COMM
    return ERR_COMM


def err_code_for_connect_message(msg):
    """Map a connect-path log/notice message to an ERR driver index."""
    if not msg:
        return None
    text = str(msg).lower()
    if 'unable to discover' in text:
        return ERR_DISCOVER
    return None
