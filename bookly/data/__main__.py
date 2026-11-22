"""`python -m bookly.data reset` recreates and seeds the demo database."""

from __future__ import annotations

import sys

from bookly.config import get_settings
from bookly.data import seed
from bookly.data.repo import Repo


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "reset"
    if cmd != "reset":
        print("usage: python -m bookly.data reset")
        return 2
    settings = get_settings()
    repo = Repo(settings.db_path)
    repo.reset()
    print(f"Reset demo database at {settings.db_path}\n")
    print("Seed scenarios (verify with order number + email):")
    for s in seed.SCENARIOS:
        print(f"  [{s['card']}] {s['customer']:<11} order {s['order_number']}  {s['email']:<24} {s['shows']}")
        print(f"      try: \"{s['first_message']}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
