# Upstream issues (proposed, not patched)

Discipline rule 1: dependencies are used as-is. Anything that looks like a bug or gap in an emulator or OpenMetadata is recorded here with a repro and routed around.

| # | Component | Observation | Repro | Expected | Workaround here | Status |
|---|---|---|---|---|---|---|
| 1 | azure-apim-emulator | No MCP OAuth discovery (`/.well-known/oauth-protected-resource`, RFC 9728) or per-tool policy scoping (both listed pending in its parity.md) | — | Gateway serves resource metadata and can scope tools | Our service serves the discovery documents; agent enforces tool allow-list; OM bot is read-only | open |
