#!/usr/bin/env python
"""
This is a TP-Link Kasa NodeServer for Polyglot v2 written in Python3
by JimBo jimboca3@gmail.com
"""

import asyncio
import logging
import os
import sys
import time
import warnings

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
from dev_python_kasa_bootstrap import bootstrap_from_marker
bootstrap_from_marker(_PLUGIN_DIR)

from udi_interface import Interface, LOGGER
from kasa_compat import apply_kasa_patches
apply_kasa_patches()
from nodes import VERSION,Controller

def main():
    # Some are getting unclosed socket warnings due to garbage collection?? no idea why, so just ignore them since we dont' care
    warnings.filterwarnings("ignore", category=ResourceWarning, message="unclosed.*<socket.socket.*>")
    # Quiet the kasa.discover logger. With several unreachable hosts on the
    # LAN it emits dozens of "Got error: [Errno 64] Host is down" ERROR lines
    # per discovery cycle; not actionable per-packet and a major contributor
    # to log volume during incidents.
    logging.getLogger('kasa.discover').setLevel(logging.WARNING)
    if sys.version_info < (3, 6):
        LOGGER.error("ERROR: Python 3.6 or greater is required not {}.{}".format(sys.version_info[0],sys.version_info[1]))
        sys.exit(1)
    try:
        polyglot = Interface([Controller])
        polyglot.start(VERSION)
        control = Controller(polyglot, 'tplkasactl', 'tplkasactl', 'Kasa Controller')
        polyglot.runForever()
    except (KeyboardInterrupt, SystemExit):
        """
        Catch SIGTERM or Control-C and exit cleanly.
        """
        sys.exit(0)

if __name__ == "__main__":
    main()
