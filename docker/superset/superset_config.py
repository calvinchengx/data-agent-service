"""Superset's configuration for the local stack.

Mounted, not baked: discipline rule 1 says published images are used as-is,
so nothing here forks the image. Everything a deployment would set in Azure is
set the same way here -- the metadata database, the secret, and the feature
flags -- so switching to a hosted Superset is a change of values, not of shape.

CSRF is left ON. Superset enforces it on mutating API calls even with a bearer
token, and turning it off would make the local client simpler than the one
production needs -- which is the emulator-only code path this repo refuses
everywhere else. `publisher/targets/superset.py` does the real dance.
"""

import os

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = os.environ["SUPERSET_METADATA_URI"]

# A promoted dashboard is generated from a template the executor already
# guarded, and it reaches Superset as a VIRTUAL dataset -- a SELECT, not a
# table grant. That is the property that keeps a `service` tier target from
# widening the surface the executor's access rules narrowed, so the ability to
# define one is required rather than optional.
FEATURE_FLAGS = {
    "ENABLE_TEMPLATE_PROCESSING": False,  # no Jinja in a generated dataset
    "DASHBOARD_RBAC": True,
}

# The executor is the only thing that should be reaching a source engine with
# a person's question. Superset reads one warehouse, through one credential,
# for dashboards this repo generated -- so SQL Lab, which exists to let people
# type arbitrary SQL, is off.
SQLLAB_BACKEND_PERSISTENCE = False
ENABLE_PROXY_FIX = True
TALISMAN_ENABLED = False
