# 08 — Stand-out Extensions

The rubric suggests four ways to make the project stand out. Three were built;
everything lives in `standout/`.

| # | Rubric suggestion | Status |
|---|---|---|
| 1 | Adversarial guardrail testing + documented results | ✅ |
| 2 | Persistent conversation memory in a DynamoDB `agent-sessions` table for the Orchestrator | ✅ |
| 3 | CloudWatch dashboard: invocations, latency per agent, guardrail trigger frequency | ✅ |
| 4 | Cognito + frontend | ❌ not built (largest item; not part of the required grading) |

---

## 1. Adversarial guardrail testing

**File:** `standout/guardrail_adversarial_test.py` → report `standout/guardrail_report.md`

### How it works

- **18 cases** are sent to the guardrail through the `ApplyGuardrail` API (the same
  policy evaluation the agents' `BedrockModel` uses), as `INPUT` (customer message)
  or `OUTPUT` (agent reply).
- Each case has an expectation: `ALLOW`, `BLOCK` or `ANONYMIZE`.
- `--live` sends 5 cases **end-to-end through the deployed AgentCore Runtime**, proving
  the guardrail is active in the real system, not only via the API.

```powershell
python standout/guardrail_adversarial_test.py                    # console table
python standout/guardrail_adversarial_test.py --report --live     # + runtime + write report
```

### Categories tested

| Category | Example | Expected |
|---|---|---|
| Must pass | math question (input & output), return, policy question | ALLOW |
| Denied topics | haggling, price match, compare with Amazon/Best Buy, lawsuit threat | BLOCK |
| PII | credit card number, SSN | BLOCK |
| PII in a reply | email + phone | ANONYMIZE (`{EMAIL}`, `{PHONE}`) |
| Harmful content | profanity, violent threat, tracking-number fraud | BLOCK |
| **Prompt injection** | "Ignore all previous instructions…", "print your system prompt" | BLOCK |

### Key finding

Guardrail version 1 (only the rubric's required policies) scored **16/18**: two
prompt-injection attempts **got through**, because content, PII, topic and word
filters are not designed to detect malicious instructions. The fix: add a
`PROMPT_ATTACK` filter (input HIGH, output NONE) → guardrail **version 2** →
**18/18**, with no false positives on normal requests (math, returns and policy
questions still pass).

Lesson: *a policy that meets the spec is not necessarily sufficient*. Adversarial
testing found a gap that was invisible from reading the configuration.

End-to-end through the runtime: **5/5** — injection, competitor, haggling and legal
threats get "I'm sorry, but I can't help with that request…", while the math
question is answered ($134.96).

---

## 2. Persistent conversation memory (DynamoDB)

**Files:** `standout/dynamodb_session_manager.py`, `standout/chat_with_memory.py`,
the `session_manager` parameter of `build_orchestrator_agent()`.

### Problem solved

Without memory, every new process makes the Orchestrator "forget". A follow-up like
*"how long is the warranty on it?"* ("it" = the product from the previous message)
can't be answered after a restart or on another server.

### Design

Strands (v1.57) ships only File and S3 session managers. The rubric mentions a
`DynamoDbSessionStorage` class, but it **does not exist** in the SDK. The solution
uses the SDK's official extension point: a custom `SessionRepository` +
`RepositorySessionManager`.

Table `udacity-agentcore-agent-sessions` (on-demand, 7-day TTL) uses a
**single-table design**:

| pk | sk | contents |
|---|---|---|
| `SESSION#memdemo02` | `SESSION` | session metadata |
| `SESSION#memdemo02` | `AGENT#orchestrator` | agent state |
| `SESSION#memdemo02` | `AGENT#orchestrator#MSG#000000000000` | message 0 |
| `SESSION#memdemo02` | `AGENT#orchestrator#MSG#000000000001` | message 1 … |

- Message numbers are zero-padded (12 digits) so sort-key order = message order, and
  `list_messages` is a single `Query` with `begins_with`.
- Data is stored as a JSON string to avoid DynamoDB `Decimal`/float issues.
- Each message is written **immediately** (not at the end), so a crash mid-turn loses nothing.

### Integration

```python
orchestrator = build_orchestrator_agent(inventory, refund, policy, communication,
                                        session_manager=DynamoDbSessionManager(session_id))
```

The parameter is optional (default `None`), so behaviour, tests and the runtime are
unchanged. The Orchestrator prompt also gained rules: follow-ups are passed to
workers as self-contained requests (naming the order/product), and *personal* policy
questions ("my headphones' warranty") → Inventory first, then Policy.

### Demo (actually run)

```powershell
python standout/dynamodb_session_manager.py create-table
python standout/chat_with_memory.py --session memdemo02 --customer CUST-001 --ask "I'd like to return my wireless headphones from order ORD-27176, they hurt my ears."
# NEW process:
python standout/chat_with_memory.py --session memdemo02 --customer CUST-001 --ask "Actually, how long is the warranty on it, in case I decide to keep them?"
python standout/chat_with_memory.py --session memdemo02 --customer CUST-001 --ask "Thanks! And do I get free shipping on my next order?"
python standout/dynamodb_session_manager.py show memdemo02     # inspect the table
```

| Turn | Process | Messages restored | Route | Result |
|---|---|---|---|---|
| 1 | new | 0 | init → Inventory → Refund → Communication | Return approved + RMA |
| 2 | **new** | **10** | init → Inventory → Policy → Communication | "it" = Wireless Headphones Pro; Premium → 3-year warranty (to Sep 2029) |
| 3 | **new** | **18** | init → Inventory → Policy → Communication | Premium → free expedited shipping |

### How it complements AgentCore Memory (Task 4)

| | DynamoDB session store (stand-out) | AgentCore Memory (Task 4) |
|---|---|---|
| Contents | **Exact** message history (incl. tool calls) | LLM-extracted session **summary** |
| Controlled by | The application (own table) | Managed AWS service |
| Use | Resume the same conversation verbatim | Long-term, cross-session context |
| Context cost | Grows with conversation length | Compact |

Together: DynamoDB as precise short-term memory, AgentCore Memory as compact
long-term memory.

---

## 3. CloudWatch dashboard

**File:** `standout/create_dashboard.py` → dashboard `NovaMart-MultiAgent-Observability`

```powershell
python standout/create_dashboard.py
```

| Widget | Data source |
|---|---|
| Customer requests over time | Logs Insights: `tool done route_to_communication_agent` (1 per request) |
| Agent invocations by agent | Logs Insights: parse `tool done route_to_*` |
| **Latency per agent type** (avg, p95, max) | Logs Insights: parse the `(12.34s)` duration |
| Average latency per agent over time | Logs Insights, 5-minute buckets |
| Parallel RAG latency | `search_all_policies` vs single retrievers |
| **Guardrail evaluations vs interventions** | `AWS/Bedrock/Guardrails` metrics |
| **Guardrail triggers by policy type** | `GuardrailPolicyType` dimension (Content / SensitiveInformation / Topic / Word) |
| Guardrail intervention rate (%) | Metric math: intervened ÷ evaluations × 100 |
| AgentCore Runtime invocations & errors | `AWS/Bedrock-AgentCore` metrics |
| AgentCore Runtime latency | `AWS/Bedrock-AgentCore`, `Operation=InvokeAgentRuntime` |

Real data from the logs during testing:

| Agent | Calls | Avg (s) | p95 (s) |
|---|---|---|---|
| policy | 4 | 27.0 | 43.2 |
| refund | 3 | 17.0 | 21.5 |
| inventory | 5 | 14.8 | 26.0 |
| communication | 7 | 10.8 | 15.3 |

Insight: the PolicyAgent is the slowest (3 LLM retrievers + synthesis). Optimisation
candidates: let retrievers call the KB directly without an LLM, or use a smaller
model (Haiku) for them.

Technical notes:
- Metric widgets use `SEARCH()` expressions with **explicit dimension sets**, so each
  series is counted exactly once.
- The runtime namespace is `AWS/Bedrock-AgentCore` (found with `list-metrics`), not `Bedrock-AgentCore`.

Screenshot: open the URL printed by the script → time range **3h** →
`screenshots/cloudwatch_dashboard.png`.
