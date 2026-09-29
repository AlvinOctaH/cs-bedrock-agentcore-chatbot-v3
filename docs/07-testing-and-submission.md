# 07 — Testing, End-to-End Scenarios, Submission & Clean-up

## 1. Automated tests (120 points)

```powershell
cd cs-bedrock-agentcore-chatbot-v3
.\venv\Scripts\activate
$env:PYTHONUTF8=1
python tests/test_agent.py all
```

| Task | What is checked | Points |
|---|---|---|
| 2 | 5 builders return an Agent, tool counts (3 / 1 / 5), Haiku vs Sonnet, ThreadPoolExecutor | 40 |
| 3 | Guardrail exists, policies match spec, numbered READY version, runtime READY + PUBLIC/HTTP + 8 env vars | 20 |
| 4 | Memory ACTIVE, SUMMARIZATION strategy, 7-day retention | 15 |
| 5 | 3 KB IDs set, ACTIVE, Titan V2, synced, parallel retrieval returns results | 25 |
| 6 | INFO logging env vars on the runtime, log group exists, X-Ray 100% + Transaction Search | 20 |
| **Total** | | **120** |

The test suite starts at **Task 2**: Task 1 is environment setup (stack, seed data,
`.env`) and has no test of its own — nothing else passes without it.
All Task 3–6 checks **read real state back from AWS**; nothing is mocked.

Result: **120/120 (100%)**.

## 2. The three required end-to-end scenarios

| Scenario | Customer | Expected route |
|---|---|---|
| "I want to return my order ORD-27176" | CUST-001 | Orchestrator → Inventory → Refund → Communication |
| "What is the return policy for premium customers?" | CUST-002 | Orchestrator → Policy (3 parallel KBs) → Communication |
| "How much are 5 items at $29.99 with 10% off?" | CUST-003 | Orchestrator → Communication (no Inventory/Policy/Refund) |

```powershell
# Local (all three, each prints its X-Ray trace id)
python src/agent_orchestrator.py test

# Interactive chat with a colour-coded trace per agent
python src/agent_orchestrator.py chat

# Through the deployed AgentCore runtime
python src/agent_orchestrator.py invoke "I want to return my order ORD-27176" CUST-001
python src/agent_orchestrator.py invoke "What is the return policy for premium customers?" CUST-002
python src/agent_orchestrator.py invoke 'How much are 5 items at $29.99 with 10% off?' CUST-003
```

> In PowerShell a `$` inside double quotes is interpolated — use single quotes (as
> above) or escape it with a backtick.

> A return test changes ORD-27176 to `return_initiated`. Before a demo or screenshot,
> reset with `python infrastructure/seed_data.py`.

## 3. Required screenshots

### A. Test score 120/120
1. `python tests/test_agent.py all`.
2. Capture the ✓ PASS list and `Score: 120/120 pts (100%)`. If it doesn't fit on one
   screen, take several parts: `screenshots/test_all_120_1.png` … `_5.png`.

### B. X-Ray Service Map (Trace Map)
1. `python src/agent_orchestrator.py test` → wait for
   "X-Ray trace ... published successfully".
2. Wait 30–60 seconds.
3. AWS Console → **CloudWatch** → **Application Signals (APM) → Trace Map**
   (older consoles: X-Ray traces → Service map).
4. Time range 15m or 1h (must include the return scenario).
5. Check: `NovaMart-Orchestrator` → InventoryAgent, RefundAgent, PolicyAgent,
   CommunicationAgent + KnowledgeBase:returns/shipping/warranty.
6. Save as `screenshots/xray_service_map.png`.

## 4. Submission checklist

- [x] `src/agent_orchestrator.py` — all Task 2, 3, 4, 6 TODOs implemented
- [x] 3 Knowledge Bases created & synced (Task 5)
- [x] `.env` has the 3 KB IDs, `AGENTCORE_RUNTIME_ARN`, `GUARDRAIL_ID`, `GUARDRAIL_VERSION`
      (`.env` is not committed; the values are listed in the README)
- [x] Screenshot test 120/120 (`test_all_120_1..5.png`)
- [x] Screenshot X-Ray trace map (`xray_service_map.png`, all 7 nodes)

## 5. Clean-up (AFTER the project is graded)

```powershell
python infrastructure/cleanup.py            # dry run: lists what would be deleted
python infrastructure/cleanup.py --yes      # delete everything
python standout/create_dashboard.py --delete
python standout/dynamodb_session_manager.py delete-table
```

`cleanup.py` deletes the KBs (and their service roles), the runtime stack
`AgentCore-udacity-default`, the Memory, the Guardrail, the bucket contents, the main
stack and the log groups. KB and AgentCore deletions are asynchronous — run it
twice to be sure everything is gone.

> Wait until the project is graded: if the reviewer asks for changes you still need the resources.
