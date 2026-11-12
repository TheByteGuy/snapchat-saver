#!/usr/bin/env python3
"""supervisord event listener: stop the container when a program gives up.

supervisord marks a program FATAL after it failed to start `startretries`
times in a row. Instead of leaving a half-working container running, this
listener asks supervisord to shut down so the container exits and Docker's
restart policy (restart: unless-stopped) starts a fresh one.
"""
import os
import signal
import sys


def main() -> None:
    while True:
        sys.stdout.write("READY\n")
        sys.stdout.flush()
        header_line = sys.stdin.readline()
        if not header_line:
            return
        headers = dict(token.split(":", 1) for token in header_line.split())
        payload = sys.stdin.read(int(headers.get("len", "0")))
        if headers.get("eventname") == "PROCESS_STATE_FATAL":
            fields = dict(token.split(":", 1) for token in payload.split() if ":" in token)
            name = fields.get("processname", "?")
            sys.stderr.write(
                f"[fatal-listener] program '{name}' entered FATAL state; stopping container\n"
            )
            sys.stderr.flush()
            os.kill(os.getppid(), signal.SIGTERM)
        sys.stdout.write("RESULT 2\nOK")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
