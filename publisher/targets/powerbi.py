"""Power BI: the Plan spelled as TMSL and PBIR, created in Fabric as the user.

This is §18 unchanged, behind the seam §20 describes. The model is Direct
Lake over the warehouse the candidate came from, which is why `accepts`
refuses any other source: a Direct Lake partition binds to a Fabric item, and
a candidate from `postgres` is not a failure of this target but out of its
reach. The identity is the asking person's, through the executor's own
on-behalf-of exchange, so Fabric records them as the creator.
"""

from __future__ import annotations

import base64
import dataclasses
import json

from publisher import fabric, model, report
from publisher.plan import Plan
from publisher.targets import Artefact
from seed import common as c

# `part` and `artefacts` below are pure functions of the Plan -- they are what
# the contract records and what the Go generator is held to -- so this module
# must be importable with no identity configured at all. That holds because
# `fabric` builds its Credential on first use rather than at import; see the
# note there. A generator that cannot be run without credentials is not a pure
# function of the Plan, whatever its docstring says.


def part(path: str, payload: dict) -> dict:
    return {
        "path": path,
        "payload": base64.b64encode(json.dumps(payload).encode()).decode(),
        "payloadType": "InlineBase64",
    }


def artefacts(plan: Plan, workspace: str, warehouse: str) -> dict[str, dict]:
    """Every definition this target emits, as pure functions of the Plan.

    Kept apart from the REST calls so the contract cases can record them and
    the Go generator can be held to the same bytes.
    """
    columns = {t: list(cols) for t, cols in plan.columns.items()}
    owned = {t: tuple(col["name"] for col in cols) for t, cols in plan.columns.items()}
    measures = list(plan.measures)
    return {
        "model.bim": model.tmsl(plan.name, workspace, warehouse, columns, measures),
        "report.json": report.layout(
            plan.title,
            plan.entity,
            measures,
            list(plan.dimensions),
            list(plan.slicers),
            plan.visual,
        ),
        "definition.pbir": report.binding(plan.name),
        "query.dax": {
            "dax": model.dax_for(
                measures, tuple(c_ for _e, c_ in plan.dimensions), plan.tables, owned
            )
        },
    }


@dataclasses.dataclass(frozen=True)
class PowerBITarget:
    kind = "powerbi"
    authz_tier = "user"
    catalog_service = "das_dashboards"

    workspace: str
    warehouse: str
    warehouse_name: str

    @classmethod
    def from_state(cls, state: dict) -> PowerBITarget:
        return cls(
            workspace=state.get("workspace", ""),
            warehouse=state.get("warehouse", ""),
            warehouse_name=state.get("warehouse_name", ""),
        )

    def accepts(self, candidate: dict, state: dict) -> str | None:
        if candidate.get("source") != self.warehouse_name:
            return (
                f"{candidate.get('source')!r} is not the Fabric warehouse "
                f"({self.warehouse_name!r}); Direct Lake binds to a Fabric item"
            )
        return None

    def publish(self, plan: Plan, *, user_token: str, who: str) -> Artefact:
        parts = artefacts(plan, self.workspace, self.warehouse)
        fabric_token = fabric.on_behalf_of(user_token, fabric.FABRIC_AUDIENCE, who)
        dataset_id = fabric.create_or_update(
            self.workspace,
            "semanticModels",
            "SemanticModel",
            plan.name,
            f"Promoted from a recurring question. {plan.title}.",
            [part("model.bim", parts["model.bim"])],
            fabric_token,
        )
        report_id = fabric.create_or_update(
            self.workspace,
            "reports",
            "Report",
            plan.name,
            plan.title,
            [
                part("report.json", parts["report.json"]),
                part("definition.pbir", parts["definition.pbir"]),
            ],
            fabric_token,
        )
        return Artefact(
            kind=self.kind,
            ids={"semanticModel": dataset_id, "report": report_id},
            url=f"{fabric.FABRIC}/groups/{self.workspace}/reports/{report_id}",
            query=parts["query.dax"]["dax"],
        )

    def evaluate(self, artefact: Artefact, plan: Plan, *, user_token: str) -> list[dict]:
        # A Power BI token, not the control-plane one.
        pbi_token = fabric.on_behalf_of(user_token, fabric.PBI_AUDIENCE, plan.name)
        return fabric.evaluate_dax(
            self.workspace, artefact.ids["semanticModel"], artefact.query, pbi_token
        )

    def catalog(self, artefact: Artefact) -> tuple[str, dict, str]:
        return (
            "PowerBI",
            {
                "type": "PowerBI",
                "clientId": "das",
                "clientSecret": "x",
                "tenantId": c.CFG.get("DAS_TENANT_ID", "local"),
            },
            artefact.url,
        )
