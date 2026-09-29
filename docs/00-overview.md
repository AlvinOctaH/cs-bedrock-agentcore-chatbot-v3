# 00 — Overview: What Was Built and Why

These notes explain the **NovaMart Multi-Agent Customer Support** project
(Udacity AWS Agentic AI — Course 3, Project 3) step by step, so it can be rebuilt
from scratch without help.

| File | Contents |
|---|---|
| [00-overview.md](00-overview.md) | Concepts, architecture, request flow (this file) |
| [01-setup.md](01-setup.md) | Tools, credentials, CloudFormation, seed data, `.env` |
| [02-multi-agent-graph.md](02-multi-agent-graph.md) | Task 2 — the 5 agents, tools, prompts, WorkflowState |
| [03-guardrail-and-runtime.md](03-guardrail-and-runtime.md) | Task 3 — Bedrock Guardrail + AgentCore Runtime deployment |
| [04-memory.md](04-memory.md) | Task 4 — AgentCore Memory |
| [05-knowledge-bases.md](05-knowledge-bases.md) | Task 5 — three Knowledge Bases in the AWS Console |
| [06-observability.md](06-observability.md) | Task 6 — CloudWatch Logs + X-Ray |
| [07-testing-and-submission.md](07-testing-and-submission.md) | 120-point test suite, E2E scenarios, screenshots, clean-up |
| [08-standout.md](08-standout.md) | Stand-out extensions |
| [09-troubleshooting.md](09-troubleshooting.md) | Every real error hit during the build, with the fix |

---

## 1. The business problem

NovaMart (a fictional e-commerce company) receives thousands of support requests
a day: order status, returns/refunds, policy questions. Human agents look up the
order, read the policy and write a reply — slow and inconsistent.

Goal: an **AI layer** that understands the request, pulls data from several sources,
decides according to policy and writes a professional reply — automatically, safely
(guardrails) and auditably (observability).

## 2. Architecture pattern: Orchestrator → Workers

Instead of one giant agent holding every tool, the work is split across
**specialists**. One "manager" agent (the Orchestrator) only decides *who* does what.

```
Customer Request
      │
OrchestratorAgent  (Claude Haiku 4.5, temp 0.0)  ── routing + WorkflowState
      │
 ┌────┼──────────────┬──────────────┬───────────────────┐
Inventory        Policy           Refund          Communication
(DynamoDB)   (multi-agent RAG)   (decision)       (final reply)
                  │
     ┌────────────┼────────────┐       ← run in PARALLEL
 Returns       Shipping      Warranty      (3 retriever sub-agents)
 KB            KB            KB

Shared state: DynamoDB WorkflowState (optimistic locking)
```

Why split it up?

- **Focused prompts** → each agent is more accurate in its own domain.
- **Least privilege** → only the RefundAgent can modify an order.
- **Right model per role** → routing uses Haiku (fast, cheap), reasoning uses
  Sonnet (stronger). A common production cost/latency optimisation.
- **Easy to test and swap** → each worker can be exercised on its own.

## 3. Agent roles

| Agent | Model / temp | Tools | Responsibility |
|---|---|---|---|
| **Orchestrator** | Haiku 4.5 / 0.0 | `initialize_session`, 4 × `route_to_*` | Routing + WorkflowState. **Never** answers the customer itself |
| **Inventory** | Sonnet 4.5 / 0.1 | `check_order_status`, `get_customer_tier`, `list_customer_orders` | Collects facts from DynamoDB. Decides nothing |
| **Policy** | Sonnet 4.5 / 0.2 (retrievers 0.0) | `search_all_policies` | RAG coordinator: 3 parallel retrievers → synthesis |
| **Refund** | Sonnet 4.5 / 0.1 | `get_inventory_context`, `initiate_refund` | Return eligibility (Standard 30 days, Premium 60 days) |
| **Communication** | Sonnet 4.5 / 0.3 | `get_full_workflow_context` | Warm, professional final reply |

**Why different temperatures?** Temperature controls how "creative" the model is.
Routing and retrieval must be deterministic (0.0), refund decisions almost
deterministic (0.1), policy synthesis slightly flexible (0.2), and the customer
reply needs natural language (0.3).

## 4. Routing rules (the heart of the Orchestrator)

| Rule | Trigger | Action |
|---|---|---|
| 1 | Every request | `initialize_session` first |
| 2 | Order status / return / refund | Inventory → Refund |
| 3 | Policy meaning questions | Policy |
| 4 | Account questions ("what is my tier?") | Inventory — **never** Policy |
| 5 | Math / calculation | Straight to Communication |
| 6 | Every request (last) | Communication writes the reply |

## 5. Shared WorkflowState + optimistic locking

Agents never call each other directly. They share **one DynamoDB record** per
session (`session_id`), and each worker writes to its own column:

```
version 0  initialize_session()       → {session_id, customer_id, version: 0}
version 1  InventoryAgent writes      → + inventory_agent
version 2  RefundAgent writes         → + refund_agent
version 3  CommunicationAgent writes  → + communication_agent
```

**Optimistic locking**: every update carries an `expected_version`. DynamoDB only
accepts the write if the stored `version` still matches (`ConditionExpression`).
If another writer got there first, the write fails → re-read → retry. This stops
two agents from silently overwriting each other without a blocking lock.

## 6. Example flow: "I want to return my order ORD-27176" (CUST-001)

1. Orchestrator → `initialize_session` → record created (v0).
2. Orchestrator → `route_to_inventory_agent` → Inventory calls `get_customer_tier`
   (Premium) + `check_order_status` (delivered 7 days ago) → written to
   `inventory_agent` (v1).
3. Orchestrator → `route_to_refund_agent` → Refund reads `get_inventory_context`,
   applies the 60-day window → eligible → `initiate_refund` sets the order to
   `return_initiated` with an RMA number → decision written to `refund_agent` (v2).
4. Orchestrator → `route_to_communication_agent` → Communication reads the whole
   state → writes the reply → `communication_agent` (v3).
5. The reply goes back to the customer. The whole journey is one X-Ray trace.

## 7. AWS services used

| Service | Purpose |
|---|---|
| **Amazon Bedrock** (Claude Haiku 4.5 & Sonnet 4.5) | The brain of every agent |
| **Strands Agents SDK** | Python framework for agents + `@tool` |
| **DynamoDB** | orders, customers, workflow-state tables |
| **S3 + S3 Vectors** | Policy documents + vector storage |
| **Bedrock Knowledge Bases** | Managed RAG (chunking, embedding, retrieval) |
| **Bedrock Guardrails** | Content, PII, denied topics, profanity, prompt attacks |
| **AgentCore Runtime** | Managed hosting of the agent as an HTTP service |
| **AgentCore Memory** | Per-session conversation summaries (7 days) |
| **CloudWatch Logs + X-Ray** | Logs and distributed tracing |
| **CloudFormation / CDK** | Infrastructure as code |
