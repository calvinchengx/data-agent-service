"""Tableau: the Plan spelled as a workbook, published as the person who asked.

The third artefact shape, and the second target with per-user identity.

Power BI takes a semantic model over the source tables; Superset takes a
query; Tableau takes a `.twb`, which is XML carrying its own connection. Three
spellings of one `Plan` is what makes the candidate IR a real intermediate
rather than one tool's model wearing a dataclass.

**This is `user` tier, and §20 said `service` until it was checked.** Connected
Apps with *direct trust* sign a JWT that names the Tableau user, and Tableau
accepts it as a bearer token on both the REST API and VizQL Data Service. That
is an on-behalf-of hop -- so the asking person is the one Tableau records, and
VDS applies their row-level security when the answer is verified. It is the
same property Power BI has and Superset cannot have.

**Everything in this module above the tenant line is a pure function of the
Plan**, and is recorded in `publisher/contract/cases.json` like every other
artefact: the workbook, the query VDS will be asked, and the claims the token
will carry. `publish` and `evaluate` need a Tableau site, which has no
container -- that hop lives in `docs/parity.md` beside the Azure rows.

The workbook's structure follows Tableau's documented element semantics: a
live `federated` connection, and the guarded template as a **custom SQL
relation** (`<relation type='text'>`), which is the same property Superset's
virtual dataset has -- Tableau sees only the columns the template projected,
never the tables behind it. It has NOT been opened by a real Tableau. That is
exactly what the parity row is for, and this docstring says so rather than
letting a green witness imply otherwise.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import json
import urllib.parse
import xml.etree.ElementTree as ET

from publisher.plan import Plan, projection
from publisher.targets import Artefact

# The workbook format version. Tableau refuses a version it does not know, and
# a version newer than the site's is the failure that reads as a corrupt file.
VERSION = "18.1"
NS_USER = "http://www.tableausoftware.com/xml/user"

# The Plan's visual vocabulary, as Tableau mark classes. A text table is what
# Tableau calls both a card and a crosstab; the difference is whether anything
# is on columns, which the Plan's dimensions already decide.
MARKS = {"card": "Text", "bar": "Bar", "table": "Text"}

# What the connected-app token is allowed to do. Narrow on purpose: publishing
# a promoted dashboard and reading data back is the whole job, and a token
# that could also delete content would be a larger blast radius than the act
# it exists to perform.
SCOPES = (
    "tableau:content:read",
    "tableau:workbooks:create",
    "tableau:viz_data_service:read",
)

# Tableau's own datatype names, from the Plan's target-neutral ones.
DATATYPES = {"int64": "integer", "double": "real", "dateTime": "datetime", "string": "string"}


class TableauNotConfigured(RuntimeError):
    """No Tableau site is configured, so the live hop cannot be attempted.

    Raised rather than returning empty: a publisher that silently did nothing
    would report success for a dashboard nobody could open, which is the
    failure this whole package exists to prevent.
    """


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def claims(
    *, client_id: str, kid: str, username: str, expires_at: int, jti: str
) -> tuple[dict, dict]:
    """The connected-app JWT's header and payload, as Tableau reads them.

    `sub` is the ASKING PERSON, which is the whole point: a connected app with
    direct trust does not act as itself, it acts as the user it names. The
    header carries `kid` (the secret's id) and `iss` (the connected app's
    client id) because Tableau resolves the signing secret from those before
    it can verify anything.

    Time is passed in rather than read: this is a pure function so the
    contract can record what it produces, and a token whose bytes depend on
    the clock could not be compared between two generators.
    """
    header = {"alg": "HS256", "typ": "JWT", "kid": kid, "iss": client_id}
    payload = {
        "iss": client_id,
        "sub": username,
        "aud": "tableau",
        "jti": jti,
        "exp": expires_at,
        "scp": list(SCOPES),
    }
    return header, payload


def token(*, secret: str, header: dict, payload: dict) -> str:
    """The signed JWT. HS256, which is what direct trust uses."""
    signing_input = (
        _b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        + "."
        + _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


def _connection_attrs(dsn: str) -> dict[str, str]:
    """A `<connection>`'s attributes, from the source's own DSN.

    No password: the workbook carries a LIVE connection and Tableau prompts
    for or stores the credential itself. A `.twb` with a password in it is a
    secret in a file someone will email, which is the reason `DAS_SOURCES`
    keeps the credential out of the DSN in the first place.
    """
    parts = urllib.parse.urlsplit(dsn)
    user = parts.netloc.rpartition("@")[0].partition(":")[0]
    host = parts.netloc.rpartition("@")[2]
    server, _, port = host.partition(":")
    return {
        "class": "postgres",
        "dbname": parts.path.lstrip("/"),
        "server": server,
        "port": port or "5432",
        "username": user,
        # Live, not an extract. An extract is a copy of the source that can
        # disagree with it -- the same argument `model.py` makes for Direct
        # Lake over an import model, for the same reason.
        "authentication": "auth-user",
    }


def workbook(plan: Plan, dsn: str) -> str:
    """The `.twb`, as a deterministic function of the Plan.

    One datasource carrying the guarded template as custom SQL, one worksheet
    whose mark class follows the shape of the answer, one dashboard holding
    it. Names are derived from `plan.name` so republishing the same recurring
    question updates rather than accumulating near-identical workbooks.
    """
    names = projection(plan.comparison_sql)
    dims = names[: len(plan.dimensions)]
    measures = names[len(plan.dimensions) :]
    types = {c["name"]: c.get("dataType", "string") for cols in plan.columns.values() for c in cols}

    ds_name = f"federated.{plan.name}"
    conn_name = f"postgres.{plan.name}"
    relation = "Custom SQL Query"

    root = ET.Element("workbook", {"version": VERSION, "xmlns:user": NS_USER})
    datasources = ET.SubElement(root, "datasources")
    datasource = ET.SubElement(
        datasources,
        "datasource",
        {"caption": plan.title, "inline": "true", "name": ds_name, "version": VERSION},
    )
    federated = ET.SubElement(datasource, "connection", {"class": "federated"})
    named = ET.SubElement(federated, "named-connections")
    named_connection = ET.SubElement(
        named, "named-connection", {"caption": plan.source, "name": conn_name}
    )
    ET.SubElement(named_connection, "connection", _connection_attrs(dsn))

    # THE security property. `type='text'` is Tableau's custom SQL, so the
    # workbook carries the template the executor already guarded -- not the
    # tables behind it. A `type='table'` relation would hand Tableau the whole
    # table and let it reach a column the access rules withheld.
    rel = ET.SubElement(
        federated, "relation", {"connection": conn_name, "name": relation, "type": "text"}
    )
    rel.text = plan.comparison_sql

    cols = ET.SubElement(federated, "cols")
    for column in names:
        ET.SubElement(cols, "map", {"key": f"[{column}]", "value": f"[{relation}].[{column}]"})

    for column in dims:
        ET.SubElement(
            datasource,
            "column",
            {
                "datatype": DATATYPES.get(types.get(column, "string"), "string"),
                "name": f"[{column}]",
                "role": "dimension",
                "type": "nominal",
            },
        )
    for column, measure in zip(measures, plan.measures, strict=True):
        ET.SubElement(
            datasource,
            "column",
            {
                # The catalog's name for it, the same one the Power BI measure
                # and the Superset metric carry. A dashboard whose axis label
                # disagrees with the glossary is a dashboard people argue with.
                "caption": measure.name,
                "datatype": DATATYPES.get(types.get(column, "double"), "real"),
                "name": f"[{column}]",
                "role": "measure",
                # The aggregation already happened in the guarded SQL, so the
                # workbook must READ the value rather than aggregate it again.
                # Sum over one row per group is identity; Count would be 1 --
                # the same trap Superset's metric had, in a third spelling.
                "aggregation": "Sum",
                "type": "quantitative",
            },
        )

    worksheets = ET.SubElement(root, "worksheets")
    worksheet = ET.SubElement(worksheets, "worksheet", {"name": plan.title})
    table = ET.SubElement(worksheet, "table")
    view = ET.SubElement(table, "view")
    view_ds = ET.SubElement(view, "datasources")
    ET.SubElement(view_ds, "datasource", {"caption": plan.title, "name": ds_name})
    panes = ET.SubElement(table, "panes")
    pane = ET.SubElement(panes, "pane")
    ET.SubElement(pane, "view")
    ET.SubElement(pane, "mark", {"class": MARKS[plan.visual]})
    ET.SubElement(table, "rows").text = "".join(f"[{ds_name}].[{m}]" for m in measures)
    ET.SubElement(table, "cols").text = "".join(f"[{ds_name}].[{d}]" for d in dims)

    dashboards = ET.SubElement(root, "dashboards")
    dashboard = ET.SubElement(dashboards, "dashboard", {"name": plan.title})
    zones = ET.SubElement(dashboard, "zones")
    ET.SubElement(zones, "zone", {"name": plan.title, "type": "layout-basic", "id": "1"})

    return (
        "<?xml version='1.0' encoding='utf-8' ?>\n" + ET.tostring(root, encoding="unicode") + "\n"
    )


def vds_query(plan: Plan, datasource_luid: str) -> dict:
    """What VizQL Data Service will be asked, when there is a site to ask.

    The fields are the template's OWN output columns, and every measure is
    read back rather than re-aggregated -- `SUM` over one row per group is the
    value itself, where `COUNT` would be 1. Recorded in the contract so the
    query is comparable even though nothing can run it yet.
    """
    names = projection(plan.comparison_sql)
    dims = names[: len(plan.dimensions)]
    measures = names[len(plan.dimensions) :]
    fields: list[dict] = [{"fieldCaption": d} for d in dims]
    fields += [{"fieldCaption": m, "function": "SUM"} for m in measures]
    return {
        "datasource": {"datasourceLuid": datasource_luid},
        "query": {"fields": fields},
    }


@dataclasses.dataclass(frozen=True)
class TableauTarget:
    kind = "tableau"
    # Connected Apps with direct trust sign a token naming the asking user, so
    # Tableau records them and VDS applies their row-level security. Not
    # aspirational: the token this module builds carries `sub`.
    authz_tier = "user"
    catalog_service = "das_dashboards_tableau"

    site: str
    site_id: str
    project_id: str
    client_id: str
    # Tableau calls this the connected app's "Secret ID", and it is an
    # IDENTIFIER, not a key: it travels in the clear as the JWT `kid` header on
    # every token we sign. Named `kid` here rather than `secret_id` because
    # code scanning classified the field as sensitive on its name alone and
    # followed it to a print -- a false positive it was right to raise, since a
    # reader would have made the same mistake. `DAS_TABLEAU_SECRET_ID` keeps
    # Tableau's own word: that is the operator's contract, not ours.
    kid: str
    # The signing key itself, and the one field here that IS a secret. Refused
    # unless it is a `keyvault:` reference (scripts/tableau_check.py).
    secret_ref: str
    source_name: str
    dsn: str

    @classmethod
    def from_state(cls, state: dict) -> TableauTarget:
        from seed import common as c

        source = str(c.CFG.get("DAS_TABLEAU_SOURCE", "contoso_support"))
        dsn = ""
        for src in json.loads(c.CFG.get("DAS_SOURCES", "[]")):
            if src.get("name") == source:
                dsn = str(src.get("dsn", ""))
        return cls(
            site=str(c.CFG.get("DAS_TABLEAU_URL", "")),
            site_id=str(c.CFG.get("DAS_TABLEAU_SITE", "")),
            project_id=str(c.CFG.get("DAS_TABLEAU_PROJECT", "")),
            client_id=str(c.CFG.get("DAS_TABLEAU_CLIENT_ID", "")),
            kid=str(c.CFG.get("DAS_TABLEAU_SECRET_ID", "")),
            # The reference, never the secret, and never resolved in a
            # constructor -- `targets.configured()` builds every target just to
            # ask which accepts a candidate.
            secret_ref=str(c.CFG.get("DAS_TABLEAU_SECRET", "")),
            source_name=source,
            dsn=dsn,
        )

    @property
    def configured(self) -> bool:
        return bool(self.site and self.client_id and self.kid and self.secret_ref)

    def accepts(self, candidate: dict, state: dict) -> str | None:
        if candidate.get("source") != self.source_name:
            return (
                f"{candidate.get('source')!r} is not the source this Tableau site reads "
                f"({self.source_name!r})"
            )
        if not self.dsn:
            return f"no DSN for {self.source_name!r} in DAS_SOURCES; nothing to connect"
        if not self.configured:
            # A REASON, not a crash, and not a silent skip. Until a developer
            # sandbox exists there is no site to publish to, and saying so is
            # what stops it reading as a defect in the candidate.
            return (
                "no Tableau site configured (DAS_TABLEAU_URL / _CLIENT_ID / _SECRET_ID / "
                "_SECRET); the generator is witnessed in CI, the live hop needs a tenant "
                "— see docs/parity.md"
            )
        return None

    def artefacts(self, plan: Plan) -> dict[str, str]:
        """Everything this target emits that needs no tenant."""
        return {"workbook.twb": workbook(plan, self.dsn)}

    def bearer(self, username: str, *, expires_at: int, jti: str) -> str:
        from vaultref import resolve

        header, payload = claims(
            client_id=self.client_id,
            kid=self.kid,
            username=username,
            expires_at=expires_at,
            jti=jti,
        )
        return token(secret=resolve(self.secret_ref), header=header, payload=payload)

    def publish(self, plan: Plan, *, user_token: str, who: str) -> Artefact:
        if not self.configured:
            raise TableauNotConfigured(
                "publishing needs a Tableau site; the workbook generator does not, and is "
                "witnessed in CI. See docs/parity.md for what has been proved where."
            )
        raise TableauNotConfigured(
            "the live publish hop is not built: it needs a developer sandbox to be "
            "witnessed against, and an unwitnessed publish path is what docs/parity.md "
            "exists to keep out of the ✅ column."
        )

    def evaluate(self, artefact: Artefact, plan: Plan, *, user_token: str) -> list[dict]:
        raise TableauNotConfigured(
            "the VizQL Data Service hop is not built; `vds_query` records what it will ask."
        )

    def catalog(self, artefact: Artefact) -> tuple[str, dict, str]:
        return (
            "Tableau",
            {
                "type": "Tableau",
                "hostPort": self.site,
                "siteUrl": self.site_id,
                "authType": {"clientId": self.client_id, "secretId": self.kid},
            },
            artefact.url,
        )
