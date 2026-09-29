# 04 — Task 4: AgentCore Memory

File: `src/agent_orchestrator.py` → `configure_memory()` · Test: `python tests/test_agent.py task4` (15 points)

## Concept

**AgentCore Memory** is managed conversation storage with two layers:

| Layer | Contents | Retention |
|---|---|---|
| **Short-term (events)** | Every raw message (user/assistant) as an *event* | `eventExpiryDuration` (here **7 days**) |
| **Long-term (records)** | What a *memory strategy* extracts from the events | Until deleted |

The memory strategy decides what is extracted:

| Strategy | Output |
|---|---|
| `semanticMemoryStrategy` | Facts ("the customer has a dog") |
| `userPreferenceMemoryStrategy` | Preferences ("likes short answers") |
| **`summaryMemoryStrategy`** ← used | **One summary per session** |

Why SESSION_SUMMARY? For customer support the most useful context is the running
session ("I already gave you my order number"), so customers don't have to repeat
themselves between turns.

## Implementation

```python
response = agentcore_control.create_memory(
    name=memory_name,                                   # config.MEMORY_NAME = udacity_agentcore_memory
    description='NovaMart customer support conversation memory: ...',
    eventExpiryDuration=7,                              # days
    memoryStrategies=[{
        'summaryMemoryStrategy': {
            'name': 'SessionSummary',
            'description': 'Summarizes each customer support session',
            'namespaces': ['/summaries/{actorId}/{sessionId}'],
        }
    }],
    clientToken=str(uuid.uuid4()),                      # idempotency
)
```

- The **name** uses underscores (`udacity_agentcore_memory`) because AgentCore
  resource names cannot contain hyphens.
- The **namespace** `/summaries/{actorId}/{sessionId}` stores summaries per customer
  (`actorId`) per session. `{...}` are placeholders AgentCore fills in.
- **`clientToken`** → if the request is sent twice (network retry), AWS does not
  create a duplicate memory.
- The provided code waits for `ACTIVE` and returns `memoryArn`, and first checks
  whether a memory with that name already exists, so `deploy` is safe to re-run.

## Verification

```powershell
python tests/test_agent.py task4
aws bedrock-agentcore-control list-memories --query "memories[].[id,status]"
```

The test checks: a memory prefixed `udacity_agentcore_memory` exists, is `ACTIVE`,
has a `SUMMARIZATION` strategy and `eventExpiryDuration == 7`.

## How it relates to WorkflowState and the local session store

| Mechanism | Scope | Role |
|---|---|---|
| **WorkflowState** (DynamoDB) | One request/turn | Shared whiteboard between agents |
| **AgentCore Memory** | Session & cross-session, 7 days | Long-term conversation summaries in the cloud |
| **Local session store** (stand-out, see 08) | One chat session | Exact Orchestrator message history for multi-turn chats |
