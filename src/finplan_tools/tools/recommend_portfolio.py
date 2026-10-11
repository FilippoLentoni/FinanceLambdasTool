"""Read selected strategy recommendations for a saved paper portfolio or supplied state."""
from ..core.registry import register_tool
from ._common import project, producer_doc

@register_tool("recommend_portfolio",description="Get portfolio planning or buy/sell recommendations from the selected policy. Call with {} to automatically load the saved paper portfolio and latest approved completed market snapshot. Optionally supply a complete explicit snapshot/as_of/holdings state. Returns all instrument and cash targets, proposed fractional share and value changes, and policy/data provenance. Read-only: no holdings changes, training or trade execution.")
def recommend_portfolio(ctx,request):
    doc=producer_doc(ctx.jobs.recommend_portfolio(request,ctx.meta),"recommend_portfolio")
    return project(doc,"tools/recommend-portfolio-response")
