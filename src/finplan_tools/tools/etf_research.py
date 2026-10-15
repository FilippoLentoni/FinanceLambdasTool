"""Read-only ETF news feature evidence; no paid requests or brokerage orders."""
from ..core.registry import register_tool
from ..core.errors import ToolError
from ._common import producer_doc

@register_tool('get_etf_research_inputs',description='Read point-in-time ETF news features, licensed source provenance, optimizer sensitivity and PPO/LLM readiness. Unknown rights or timestamps are excluded. No live orders or accuracy claims.')
def get_etf_research_inputs(ctx,request):
    if ctx.environment!='beta':raise ToolError.forbidden('ETF research is beta only')
    return producer_doc(ctx.jobs.classical(ctx.spec.name,dict(request),ctx.meta),'ETF research')
