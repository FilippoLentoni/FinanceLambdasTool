"""Read recorded paper or simulated account statements; does not record or execute trades."""
from ..core.registry import register_tool
from ._common import project, producer_doc

@register_tool("list_executions",description="Read recorded paper or simulated account statements; does not record or execute trades.")
def list_executions(ctx,request):
    doc=producer_doc(ctx.platform.list_executions(request["publication_id"],ctx.meta, **{k:request[k] for k in ("page_size","next_token") if k in request}),"list_executions")
    return project(doc,"tools/list-executions-response")
