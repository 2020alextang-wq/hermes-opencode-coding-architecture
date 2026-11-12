#!/usr/bin/env python3
"""Thin compatibility entry point for the coding_control supervisor."""
import sys

OPERATIONS = ("preflight", "run", "status", "usage", "cancel")
USAGE = "usage: bootstrap.py {preflight,run} TASK | {status,usage,cancel} RUN_ID"


def main(argv):
    if len(argv) == 1 and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if (len(argv) != 2 or argv[0] not in OPERATIONS
            or argv[1].lower().endswith(".md")):
        print(USAGE, file=sys.stderr)
        return 2
    from coding_control import main as control_main
    return control_main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
