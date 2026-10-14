"""Read-only beta crypto paper-research adapters; no payment signing or real orders."""
from ..core.registry import register_tool
from ..core.errors import ToolError
from ._common import producer_doc


def call(ctx, request):
    if ctx.environment != "beta":
        raise ToolError.forbidden("crypto paper research is beta only")
    return producer_doc(ctx.jobs.classical(ctx.spec.name, dict(request), ctx.meta), "crypto research")

@register_tool("get_crypto_experiment", description="Read the hourly synthetic USD 100 crypto experiment: optimization portfolios, cash and buy-and-hold controls, model eligibility, costs and metrics. No live trading.")
def get_crypto_experiment(ctx, request):
    return call(ctx, request)

@register_tool("recommend_crypto_portfolio", description="Read an hourly crypto paper recommendation and model eligibility; equity PPO is incompatible. No order execution.")
def recommend_crypto_portfolio(ctx, request):
    return call(ctx, request)

@register_tool("list_crypto_history", description="Read bounded immutable hourly synthetic crypto portfolio history and comparable net performance.")
def list_crypto_history(ctx, request):
    return call(ctx, request)

@register_tool("discover_research_data", description="Discover optional crypto news and research through public Coinbase x402 Bazaar. Free discovery only; purchases require separate operator approval.")
def discover_research_data(ctx, request):
    return call(ctx, request)

