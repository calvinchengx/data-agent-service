"""Recurring-question promotion, without storing anyone's question.

See docs/00-plan.md §17. The promoter reads the executor's audit log, reduces
each query to a literal-free template, and counts templates by pseudonymous
user. No natural language enters this package at any point — the privacy
argument is that the data is not here, which is the only argument that
survives a subpoena.
"""
