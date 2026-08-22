"""A self-contained environment for the unit suite.

The executor reads its configuration at IMPORT time — issuer, audience,
sources, access rules — because those are deployment facts that must not vary
per request. That makes importing it depend on a configured environment, and a
unit suite that needs a configured stack is not a unit suite: it passes in a
container that loaded `.env` and fails on a bare checkout, which is exactly how
CI found this.

So the defaults below are set before any test module imports the executor.
`setdefault` rather than assignment on purpose: when the stack's real
configuration IS present the tests run against it unchanged, and the two must
agree. The values mirror `.env.example` for the settings the tests assert on —
notably that `Data.Analyst` may not read the email columns.
"""

from __future__ import annotations

import json
import os

DEFAULTS = {
    "DAS_ENTRA_ISSUER": "https://entra-emulator:8443/6f89cf12-978b-4d23-ac18-9ef0c127cf87/v2.0",
    "DAS_ENTRA_JWKS_URL": "https://entra-emulator:8443/6f89cf12-978b-4d23-ac18-9ef0c127cf87"
    "/discovery/v2.0/keys",
    "DAS_ENTRA_TLS_INSECURE": "true",
    "DAS_AGENT_AUDIENCE": "api://data-agent-service",
    "DAS_REQUIRED_SCOPE": "access_as_user",
    "DAS_MIDDLE_TIER_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "DAS_SQL_MAX_ROWS": "500",
    "DAS_ROLE_SOURCE": "appRole",
    "DAS_DEFAULT_SOURCE": "contoso_warehouse",
    "DAS_SOURCES": json.dumps(
        [
            {
                "name": "contoso_warehouse",
                "kind": "fabric",
                "dialect": "tsql",
                "authz_tier": "user",
                "om_service_fqn": "fabric_contoso",
                "workspace": "contoso-analytics",
                "item": "contoso_warehouse",
                "schemas": ["dbo"],
                "tds_server": "contoso-analytics.datawarehouse.fabric.microsoft.com:1433",
            }
        ]
    ),
    "DAS_ACCESS_RULES": json.dumps(
        [
            {"role": "Data.Admin", "allow_tables": ["*"], "deny_columns": []},
            {"role": "Data.Finance", "allow_tables": ["dbo.*", "support.*"], "deny_columns": []},
            {
                "role": "Data.Analyst",
                "allow_tables": ["dbo.*", "support.*"],
                "deny_columns": [
                    "dbo.dim_customer.email",
                    "dbo.dim_party.email",
                    "dbo.dim_customer.name",
                    "support.customers.email",
                    "support.agents.email",
                ],
            },
            {"role": "*", "allow_tables": ["dbo.*", "support.*"], "deny_columns": []},
        ]
    ),
    # An address that resolves nowhere: nothing in the unit suite may reach a
    # network, and a test that starts doing so should fail rather than quietly
    # depend on a running emulator.
    "IDENTITY_ENDPOINT": "http://127.0.0.1:1/msi/token",
    "IDENTITY_HEADER": "unit-test",
    "DAS_APIM_BASE": "https://127.0.0.1:1",
    "DAS_PROMOTE_KEY_SECRET": "unit-test-key",
}

for key, value in DEFAULTS.items():
    os.environ.setdefault(key, value)
