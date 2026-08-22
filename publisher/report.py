"""The report: PBIR, one page, the visual chosen by the shape of the answer.

Chosen, not asked for. A candidate that groups by one dimension is a bar
chart; one that groups by nothing is a card; several dimensions is a table.
That rule is short enough to state and therefore short enough to argue with,
which a model's taste is not.

Every slot the promoter recorded becomes a slicer with NO default value. §17
keeps the literals out of the store deliberately, so there is no default to
restore -- and inventing one would put a filter on the page that nobody chose
and everybody would read as the organisation's own.
"""

from __future__ import annotations

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"


def visual_type(dimensions: tuple[str, ...]) -> str:
    if not dimensions:
        return "card"
    if len(dimensions) == 1:
        return "barChart"
    return "tableEx"


def binding(model_name: str) -> dict:
    """definition.pbir -- what this report reads.

    byPath, relative, because a report that names its model by id cannot be
    moved between workspaces without editing, and the promotion flow creates
    both items together.
    """
    return {
        "version": "1.0",
        "datasetReference": {"byPath": {"path": f"../{model_name}.SemanticModel"}},
    }


def _field(entity: str, column: str) -> dict:
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": column}}


def _measure(entity: str, name: str) -> dict:
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def layout(
    title: str,
    entity: str,
    measures: list,
    dimensions: list[tuple[str, str]],
    slicers: list[tuple[str, str]],
) -> dict:
    """report.json -- one page, one visual, a slicer per slot."""
    visual = visual_type(tuple(d[1] for d in dimensions))
    containers = [
        {
            "x": 0,
            "y": 0,
            "width": 960,
            "height": 480,
            "config": {
                "name": "answer",
                "title": title,
                "singleVisual": {
                    "visualType": visual,
                    "projections": {
                        "Category": [
                            {"queryRef": f"{e}.{c}", "field": _field(e, c)} for e, c in dimensions
                        ],
                        "Y": [
                            {"queryRef": f"{entity}.{m.name}", "field": _measure(entity, m.name)}
                            for m in measures
                        ],
                    },
                },
            },
        }
    ]
    # A slicer per recorded slot, each one empty. The column is known because
    # the promoter kept it; the value is not, because it deliberately did not.
    for i, (slicer_entity, column) in enumerate(slicers):
        containers.append(
            {
                "x": 0,
                "y": 480 + i * 120,
                "width": 320,
                "height": 100,
                "config": {
                    "name": f"slicer-{column}",
                    "singleVisual": {
                        "visualType": "slicer",
                        "projections": {
                            "Values": [
                                {
                                    "queryRef": f"{slicer_entity}.{column}",
                                    "field": _field(slicer_entity, column),
                                }
                            ]
                        },
                    },
                },
            }
        )
    return {
        "$schema": f"{SCHEMA}/report/1.0.0/schema.json",
        "sections": [
            {
                "name": "page1",
                "displayName": title,
                "width": 1280,
                "height": 720,
                "visualContainers": containers,
            }
        ],
    }
