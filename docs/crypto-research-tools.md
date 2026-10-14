# Crypto paper research MCP tools

Contract 1.7.0 adds four read-only beta tools using the existing owned FinanceModel
classical backend: get_crypto_experiment, recommend_crypto_portfolio,
list_crypto_history (at most 24 observations), and discover_research_data.
The hosted LangGraph agent accesses them through the independent classical
AgentCore Gateway. Normal identity, producer-version, schema, response-size and
private-location checks apply. Gamma/prod requests fail closed.

All portfolios are synthetic. PPO and LLM compatibility is returned explicitly;
the equity PPO is not used as a crypto policy. Research discovery does not acquire
data, authorize a payment, trust merchant instructions or enable trading. Paid
acquisition remains the separately approved AgentCore Payments operator path.
