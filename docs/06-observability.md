# 06 — Task 6: Observability (CloudWatch Logs + X-Ray)

File: `src/agent_orchestrator.py` → `configure_observability()` · Test: `python tests/test_agent.py task6` (20 points)

## Concept

Multi-agent systems are hard to debug: one request passes through 3–5 agents, a
dozen LLM calls, DynamoDB and Knowledge Bases. Observability answers
*"what happened, where, and how long did it take?"*.

| Pillar | Service | Contents |
|---|---|---|
| **Logs** | CloudWatch Logs | Tool calls, arguments, durations, trace ids (INFO level) |
| **Traces** | AWS X-Ray | Per-request call tree: Orchestrator → Worker → Knowledge Base, with latency per node |

**Sampling rate** = share of requests that are traced. Development: **1.0 (100%)**
so every request is visible. Production is usually ~0.05 (5%) to save cost.

## Implementation

```python
logging_configuration = {
    'cloudWatchConfig': {'logGroupName': config.AGENT_LOG_GROUP,
                         'logLevel': 'INFO', 'enabled': True},
    'xRayConfig':       {'enabled': True, 'samplingRate': 1.0},
}
try:
    summary = apply_observability_config(runtime_arn, logging_configuration)
    print(...log group and sampling rate...)
except Exception as e:
    print(f"  [Note] Observability configuration failed: {e}")
```

The rubric asks for `try/except`: an observability failure must not fail the agent
deployment. The agent keeps serving customers even if tracing breaks.

The provided `apply_observability_config()` then:
1. Creates the log group if needed.
2. Enables **CloudWatch Transaction Search** (the mechanism AgentCore Observability
   uses): X-Ray segments go to CloudWatch Logs `aws/spans`, indexed at 100%.
3. Stores the settings as runtime env vars (`AGENT_LOG_LEVEL`,
   `AGENT_LOG_TO_CLOUDWATCH`, `AGENT_TRACING_ENABLED`, `AGENT_TRACE_SAMPLING_RATE`)
   and runs `agentcore deploy` a second time.

## How traces are built

`agent_observability.py` wraps the `@tool` decorator:

| Tool | Node on the map |
|---|---|
| `route_to_inventory_agent` | **InventoryAgent** |
| `route_to_policy_agent` | **PolicyAgent** |
| `route_to_refund_agent` | **RefundAgent** |
| `route_to_communication_agent` | **CommunicationAgent** |
| `retrieve_from_knowledge_base()` | **KnowledgeBase:returns / shipping / warranty** |

The retrievers run on other threads (ThreadPoolExecutor). New threads don't inherit
the trace *context*, so the tracer falls back to "adopting" KB nodes into an open
node — the graph stays connected (in the console the KB nodes appear next to the
workers under `NovaMart-Orchestrator`).

## Verification and screenshot

```powershell
python tests/test_agent.py task6
python src/agent_orchestrator.py test        # 3 scenarios → prints X-Ray trace ids
```

Wait 30–60 seconds → **CloudWatch → Application Signals (APM) → Trace Map**
(older consoles: *X-Ray traces → Service map*). Direct link:
`https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#xray:service-map/map`.

> Screenshot tip: pick a time range that covers **both the return and the policy
> scenarios**. A 5-minute window after running only policy/math shows no
> InventoryAgent or RefundAgent. Use 15m or 1h, then zoom in (+) until node names
> are readable.

Agent logs: **CloudWatch → Log groups → `/aws/bedrock/agentcore/udacity-agentcore`**.
