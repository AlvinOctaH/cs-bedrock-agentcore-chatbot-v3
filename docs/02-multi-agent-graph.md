# 02 — Task 2: The Multi-Agent Graph (5 Agents)

File: `src/agent_orchestrator.py` · Test: `python tests/test_agent.py task2` (40 points)

## 1. Strands Agents basics

A Strands agent has three ingredients: **model**, **system prompt**, **tools**.

```python
from strands import Agent
from strands.models import BedrockModel
from agent_observability import tool        # = strands @tool + X-Ray subsegment

@tool
def get_customer_tier(customer_id: str) -> dict:
    """Retrieve a customer's tier ... (the docstring is the LLM's manual)"""
    ...

model = BedrockModel(model_id=config.WORKER_MODEL_ID,
                     region_name=config.AWS_REGION, temperature=0.1)
agent = Agent(name='InventoryAgent', model=model,
              system_prompt="You are ...", tools=[get_customer_tier])
result = agent("What tier is CUST-001?")    # call it like a function
```

Key points:
- **The tool docstring is the contract with the LLM.** The model decides when to
  call a tool purely from its name, parameters and docstring. A clear docstring
  (purpose, `Args`, `Returns`) means correct tool calls — and the rubric grades it.
- `@tool` is imported from `agent_observability` (not `strands`) so every tool call
  is recorded in X-Ray automatically.
- Models are **never hard-coded**: always `config.WORKER_MODEL_ID` /
  `config.ORCHESTRATOR_MODEL_ID` (rubric).
- Tools are defined **inside** each `build_*_agent()` → builders are self-contained
  and return exactly one `Agent`.

## 2. Shared helpers added

| Helper | Purpose | Why |
|---|---|---|
| `RETURN_WINDOW_DAYS = {'Standard': 30, 'Premium': 60}` | Single source of truth for return windows | Used by the Refund prompt **and** the check in `initiate_refund` |
| `_json_safe()` | DynamoDB `Decimal` → int/float | boto3 returns numbers as `Decimal`, which is not JSON-serialisable |
| `_days_since()` | Days elapsed since an ISO date | LLMs miscount dates → compute in code, the LLM just reads the number |
| `_evaluate_return_eligibility()` | Deterministic eligibility rule | Backstop: an ineligible order is **never** processed even if the LLM is wrong |
| `_run_worker()` | Clear a worker's history, then call it | Agents are built once and reused; without this, customer A's data could leak into customer B's request |

## 3. 2.A — InventoryAgent (fact gatherer)

Model: Sonnet 4.5, temperature **0.1**.

| Tool | DynamoDB operation | Notes |
|---|---|---|
| `check_order_status(customer_id, order_id)` | `get_item` on orders | Orders use a **composite key** (`customer_id` partition + `order_id` sort) → `get_item` needs both. Also adds `days_since_order` and `days_since_delivery` |
| `get_customer_tier(customer_id)` | `get_item` on customers | Profile + tier |
| `list_customer_orders(customer_id)` | `query` with `Key('customer_id').eq(...)` | For requests without an order number ("my headphones") |

The system prompt stresses: **report facts only, never decide return eligibility**
— that decision belongs to the RefundAgent. Output uses a fixed format
(`CUSTOMER / ORDER / DATES / TRACKING / NOTES`) that the next agent can read easily.

## 4. 2.B — RefundAgent (decision maker)

Model: Sonnet 4.5, temperature **0.1**.

| Tool | Purpose |
|---|---|
| `get_inventory_context(session_id)` | Read the `inventory_agent` column from WorkflowState |
| `initiate_refund(customer_id, order_id, reason)` | Set the order to `return_initiated` and create an `RMA-XXXXXXXX` reference |

Decision process in the prompt: (1) always read the inventory context first →
(2) take tier, status and days since delivery → (3) apply the 30/60-day window →
(4) if eligible **and** the customer asked for a return → `initiate_refund` →
(5) otherwise never call it. Output: `DECISION: APPROVED | DENIED | NO_ACTION | NEEDS_INFO`.

Safety inside `initiate_refund` (production practice):
- Re-check eligibility deterministically before writing.
- `ConditionExpression='#status = :delivered'` → atomic update only while the order
  is still `delivered` (prevents double returns). `status` is a DynamoDB **reserved
  word**, hence `ExpressionAttributeNames`.
- Idempotent: a second request for the same order returns the existing RMA number.

The window is counted from the **delivery date** (policy: *"within 30 days of delivery"*),
using `estimated_delivery` in the seed data.

## 5. 2.C — PolicyAgent (parallel multi-agent RAG) ⭐

Inside `build_policy_agent()` three **retriever sub-agents** are created:

| Sub-agent | Tool | Knowledge Base |
|---|---|---|
| `ReturnsPolicyRetrieverAgent` | `retrieve_returns_policy` | `config.RETURNS_KB_ID` |
| `ShippingPolicyRetrieverAgent` | `retrieve_shipping_policy` | `config.SHIPPING_KB_ID` |
| `WarrantyPolicyRetrieverAgent` | `retrieve_warranty_policy` | `config.WARRANTY_KB_ID` |

Retrievers use temperature **0.0** (deterministic) and `callback_handler=None`
(three agents stream at the same time; their output would interleave in the terminal).

The coordinator tool `search_all_policies(query)` runs all three **in parallel**:

```python
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = {executor.submit(_run_retriever, domain, agent, query): domain
               for domain, agent in retrievers.items()}
    for future in as_completed(futures):       # collect in completion order
        domain, text = future.result()
        results[domain] = text
```

Why parallel? Each retriever = 1 LLM call + 1 KB call (~5–10 s). Sequential = the sum;
parallel = only the slowest one. The work is **I/O-bound** (waiting on the network),
so Python threads are effective despite the GIL.

One failing retriever does not break the others (`try/except` per retriever).
Results are combined in a fixed order (Returns → Shipping → Warranty).

The coordinator (temperature **0.2**) must: always call `search_all_policies` first,
answer **only** from retrieved passages, name the source policy, merge duplicates
(the tier document exists in all three KBs), and when rules conflict (e.g. 2-year
electronics warranty vs 1-year Standard tier) explain which rule is more specific.

## 6. 2.D — CommunicationAgent (reply writer)

Model: Sonnet 4.5, temperature **0.3** (more natural language).
Tool: `get_full_workflow_context(session_id)` → the whole WorkflowState.

Prompt rules:
- Use **all** relevant findings (decision, reason, RMA number, next steps).
- Denied return: explain with empathy + offer an alternative (warranty claim).
- Math: calculate step by step and **round to cents only at the end** (round half up):
  5 × $29.99 = $149.95 → discount $14.995 → $134.955 → **$134.96**.
- Never mention internal details (agent names, session IDs, DynamoDB).
- Never write emails/phone numbers — the guardrail would mask them as `{EMAIL}`.
- Plain text, no markdown.

## 7. 2.E — OrchestratorAgent (router)

Model: **Haiku 4.5**, temperature **0.0** (routing must be deterministic).

Every routing tool follows the same pattern (function `_invoke_and_record`):

```
1. state = _read_workflow_state(session_id)          ← read first
2. result = worker(prompt)                           ← call the worker
3. _update_workflow_state(session_id,
       {'<column>': result},
       expected_version=state['version'])            ← write with optimistic locking
```

| Tool | Column written |
|---|---|
| `initialize_session` | creates the record (version 0) |
| `route_to_inventory_agent` | `inventory_agent` |
| `route_to_policy_agent` | `policy_agent` |
| `route_to_refund_agent` | `refund_agent` |
| `route_to_communication_agent` | `communication_agent` |

The system prompt contains the **6 routing rules** plus an example tool sequence
per request type, and the CRITICAL rule: the Orchestrator never writes the reply
itself; `route_to_communication_agent` is always the last call and its text is
passed through verbatim.

Production details added:
- `tool_executor=SequentialToolExecutor()` — Strands may run several tool calls
  **concurrently** by default. Routing must be sequential (Refund needs Inventory's
  result), so execution is forced to be sequential.
- **Multi-turn**: `initialize_session` on an existing session doesn't fail; it starts
  a new "turn": agent columns are cleared and the previous reply is kept in `previous_response`.
- If the model forgets `initialize_session`, routing tools create the state
  themselves (defensive). Rule 1 was also made explicit about follow-up messages
  because Haiku skipped it on a second turn during memory testing.
- **Personal policy questions** ("how long is the warranty on *my* headphones?") →
  Inventory first (tier & order), then Policy, so the answer is specific to the
  customer. General questions ("return policy for premium customers") stay Policy only.
- **Follow-up messages** that refer to earlier turns ("it", "that order") are passed
  to workers as self-contained requests naming the order/product.
- Optional `session_manager` parameter → the Orchestrator history can be persisted
  in DynamoDB (see [08-standout.md](08-standout.md)).

## 8. How to test

```powershell
$env:PYTHONUTF8=1
python tests/test_agent.py task2          # 40/40 — structure (tools, models, ThreadPool)
python src/demo.py                        # one end-to-end refund scenario with trace
python src/agent_orchestrator.py chat     # interactive chat, pick CUST-001..004
```

Local results:

| Request | Route | Result |
|---|---|---|
| Return ORD-27176 (CUST-001, Premium, 7 days) | Inventory → Refund → Communication | APPROVED + RMA |
| Return ORD-28001 (CUST-002, Standard, 40 days) | Inventory → Refund → Communication | DENIED, warranty claim offered |
| "Am I a premium member?" (CUST-002) | Inventory → Communication | Standard (not via Policy) |
| 5 × $29.99 with 10% off | Communication only | $134.96 |
