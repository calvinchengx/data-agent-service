"""`make seed` — run the seed steps that exist so far, in order."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Per dataset, then once for the whole stack. Datasets are listed rather than
# discovered so adding one is a visible decision — and so the order (data
# before catalog) stays explicit.
DATASETS = ["contoso", "support"]
PER_DATASET = [
    ("seed.provision", "data"),
    ("seed.govern", "OpenMetadata semantics"),
]
ONCE = [
    ("seed.apps", "app registrations"),
    ("seed.apim", "gateway APIs + policies"),
    ("seed.authz", "personas + access rules"),
]

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.environ.get("DAS_ENV", "local"))
    ap.add_argument("--dataset", default=None, help="one dataset; default is all of them")
    ap.add_argument("--reset", action="store_true")
    a = ap.parse_args()
    env = dict(os.environ, DAS_ENV=a.env)
    datasets = [a.dataset] if a.dataset else DATASETS
    for dataset in datasets:
        for mod, what in PER_DATASET:
            print(f"\n### {dataset}: {what} ({mod})", flush=True)
            args = [sys.executable, "-m", mod, "--dataset", dataset] + (
                ["--reset"] if a.reset else [])
            subprocess.run(args, check=True, env=env)
    for mod, what in ONCE:
        print(f"\n### {what} ({mod})", flush=True)
        subprocess.run([sys.executable, "-m", mod], check=True, env=env)
