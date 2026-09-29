# Reflection

**Design decision — deterministic code around probabilistic agents.** The rubric
gives the RefundAgent the eligibility decision, and it does make that decision from
the inventory facts. But an LLM that writes to the orders table is a liability, so
`initiate_refund` re-checks the tier window in plain Python and updates the order with a
DynamoDB condition (`status = delivered`). An ineligible or already-returned order
can never be changed, even if the model reasons incorrectly. The same idea shows
up elsewhere. Date arithmetic (`days_since_delivery`) is computed in the tool, not by
the model. The orchestrator uses a `SequentialToolExecutor`, so Refund can never run
before Inventory. Worker agents are cleared before every call, so one customer's data
cannot leak into another's request. The agents decide; the code guarantees the
invariants.

**Challenge — the guardrail passed its spec but failed a real attack.** The required
policies (content, PII, three denied topics, profanity) all validated, and the math
scenario was allowed thanks to a narrow "pricing negotiation" definition on the
STANDARD topic tier. The adversarial suite I built then showed that version 1 let
prompt-injection and system-prompt-extraction attempts straight through: 16/18. None of
the required filters look for instructions aimed at the model. Adding a `PROMPT_ATTACK`
input filter produced version 2 at 18/18 with no false positives, confirmed end to end
through the deployed runtime. A second finding came from reading replies, not test
output. The CommunicationAgent sometimes included a support email address, which the
guardrail correctly anonymized into `{EMAIL}`. Safe, but a broken experience, fixed in
the prompt rather than by weakening the guardrail.

**Production considerations.** Latency is the main cost of this architecture. The
CloudWatch dashboard shows the PolicyAgent averaging about 27 s, because each retriever
is a full LLM call on top of its KB query. Running the three retrievers in parallel
already cuts that to the slowest branch. The next step would be calling the Knowledge
Bases directly, or using Haiku for the retrievers. X-Ray sampling at 100% is right for
development but should drop to around 5% in production. Finally, the orchestrator is
built once per runtime process and holds conversation history. Persisting that history
per session in DynamoDB (the stand-out session manager) is what makes horizontal
scaling and resumable chats safe.
