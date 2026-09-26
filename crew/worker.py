"""Run one backend role per process: python -m crew.worker Concierge."""

from __future__ import annotations

import os
import sys
import time

from .jobs import ROLE_KINDS, run_one_job
from .seed import seed_demo


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    role = args[0] if args else os.getenv("CREW_WORKER_ROLE", "")
    if role not in ROLE_KINDS:
        raise SystemExit("Set CREW_WORKER_ROLE to Concierge, Buyer, Events, or Treasurer")
    seed_demo()
    while True:
        if not run_one_job(role):
            time.sleep(0.5)


if __name__ == "__main__":
    main()
