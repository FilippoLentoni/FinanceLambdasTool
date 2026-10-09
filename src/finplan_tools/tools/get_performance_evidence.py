"""Reconcile a published plan against recorded paper/simulated account outcomes and return deterministic gap evidence, diagnostic whys and feedback actions."""
from ..core.registry import register_tool
from ._common import project, producer_doc

@register_tool("get_performance_evidence",description="Reconcile a published plan against recorded paper/simulated account outcomes and return deterministic gap evidence, diagnostic whys and feedback actions.")
def get_performance_evidence(ctx,request):
    doc=producer_doc(ctx.jobs.get_performance_evidence(request,ctx.meta),"get_performance_evidence")
    return project(doc,"tools/get-performance-evidence-response")
