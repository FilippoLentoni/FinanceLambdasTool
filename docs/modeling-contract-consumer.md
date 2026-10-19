# Beta modeling evidence contract consumer

This release pins the exact compatible `finplan-contracts` 1.14.0 wheel produced
by FinancialPlanning. Portfolio decisions now identify language model, agentic,
control, reinforcement learning and optimization families honestly. Existing tools
pass through versioned platform evidence instead of substituting family labels.

The existing user resolution request still requires explicit confirmation. The
platform-only isolated experiment receipt may identify an automatic experiment
policy; this does not grant a tool, agent or caller automatic acceptance of a user
portfolio. Tool permissions, budgets, gateway authentication and real-order
prohibitions remain unchanged.

Version 1.14.0 replaces `recommend_portfolio` and `recommend_classical_portfolio` with
`generate_portfolio_recommendation_min_variance`, `_cvar` and `_ppo`: one tool per model, each
requiring an explicit `instruments` universe (see `classical-mcp-tools.md` and
`selected-strategy-tool.md`). The removed tools have no aliases. Existing snapshot-backed
decisions and user confirmation are preserved.

Validation includes the full unit/contract suite, release synth with real Lambda bundles and the
build gates against the frozen wheel. Pipeline build and deployed beta checks are recorded in the
central beta-modeling deployment evidence.
