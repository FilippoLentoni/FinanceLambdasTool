"""List published plan baselines with pagination."""
from ..core.registry import register_tool
from ._common import project, producer_doc

@register_tool("list_publications",description="List published plan baselines with pagination.")
def list_publications(ctx,request):
    doc=producer_doc(ctx.platform.list_publications(request["plan_id"],ctx.meta, **{k:request[k] for k in ("page_size","next_token") if k in request}),"list_publications")
    return project(doc,"tools/list-publications-response")
