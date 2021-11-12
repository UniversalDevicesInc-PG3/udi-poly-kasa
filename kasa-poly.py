#!/usr/bin/env python
"""
This is a TP-Link Kasa NodeServer for Polyglot v2 written in Python3
by JimBo jimboca3@gmail.com
"""

import asyncio
import sys
from udi_interface import Interface,LOGGER
import time
import warnings
from nodes import Controller

def main():
    # Some are getting unclosed socket warnings due to garbage collection?? no idea why, so just ignore them since we dont' care
    warnings.filterwarnings("ignore", category=ResourceWarning, message="unclosed.*<socket.socket.*>")
    if sys.version_info < (3, 6):
        LOGGER.error("ERROR: Python 3.6 or greater is required not {}.{}".format(sys.version_info[0],sys.version_info[1]))
        sys.exit(1)
    try:
        polyglot = Interface([Controller])
        polyglot.start()
        control = Controller(polyglot, 'controller', 'controller', 'FlumeWater')
        polyglot.runForever()
    except (KeyboardInterrupt, SystemExit):
        """
        Catch SIGTERM or Control-C and exit cleanly.
        """
        sys.exit(0)

if __name__ == "__main__":
    main()