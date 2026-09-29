# 03 — Task 3: Bedrock Guardrail + AgentCore Runtime Deployment

File: `src/agent_orchestrator.py` → `create_guardrail()`, `_guardrail_policies()`,
`deploy_to_agentcore_runtime()` · Test: `python tests/test_agent.py task3` (20 points)

---

## Part A — Bedrock Guardrail

### Concept

A guardrail is a **safety filter** Bedrock evaluates on every model call, on the
**input** (customer message) and the **output** (model answer). A violation replaces
the message with a friendly "blocked" message.

Here the guardrail is not attached to the runtime but to **every agent's
`BedrockModel`**. The provided `_apply_guardrail()` calls
`model.update_config(guardrail_id=..., guardrail_version=...)` whenever
`GUARDRAIL_ID` and `GUARDRAIL_VERSION` are set (from `.env` locally, from runtime
environment variables when deployed).

### Policies

| Policy | Configuration | Reason |
|---|---|---|
| **Content filter** | SEXUAL, VIOLENCE, HATE = **HIGH**; INSULTS, MISCONDUCT = **MEDIUM** (input & output) | Block harmful content; insults/misconduct slightly looser so an annoyed customer isn't instantly blocked |
| **PII** | Credit card & SSN = **BLOCK**; email & phone = **ANONYMIZE** | Payment/identity data must never be processed; contact details are masked (`{EMAIL}`, `{PHONE}`) |
| **Denied topics** | Competitor Products, Pricing Negotiations, Legal Threats | Keep the agent on topic and avoid business/legal commitments |
| **Word policy** | Managed **PROFANITY** list | Swear words |
| **Prompt attack** ⭐ (extra) | PROMPT_ATTACK = HIGH (input only) | Adversarial testing showed injection got through without it (see 08-standout) |

### Detail: STANDARD topic tier + a narrow "pricing negotiation" definition

```python
topicPolicyConfig={'topicsConfig': topics_config,
                   'tierConfig': {'tierName': 'STANDARD'}},
crossRegionConfig={'guardrailProfileIdentifier': 'us.guardrail.v1:0'},
```

- The **STANDARD tier** uses a more accurate topic-detection model than *Classic*.
  It **requires** a *cross-region inference profile* (`us.guardrail.v1:0`), so
  evaluation can be served from several US regions.
- Topic definitions need care. Defined too broadly ("anything about prices or
  discounts"), "pricing negotiations" also blocks *"How much are 5 items at $29.99
  with 10% off?"* (false positive). The definition is therefore narrow:
  > *Haggling or asking NovaMart to lower, match or change an advertised price or to
  > grant an unoffered discount. Calculating a total from a stated price and discount
  > is not negotiation.*

Each topic also gets 3–4 **example sentences** (`examples`) for better classification.

### Why a numbered version and not DRAFT?

`create_guardrail()` produces a mutable **DRAFT**. `create_guardrail_version()`
freezes it into version **1, 2, …** (immutable). Production must point at a numbered
version so later DRAFT edits cannot silently change a running agent. The code also
waits for status `READY`.

### Refactor: `_guardrail_policies()`

All policies live in one helper that returns a `dict` of arguments, used by:
- `create_guardrail()` → `bedrock.create_guardrail(name=..., **_guardrail_policies())`
- updating the existing guardrail → `bedrock.update_guardrail(..., **_guardrail_policies())`
  followed by `create_guardrail_version()` → version 2.

| Version | Contents |
|---|---|
| 1 | All required policies |
| **2** (in use) | + PROMPT_ATTACK filter after adversarial testing |

### Validation

```powershell
python standout/guardrail_adversarial_test.py --report   # 18 cases, target 18/18
```

---

## Part B — Deploying to AgentCore Runtime

### Concept

**AgentCore Runtime** is a managed service that runs agent code as an HTTP service
(serverless, auto-scaling, per-session isolation). The contract is simple:
`POST /invocations` and `GET /ping` on port 8080 — provided by `BedrockAgentCoreApp`
in `run_serve()`.

AgentCore does **not** store Python objects. **Code** is uploaded; the runtime starts
`agent_orchestrator.py` (`serve` mode) and rebuilds the 5-agent graph on the first request.

### Deployment flow (AgentCore CLI)

```
stage_runtime_code()   → copy src/*.py + config.py to build/runtime/ + pyproject.toml
configure_runtime()    → write settings to agentcore/agentcore.json
deploy()               → `agentcore deploy -y`:
                           uv downloads arm64 / Python 3.12 wheels
                           zip code + dependencies ("direct code deployment")
                           CDK → CloudFormation stack "AgentCore-udacity-default"
deployed_runtime_arn() → read the ARN from agentcore/.cli/deployed-state.json
```

### Implementation

```python
runtime_env = {
    'AWS_REGION': ..., 'PROJECT_NAME': ...,
    'RETURNS_KB_ID': ..., 'SHIPPING_KB_ID': ..., 'WARRANTY_KB_ID': ...,
    'AGENT_LOG_GROUP': ...,
    'GUARDRAIL_ID': guardrail_id, 'GUARDRAIL_VERSION': guardrail_version,
}
agentcore_cli.configure_runtime(env_vars=runtime_env, network_mode='PUBLIC',
                                protocol='HTTP',
                                execution_role_arn=config.AGENTCORE_ROLE_ARN)
agentcore_cli.deploy()
runtime_arn = agentcore_cli.deployed_runtime_arn()
```

| Setting | Value | Meaning |
|---|---|---|
| `network_mode` | `PUBLIC` | Runtime reaches public AWS endpoints (no VPC) |
| `protocol` | `HTTP` | Plain `/invocations` contract (alternatives: MCP, A2A) |
| `execution_role_arn` | role from the stack | IAM role assumed by the runtime → Bedrock, DynamoDB, KB, X-Ray permissions |
| env vars | 8 variables | No `.env` inside the runtime; config and guardrail read these instead |

The code also warns about empty variables (e.g. KB IDs not created yet), because
`configure_runtime` skips empty values.

### Running it

```powershell
python src/agent_orchestrator.py deploy
```

6-step pipeline: build graph → guardrail → **deploy runtime** → memory →
observability (second deploy for logging/tracing env vars) → gateway (optional, skipped).
The first deploy takes ~5–15 minutes (CDK bootstrap). Afterwards copy the printed
values into `.env`:

```
AGENTCORE_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/udacity_agentcore_runtime-XXXX
GUARDRAIL_ID=...
GUARDRAIL_VERSION=2
```

Check:

```powershell
python tests/test_agent.py task3
python src/agent_orchestrator.py invoke "What is the return policy for premium customers?" CUST-002
agentcore status        # runtime details
agentcore logs          # runtime logs
```

Redeploying only code changes (no new env vars) is faster:

```powershell
cd src
python -c "import agentcore_cli; agentcore_cli.stage_runtime_code(); agentcore_cli.deploy()"
```

### Order matters

Task 5 (Knowledge Bases) **must be done before deploying**, because the Task 3 test
checks that the runtime has `RETURNS_KB_ID` etc. If you already deployed without KBs,
run `deploy` again — editing `.env` alone does **not** change a running runtime.
