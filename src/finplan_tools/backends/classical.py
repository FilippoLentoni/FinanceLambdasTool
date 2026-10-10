"""Bounded invocation of this environment's independent traditional-optimization service."""
from __future__ import annotations

import json
import re

from ..core.errors import ToolError, from_producer_envelope

MAX_RESPONSE_BYTES = 65536


class ClassicalLambdaClient:
    def __init__(self, client, references, *, environment, region, account):
        self.client, self.references = client, references
        self.environment, self.region, self.account = environment, region, account

    def call(self, operation, body, meta):
        allowed = {"explain_portfolio_decision", "compare_portfolio_decisions", "evaluate_portfolio_decision", "recommend_classical_portfolio", "explain_classical_recommendation", "compare_classical_plans", "evaluate_classical_performance", "get_classical_analysis", "list_classical_analyses", "research_portfolio_models", "research_market_events", "run_portfolio_research", "run_recursive_improvement", "submit_portfolio_feedback"}
        if operation not in allowed:
            raise ToolError.validation("unknown classical operation")
        ref = self.references.get("financemodel", "api", "classical-function-ref")
        pattern = rf"arn:aws:lambda:{re.escape(self.region)}:{re.escape(self.account)}:function:finplan-{re.escape(self.environment)}-financemodel-job-api-handler-classical"
        if not ref or not re.fullmatch(r"[0-9]{12}", self.account) or not re.fullmatch(pattern, ref):
            raise ToolError.dependency_unavailable("no traditional optimization service is released in this environment")
        payload = {"environment": self.environment, "operation": operation, "request": dict(body), "headers": meta.headers()}
        try:
            response = self.client.invoke(FunctionName=ref, InvocationType="RequestResponse", Payload=json.dumps(payload, allow_nan=False).encode())
        except Exception as exc:  # noqa: BLE001 - normalize SDK transport failures
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code == "AccessDeniedException":
                raise ToolError.forbidden("traditional optimization service invocation was denied") from None
            raise ToolError.dependency_unavailable("traditional optimization service could not be reached") from None
        if response.get("FunctionError") or response.get("StatusCode") != 200:
            raise ToolError.dependency_unavailable("traditional optimization service did not complete successfully")
        stream = response.get("Payload")
        if stream is None or not hasattr(stream, "read"):
            raise ToolError.internal("traditional optimization service returned no response payload")
        raw = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ToolError.internal("traditional optimization response exceeds the tool response bound")
        try:
            doc = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, TypeError):
            raise ToolError.internal("traditional optimization service returned malformed JSON") from None
        if not isinstance(doc, dict):
            raise ToolError.internal("traditional optimization service returned a non-object response")
        if "code" in doc:
            raise from_producer_envelope(doc)
        return doc
