"""Recommendation from FinanceModel's frozen PPO policy trained on exactly the requested universe."""
from ..core.registry import register_tool
from ._common import producer_doc, project, require_requested_universe

TOOL = "generate_portfolio_recommendation_ppo"


@register_tool(TOOL, description="Generate a portfolio recommendation with the PPO model: FinanceModel's frozen PPO policy trained on exactly the requested instruments (required, 1-100 symbols); if no frozen policy was trained on that universe it returns a clear no-policy error instead of another universe's weights. Call with only instruments to load the saved paper portfolio and latest approved completed market snapshot, or supply a complete explicit snapshot/as_of/holdings scenario. Returns cash and target weights only for those instruments, proposed fractional share and value changes, and policy/data provenance. Read-only: no holdings changes, training or trade execution.")
def generate_portfolio_recommendation_ppo(ctx, request):
    doc = producer_doc(ctx.jobs.recommend_portfolio(request, ctx.meta), "PPO recommendation")
    require_requested_universe(doc, request["instruments"])
    return project(doc, "tools/generate-portfolio-recommendation-ppo-response")
