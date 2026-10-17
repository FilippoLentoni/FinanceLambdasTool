# Beta modeling evidence contract consumer

This release pins the exact compatible `finplan-contracts` 1.12.0 wheel published
by FinancialPlanning. Portfolio decisions now identify language model, agentic,
control, reinforcement learning and optimization families honestly. Existing tools
pass through versioned platform evidence instead of substituting family labels.

The existing user resolution request still requires explicit confirmation. The
platform-only isolated experiment receipt may identify an automatic experiment
policy; this does not grant a tool, agent or caller automatic acceptance of a user
portfolio. Tool permissions, budgets, gateway authentication and real-order
prohibitions remain unchanged.

Validation for this pin: unit/contract suite (801 tests), then contract suite (171
tests) and pin check against the final rebuilt wheel. Pipeline build and deployed
beta checks are recorded in the central beta-modeling deployment evidence.
