"""FinanceLambdasTool: thin, stateless MCP adapter Lambdas (OpenSpec change add-mcp-tool-adapters).

Packages: ``core`` (the shared request pipeline, registry, contracts access, errors, identity,
idempotency, bounds, references, audit), ``backends`` (typed producer clients and the SigV4
transport), ``tools`` (one module per tool) and ``handler`` (the Lambda entry point).
The tools hold no authoritative state and mint no platform or model identifiers.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
