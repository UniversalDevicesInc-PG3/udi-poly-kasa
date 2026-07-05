"""Custom params loader that redacts sensitive values in debug logs."""

import logging

from udi_interface import Custom

CLOGGER = logging.getLogger('udi_interface.custom')

_SENSITIVE_PARAM_KEYS = frozenset({'password'})


def redact_sensitive_params(params):
    """Return a shallow copy of params safe for logging."""
    if not isinstance(params, dict):
        return params
    redacted = dict(params)
    for key in _SENSITIVE_PARAM_KEYS:
        if key in redacted and redacted[key]:
            redacted[key] = '***'
    return redacted


class SafeCustom(Custom):
    """Custom store that keeps secrets out of udi_interface debug logs."""

    def load(self, new_data, save=False):
        if self.__dict__.get('custom') != 'customparams' or not isinstance(new_data, dict):
            super().load(new_data, save=save)
            return
        sensitive = {
            key: new_data.get(key)
            for key in _SENSITIVE_PARAM_KEYS
            if new_data.get(key)
        }
        if not sensitive:
            super().load(new_data, save=save)
            return
        orig_debug = CLOGGER.debug

        def redacting_debug(msg, *args, **kwargs):
            if isinstance(msg, str):
                for value in sensitive.values():
                    text = str(value)
                    if text and text in msg:
                        msg = msg.replace(text, '***')
            orig_debug(msg, *args, **kwargs)

        CLOGGER.debug = redacting_debug
        try:
            super().load(new_data, save=save)
        finally:
            CLOGGER.debug = orig_debug
