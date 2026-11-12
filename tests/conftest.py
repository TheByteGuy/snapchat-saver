"""Shared test setup."""
import os

# The daily job waits a random few minutes before sending; tests must not.
os.environ.setdefault("SNAP_SEND_JITTER_MINUTES", "0")
