---
name: om-context-native
description: Use the catalog's assembled-context tools instead of search + entity reads
when: context=native
---

Loaded when `DAS_OM_CONTEXT_MODE=native` and the catalog exposes `find_context` / `get_asset_context`. These replace steps 1–3 of `om-grounded-sql`; steps 4–6 and the abstention rules are unchanged.

- Call `find_context` once with the question's business terms together, not one term at a time. It returns glossary terms, metrics, and candidate assets already related to each other.
- Call `get_asset_context` on each asset you intend to query; it returns the table with its column descriptions, tags, keys, and linked terms in one payload. Still call the warehouse's `describe_table` — the catalog describes meaning, the executor describes what is physically there, and the two can disagree (say so if they do).
- A metric's `expression` is still the definition; the assembled context does not change that.
- If `find_context` returns nothing relevant, fall back to `search_metadata` per term before abstaining, and name both attempts in the abstention.
