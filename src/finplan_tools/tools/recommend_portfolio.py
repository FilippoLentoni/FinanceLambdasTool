"""Run the selected frozen strategy on an approved snapshot and observed portfolio state."""
from ..core.registry import register_tool
from ._common import project, producer_doc

@register_tool("recommend_portfolio",description="Run the selected portfolio strategy on an approved completed snapshot and stated holdings. Returns advisory allocation and buy/sell/hold deltas with model provenance; no training or trade execution.")
def recommend_portfolio(ctx,request):
    doc=producer_doc(ctx.jobs.recommend_portfolio(request,ctx.meta),"recommend_portfolio")
    return project(doc,"tools/recommend-portfolio-response")
