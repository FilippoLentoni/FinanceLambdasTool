"""Read-only financial news feature evidence; no paid requests or brokerage orders."""
from ..core.registry import register_tool
from ..core.errors import ToolError
from ._common import producer_doc
from .classical import _validate_public_citation

# Only validated scalar URL leaves; titles, source metadata and model subtrees
# continue through the ordinary storage/ARN/account-reference guard.
_MAX_CONTEXT=100
_CITATION_POINTERS=tuple(f'/models/llm/context/{i}/url' for i in range(_MAX_CONTEXT))


@register_tool('get_etf_research_inputs',description='Read point-in-time financial news features for ETFs and supported equity research symbols such as GOOGL, licensed source provenance, optimizer sensitivity and PPO/LLM readiness. Unknown rights or timestamps are excluded. No live orders or accuracy claims.',grant_pointers=_CITATION_POINTERS)
def get_etf_research_inputs(ctx,request):
    if ctx.environment!='beta':raise ToolError.forbidden('Financial news research is beta only')
    document=producer_doc(ctx.jobs.classical(ctx.spec.name,dict(request),ctx.meta),'Financial news research')
    models=document.get('models',{})
    if not isinstance(models,dict) or not isinstance(models.get('llm',{}),dict):
        raise ToolError.internal('the producer returned invalid financial research model metadata')
    context=models.get('llm',{}).get('context',[])
    if not isinstance(context,list) or len(context)>_MAX_CONTEXT:
        raise ToolError.internal('the producer returned invalid financial research citations')
    for row in context:
        _validate_public_citation(row.get('url') if isinstance(row,dict) else None)
    return document
