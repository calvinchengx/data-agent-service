# Adding a dashboard target

A dashboard target is a tool a promoted question can be published into. Adding
one is a class behind `DashboardTarget`, a name in `DAS_DASHBOARD_TARGETS`, and
a row in the contract cases. Nothing above it changes — not the promoter, not
the executor, not the agent, not the verification.

This is the sibling of [09-adding-a-source](09-adding-a-source.md) and the two
are deliberately symmetric, but they point opposite ways: a `SourceBackend`
**reads from** an engine, a `DashboardTarget` **writes to** a tool and then
reads back out of it to check what it wrote.

## The Plan is the input

```
candidate ──► Plan ──► target.publish ──► target.evaluate ──► compare ──► catalog
             neutral      your code           your code        neutral    your serviceType
```

`publisher/plan.py` resolves a released candidate against the executor's own
`describe_table` output and produces a `Plan`: a name, a title, the tables,
those column lists, `Measure`s carrying a **function** and no expression,
dimensions and slicers as `(entity, column)`, a visual in `{card, bar, table}`,
and the comparison SQL with its slot predicates dropped.

Everything a target does is a function of that object and its own settings.
`publisher/contract/plan.schema.json` is its schema; read it before writing
code, because the vocabulary is closed on purpose — `AVERAGE` is the Plan's
word and your target maps it to whatever its engine says.

## What to implement

```python
class MyTarget:
    kind = "mytool"
    authz_tier = "user" | "service"
    catalog_service = "das_dashboards_mytool"

    @classmethod
    def from_state(cls, state: dict) -> "MyTarget": ...

    def accepts(self, candidate: dict, state: dict) -> str | None: ...
    def publish(self, plan, *, user_token: str, who: str) -> Artefact: ...
    def evaluate(self, artefact, plan, *, user_token: str) -> list[dict]: ...
    def catalog(self, artefact) -> tuple[str, dict, str]: ...
```

Register it in `publisher/targets/registry()`, and name it in
`DAS_DASHBOARD_TARGETS`. A name with no target behind it is refused at
startup, for the same reason the executors refuse an unknown source `kind`: a
setting that silently does nothing is a setting nobody can trust.

### `accepts` returns a reason, not a boolean

An earlier version of `publisher/run.py` had this instead:

```python
if candidate["source"] != state.get("warehouse_name"):
    continue
```

Every candidate from `postgres` was dropped on the floor with a one-line
print, and nothing said what a person would have to change. Return the reason:
`"'contoso_support' is not the Fabric warehouse; Direct Lake binds to a Fabric
item"` is a sentence someone can act on.

### If your tool renders a QUERY, the metric is a pass-through

Power BI takes a semantic model over the source tables, so its measures do the
aggregation. A tool that takes a query does not: `plan.comparison_sql` has
already aggregated, and the dataset holds one row per group with the answer
in it. Mirroring the measure's own function aggregates a second time.

That is harmless for `SUM` and `AVG` over a single value and **wrong for
`COUNT`, which returns 1 for every group** — a plausible number, on a
dashboard, that nobody would question. `SupersetTarget` uses `MAX`, which is
identity over one row per group for every measure kind the Plan emits. Check
this against your tool with a `COUNT` template before you trust it.

### `evaluate` must go through the tool's own engine

Not through a re-run of the SQL, and not through what you sent. The whole
point of the comparison is that a dashboard's *engine* — DAX, Superset's query
layer, whatever — answers the same question the SQL did. Asking the target to
echo back its own definition proves nothing and would pass forever.

`compare()` takes rows as `{label: value}` and matches them as sets of
`(label, values)`, rounded to four places. It refuses two empty results and
refuses rows carrying no measure value at all — agreement has to be evidence
of something.

### Slots are filters with **no default**

Every target renders each slicer unset. §17 keeps the literals out of the
store deliberately, so there is no default to restore; inventing one puts a
filter on the page nobody chose and everybody reads as the organisation's own.

### Say which tier you are honestly

`authz_tier = "user"` means the tool records the asking person, through an
on-behalf-of exchange. `"service"` means it does not, and the dashboard is
read with a shared credential — in which case bound what it can reach (publish
the *template* as the dataset, not the table) and record the asker as owner in
the catalog, because the tool did not.

Do not label a target `"user"` because you would like it to be.

Two things follow from `"service"` that are easy to skip:

* **Bound what the credential can reach.** Publish the *template* as the
  dataset, not the table. `SupersetTarget` sends `plan.comparison_sql` as a
  virtual dataset, so Superset sees only the columns the template projected
  and cannot widen the surface the executor's access rules narrowed. A
  physical dataset would hand one shared credential the whole table.
* **The catalog is the only record of who asked.** Pass `owner` to
  `record_lineage`, which resolves it to an OpenMetadata `owners` reference —
  `seed.authz.om_people()` provisions one user per persona for this. Do not
  settle for the description: it is prose, and OM HTML-escapes it, so `@`
  comes back as `&#64;`.

### Do not resolve a secret in your constructor

`targets.configured()` builds every target just to ask which one accepts a
candidate. A `from_state` that reached a vault would make *listing* the
targets need a credential — and unit tests that never touch Superset would
fail on a missing `DAS_KEYVAULT_URL`. Keep the `keyvault:` reference in the
dataclass and resolve it when a call is about to be made.

`publisher/fabric.py` had the same defect one layer down, building a
`Credential` at import. It is an easy shape to reproduce.

## Add it to the contract

`publisher/contract/gen_cases.py` records what each target produces for each
case. Add your target to the `targets` dict, run it, commit the regenerated
`cases.json`, and CI will regenerate and diff on every push. If you also write
a Go generator in `publisher-go/`, it is held to those same bytes.

Two rules that cost nothing and have both already paid:

- **Record refusals beside successes.** A corpus of only successes passes
  against a function that never refuses anything. The `bindings` section
  exists for this, and caught a real divergence on its first run.
- **Write the property, not just the cases.** "Feed the output back into the
  thing that produced it and demand the same answer" is about forty lines and
  killed a canonicalisation bug the day it was written. If your target has a
  transform whose result could depend on how it was *reached* rather than on
  what it *means*, that property will find it.

```bash
uv run python publisher/contract/gen_cases.py
make publisher-contract
```

## Add a witness

The witness must **create whatever it asserts on, in the same function**.
Three separate phases have now been written that read state a manual run had
left behind — a file the promoter writes, then twice a catalog entry — and all
three passed locally and failed in CI, which has a clean catalog every time.
There is no version of "the promoter will have run" that is true in CI.

## What decides whether a target is worth building

In this order:

1. **Is it in OpenMetadata's `dashboardService` enum?** If not, the published
   dashboard has no catalog entity and no lineage — and a promoted number with
   no lineage is the thing this project exists to avoid.
2. **Can its engine be queried back headlessly?** If not, `evaluate` cannot be
   written and you would be publishing blind.
3. **Is there a container?** If not, the *live hop* belongs in `parity.md` as
   a hosted row — but see below, because this filter decides less than it
   looks like it does.

Tableau passes the first two and fails the third: it publishes open-source
*clients* (`tableauserverclient`, `document-api-python`), but a client is the
wrong half of a witness. A witness needs something on the other side that can
say no.

## Split at the tenant line, do not defer past it

The third filter is a reason to split a target, not to postpone one. Most of a
target is a **pure function of the `Plan`** — the artefact it emits, the query
it will ask, the token it will present — and none of that needs a tenant:

| Above the line — witnessed in CI | Below — needs a tenant |
|---|---|
| the artefact (`.twb`, TMSL, a dataset body) | creating it on the server |
| the query the tool will be asked | running it |
| the token, and whose name is in it | presenting it |
| `accepts()`, including the refusal when unconfigured | — |

`TableauTarget` is built this way: `workbook()`, `vds_query()` and `claims()`
are pure and recorded in `publisher/contract/cases.json`, `publish()` refuses
by name, and `docs/parity.md` carries the live hop as 🔴 with the reason.

Two rules make the split honest rather than a way of claiming credit:

- **The refusal must name the ledger.** `accepts()` returns "…the generator is
  witnessed in CI, the live hop needs a tenant — see docs/parity.md". A reader
  who hits it should land on the honest record, not conclude the candidate is
  at fault.
- **The witness must say what it does not prove.** phase20 asserts the
  generator *and* asserts that `parity.md` still says "not yet". A green
  witness beside a ledger claiming the hop was proved is exactly the failure
  this repo keeps finding.

Deferring Tableau because it had no container was the wrong call once already,
and it was propped up by a second error: §20 recorded it as `service` tier when
Connected Apps with direct trust make it `user`. Check the tier against the
tool's documentation before you rank a target by it.
