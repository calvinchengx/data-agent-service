"""What the catalog calls each column, reused from the promoter.

The same rule, deliberately: a glossary term names a column only when it is
that term's sole bearer. A dashboard is where a wrong name does the most
damage, because it is the version people quote in a meeting.
"""

from __future__ import annotations

from promoter.catalog import column_names


def for_columns() -> dict[str, str]:
    return column_names()
