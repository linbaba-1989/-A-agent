# Model Arena

Model Arena compares candidate models by role without changing the production routes in
`config/agent_config.json`. GLM, Hunyuan, MiniMax, and StepFun are registered as disabled
candidates. Their model IDs, endpoints, and credentials come from local environment variables.

All candidates receive the same versioned Fact Bundle, role prompt, JSON schema, knowledge
context, evidence policy, and evaluation rules. The first golden case is `600498.SH`; its fixed
hash prevents later market updates from silently changing the exam input.

Evaluation has three levels:

1. Level 1 runs the 20 deterministic mock scenarios and is safe for routine tests.
2. Level 2 evaluates stored historical Fact Bundles without querying live market data.
3. Level 3 calls paid provider APIs and must never run without explicit user authorization for
   that specific run.

Scores retain PASS/FAIL gates and add role-specific 0–5 dimensions. Unsupported facts, treating
unavailable data as confirmed, confidence-cap violations, and unstable schema output are hard
failures. Rankings are separate for Technical, Fundamental/Event, Sentiment, Risk, and Chief.
An Arena winner is only a recommended candidate; promotion to production requires a separate,
explicit human action.

Capabilities and pricing that have not been verified are stored as `unknown`. When both input
and output prices are absent, estimated cost is JSON `null` and the cost status is `unknown`.
Single latency observations remain raw samples; P95 is not shown until multiple samples exist.
