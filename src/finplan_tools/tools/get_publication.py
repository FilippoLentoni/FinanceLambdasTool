"""Read the immutable publication and its exact plan-version checksum."""
from ..core.registry import register_tool
from ._common import project, producer_doc

@register_tool("get_publication",description="Read the immutable publication and its exact plan-version checksum.")
def get_publication(ctx,request):
    doc=producer_doc(ctx.platform.get_publication(request["publication_id"],ctx.meta),"get_publication")
    return project({"publication":doc},"tools/get-publication-response")
