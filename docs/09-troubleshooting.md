# 09 — Troubleshooting (every problem actually hit)

Every row below **actually happened** while building this project, with its cause and fix.

## Environment & credentials

| Symptom | Cause | Fix |
|---|---|---|
| Starter files differ from the instructions (Claude 3 vs 4.5; `agentcore_cli.py`, `agent_observability.py`, `cleanup.py` missing) | The downloaded `.tar.gz` was an old version | Take the latest starter from `github.com/udacity/cd14764-aws-agentic-c3-classroom` → `project/starter` |
| `ExpiredToken` | Udacity temporary credentials expired | Start a new lab session → reconfigure the 3 credential values |
| `AccessDenied ... explicit deny ... voc-cancel-cred` | Lab session closed; old credentials revoked | Same as above. Existing resources are kept |
| `SignatureDoesNotMatch: Signature expired` with fresh credentials | **Windows clock 7 hours behind** (not synced). AWS rejects requests skewed by > 15 minutes | Settings → Time & language → Date & time → *Set time automatically* → **Sync now** |
| `UnicodeEncodeError: 'charmap' codec can't encode character '✓'` | Windows console uses cp1252 and can't print ✓ | `$env:PYTHONUTF8=1` before running Python |
| `git clone` of the Udacity repo fails: *Filename too long* | Windows 260-character path limit | Extract only the subfolder (`git archive HEAD project \| tar -x`) or `git config core.longpaths true` |

## AgentCore deployment

| Symptom | Cause | Fix |
|---|---|---|
| `python src/agent_orchestrator.py deploy` sits silent after the header | Run as a background process with stdin open; `agentcore --version` (Node) waits for input | Run from a normal terminal, or close stdin (`< /dev/null`) |
| First deploy is very slow (~15 minutes) | CDK bootstrap (`CDKToolkit`) + `uv` downloading arm64 wheels | Normal. Later deploys take ~3–5 minutes |
| `CDK deploy failed: Failed to publish asset ... Template` | Transient network error while uploading to the CDK asset bucket (happened together with a Bedrock `ProtocolError`) | Re-run the deploy; nothing changes on a failed deploy, the running runtime keeps working |
| `NodeVersionSupportWarning ... require node >=22` | The AWS JS SDK drops Node 20 in 2027 | Safe to ignore for now; upgrade to Node 22 when possible |
| Runtime has no KB IDs | Deployed before Task 5 | Create the KBs, fill `.env`, `deploy` again |

## Agents & guardrail

| Symptom | Cause | Fix |
|---|---|---|
| Prompt injection ("Ignore all previous instructions…") passes the guardrail | The rubric's required policies don't cover prompt attacks | Add a `PROMPT_ATTACK` filter (input HIGH, output NONE) → guardrail v2 |
| Customer reply contains `{EMAIL}@...` | The CommunicationAgent wrote the support email; the guardrail (email = ANONYMIZE) masked it | Prompt: never write emails/phones, invite the customer to reply instead |
| Math reply uses `**bold**` | Models default to markdown | Stricter prompt: plain text, no `**` / `#` |
| Math question could be blocked as "pricing negotiation" | Topic definition too broad (observed on the Classic tier) | STANDARD topic tier + narrow definition ("haggling / changing an advertised price… calculating a total is not negotiation") |
| Follow-up turn skipped `initialize_session` | Haiku assumed the session was already initialised | Rule 1 now says explicitly that it applies to follow-ups too |
| "How long is the warranty on it?" answered without the customer's tier | Only the PolicyAgent was called; it has no customer data | Personal policy questions route Inventory → Policy |
| (Potential) data leaking between customers | Worker agents are built once and keep message history | `_run_worker()` clears `agent.messages` before each call |
| (Potential) Refund running before Inventory finishes | Strands can execute tools concurrently by default | `tool_executor=SequentialToolExecutor()` on the Orchestrator |
| `initialize_session` fails on the second message in a session | `_create_workflow_state` uses `attribute_not_exists` | Catch `ConditionalCheckFailedException` → start a new turn (clear agent columns) |
| `The provided key element does not match the schema` | Orders table uses a composite key | `get_item(Key={'customer_id': ..., 'order_id': ...})` |
| `ValidationException ... reserved keyword: status` | `status` is a DynamoDB reserved word | `ExpressionAttributeNames={'#status': 'status'}` |
| `TypeError: Object of type Decimal is not JSON serializable` | boto3 returns numbers as `Decimal` | `_json_safe()` helper |

## Knowledge Bases & console

| Symptom | Fix |
|---|---|
| Structured or unstructured data? | **Unstructured** (text documents) |
| Vector index not in the dropdown | Region must be us-east-1; stack must be `CREATE_COMPLETE` |
| `retrieve()` returns nothing | Click **Sync**; make sure the IDs in `.env` aren't swapped |
| Can't find "X-Ray traces → Service map" | In the new CloudWatch console it is **Application Signals (APM) → Trace Map** |
| Trace map missing InventoryAgent/RefundAgent | Time range too short (only policy/math traces) — widen to 15m/1h |
