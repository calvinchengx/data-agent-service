"""The audit line's surfaces, and the cap that decides what may be promoted.

§21, hazard 2. A payload clipped at the executor's cap does not describe the
call that was made. SQL announces this by failing to parse; a URL missing its
last query parameter parses perfectly and describes a NARROWER call than the
one that ran -- so it would produce a confidently wrong template that looks
like a working feature. These pin the refusal before HTTP promotion exists,
because afterwards there is nothing to see.
"""

from __future__ import annotations

import json
import pathlib

from promoter import audit

ROOT = pathlib.Path(__file__).resolve().parents[1]


def line(**record) -> audit.AuditLine:
    return next(audit.parse(["INFO audit " + json.dumps(record)]))


def test_the_caps_still_match_the_executor_that_writes_them():
    """The promoter hardcodes caps another process applies.

    If `app.py` changes one and this does not, `truncated` silently becomes
    the wrong answer -- and it is the answer that decides whether a clipped
    payload is turned into a template. Read here rather than trusted.
    """
    app = (ROOT / "services" / "warehouse-query-py" / "app.py").read_text()
    assert f"sql=verdict.sql[:{audit.SQL_CAP}]" in app
    assert f"url=verdict.url[:{audit.URL_CAP}]" in app


def test_a_sql_line_and_an_http_line_name_their_surface():
    assert line(op="run_query", sql="SELECT 1").surface == "sql"
    assert line(op="call_operation", url="http://x/y").surface == "http"
    # An operation with no canonicaliser names no surface rather than
    # defaulting to SQL, which is how the executors treat an unknown kind.
    assert line(op="describe_table").surface == ""


def test_a_url_clipped_at_the_cap_is_truncated():
    clipped = "http://host/api/tickets?" + "a" * audit.URL_CAP
    assert line(op="call_operation", url=clipped).truncated
    assert not line(op="call_operation", url="http://host/api/tickets?team=x").truncated


def test_the_sql_cap_is_not_applied_to_a_url():
    """A URL is capped at 300 and SQL at 1000.

    Judging a URL by the SQL cap would pass every clipped URL there is --
    300 is below 1000, so nothing would ever be refused.
    """
    clipped = "u" * audit.URL_CAP
    assert len(clipped) < audit.SQL_CAP
    assert line(op="call_operation", url=clipped).truncated


def test_a_sql_line_is_still_judged_by_the_sql_cap():
    assert line(op="run_query", sql="S" * audit.SQL_CAP).truncated
    assert not line(op="run_query", sql="SELECT 1").truncated


def test_the_http_payload_survives_parsing():
    got = line(op="call_operation", operation="listTickets", url="http://h/t?team=billing")
    assert got.operation == "listTickets"
    assert got.url == "http://h/t?team=billing"


def test_http_lines_are_not_promotable_yet_and_say_so_by_op():
    """Unit 8 opens this; until then the surface is carried, not promoted."""
    got = line(op="call_operation", verdict="ok", url="http://h/t", operation="listTickets")
    assert got.surface == "http"
    assert not got.promotable
