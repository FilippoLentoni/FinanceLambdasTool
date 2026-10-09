"""Direct, same-environment invocation of FinanceModel's frozen strategy service."""
from __future__ import annotations

import json
import re

from ..core.errors import ToolError, from_producer_envelope

MAX_RESPONSE_BYTES = 65536


class StrategyLambdaClient:
    def __init__(self, client, references, *, environment, region, account):
        self.client, self.references = client, references
        self.environment, self.region, self.account = environment, region, account

    def recommend(self, body, meta):
        ref = self.references.get("financemodel", "api", "strategy-function-ref")
        if not ref:
            raise ToolError.dependency_unavailable("no selected-strategy service is released in this environment")
        pattern = rf"arn:aws:lambda:{re.escape(self.region)}:{re.escape(self.account)}:function:finplan-{re.escape(self.environment)}-financemodel-job-api-handler-inference"
        if not re.fullmatch(r"[0-9]{12}", self.account) or not re.fullmatch(pattern, ref):
            raise ToolError.dependency_unavailable("strategy reference does not name this environment's serving function")
        payload = {"environment": self.environment, "request": dict(body), "headers": meta.headers()}
        try:
            response = self.client.invoke(FunctionName=ref, InvocationType="RequestResponse", Payload=json.dumps(payload, allow_nan=False).encode())
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "AccessDeniedException":
                raise ToolError.forbidden("strategy service invocation was denied") from None
            raise ToolError.dependency_unavailable("strategy service could not be reached") from None
        if response.get("FunctionError") or response.get("StatusCode") != 200:
            raise ToolError.dependency_unavailable("strategy service did not complete successfully")
        stream = response.get("Payload")
        if stream is None or not hasattr(stream, "read"):
            raise ToolError.internal("strategy service returned no response payload")
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ToolError.internal("strategy response exceeds the tool response bound")
        try:
            doc = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, TypeError):
            raise ToolError.internal("strategy service returned malformed JSON") from None
        if not isinstance(doc, dict):
            raise ToolError.internal("strategy service returned a non-object response")
        if "code" in doc:
            raise from_producer_envelope(doc)
        return doc
