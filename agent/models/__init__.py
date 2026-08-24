"""One module per model wire protocol.

The list is short and stable where the list of gateways is neither: a gateway
speaks OpenAI's chat completions, Anthropic's messages, or both, so this is
where support for "any gateway" actually lives. Adding a gateway is
configuration; adding a protocol is a file here.
"""
