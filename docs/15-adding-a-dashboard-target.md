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

## Add it to the contract

`publisher/contract/gen_cases.py` records what each target produces for each
case. Add your target to the `targets` dict, run it, commit the regenerated
`cases.json`, and CI will regenerate and diff on every push. If you also write
a Go generator in `publisher-go/`, it is held to those same bytes.

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
3. **Is there a container?** If not, nothing can be witnessed in CI, and the
   target belongs in `parity.md` as a hosted row rather than in the suite.

Tableau passes the first two and fails the third: it publishes open-source
*clients* (`tableauserverclient`, `document-api-python`), but a client is the
wrong half of a witness. A witness needs something on the other side that can
say no.
