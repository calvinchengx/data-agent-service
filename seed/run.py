"""`make seed` — run the seed steps that exist so far, in order."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

STEPS = [
    ("seed.provision", "warehouse + data"),
    # ("seed.govern", "OpenMetadata semantics"),   # Phase 2
    # ("seed.authz", "personas + policies"),        # Phase 6
    # ("seed.apim", "gateway APIs + policies"),     # Phase 4/5
]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    ap.add_argument("--dataset", default="contoso")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    env = dict(os.environ, DAS_ENV=a.env)
    for mod, what in STEPS:
        print(f"\n### {what} ({mod})", flush=True)
        args = [sys.executable, "-m", mod, "--dataset", a.dataset] + (["--reset"] if a.reset else [])
        subprocess.run(args, check=True, env=env)
