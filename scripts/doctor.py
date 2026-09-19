#!/usr/bin/env python3
"""Compatibility entry point: the checks live in setup.py (`./tabs9 doctor`)."""
import os
import sys

if __name__ == '__main__':
    here = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    if '--check' not in args:
        args = ['--doctor', *args]
    os.execv(sys.executable, [sys.executable, os.path.join(here, 'setup.py'), *args])
