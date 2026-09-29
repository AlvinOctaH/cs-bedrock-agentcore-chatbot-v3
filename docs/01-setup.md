# 01 — Setup: Tools, Credentials, Infrastructure

Goal: prepare every base AWS resource and the local environment before writing code.

## 1. Local prerequisites

| Tool | Version | Check |
|---|---|---|
| Python | **3.12** (matches the AgentCore runtime) | `py -3.12 --version` |
| AWS CLI | v2 | `aws --version` |
| Node.js | 20+ (used by the AgentCore CLI) | `node --version` |
| uv | latest (used by `agentcore deploy` to fetch arm64 wheels) | `uv --version` |
| AgentCore CLI | **0.30.0** | `agentcore --version` |

```bash
npm install -g @aws/agentcore@0.30.0
agentcore --version
```

> If the older Python `bedrock-agentcore-starter-toolkit` is installed, remove it
> (`pip uninstall bedrock-agentcore-starter-toolkit`) — both install a command named `agentcore`.

## 2. Get the latest starter

The official starter lives in `github.com/udacity/cd14764-aws-agentic-c3-classroom`
under `project/starter`. An older downloaded archive was missing `agentcore_cli.py`,
`agent_observability.py` and `cleanup.py` and still referenced Claude 3 — always
start from the repo.

On Windows a full clone fails with *Filename too long*; extract only the folder:

```bash
git clone --depth 1 --no-checkout https://github.com/udacity/cd14764-aws-agentic-c3-classroom.git classroom
cd classroom && git archive HEAD project | tar -x -C ../starter
```

## 3. AWS credentials (Udacity Cloud Lab)

Udacity accounts use **temporary credentials** (access key + secret + session token)
that expire after a few hours.

1. Udacity → **Cloud Resources / Launch AWS Gateway** → copy the three values.
2. Configure them:
   ```bash
   aws configure set aws_access_key_id     <ACCESS_KEY>
   aws configure set aws_secret_access_key <SECRET_KEY>
   aws configure set aws_session_token     <SESSION_TOKEN>
   aws configure set region us-east-1
   aws sts get-caller-identity          # must print your Account ID
   ```
3. Never commit credentials (`.env` and `~/.aws` stay out of Git).

Expired credentials show up as `ExpiredToken` or `explicit deny ... voc-cancel-cred`
→ start a new lab session and repeat. Resources already created are kept.

## 4. Deploy the CloudFormation stack (foundation)

`infrastructure/starter_stack.yaml` creates:

| Resource | Name |
|---|---|
| DynamoDB | `udacity-agentcore-orders` (key `customer_id` + `order_id`), `-customers`, `-workflow-state` |
| S3 bucket | `udacity-agentcore-policy-docs-<account>-<suffix>` (policy documents) |
| S3 Vectors | vector bucket + 3 indexes (`returns/shipping/warranty-policy-index`, 1024 dims, cosine) |
| IAM role | `udacity-agentcore-agentcore-role` (runtime execution role) |
| CloudWatch | log group `/aws/bedrock/agentcore/udacity-agentcore` |

Via the CLI (or Console → CloudFormation → Create stack → upload template, stack name
`udacity-agentcore`, tick *acknowledge IAM*):

```bash
aws cloudformation deploy \
  --template-file infrastructure/starter_stack.yaml \
  --stack-name udacity-agentcore \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

aws cloudformation describe-stacks --stack-name udacity-agentcore \
  --query "Stacks[0].StackStatus"          # wait for CREATE_COMPLETE
```

> `CAPABILITY_NAMED_IAM` is required because the template creates an IAM role with a fixed name.

## 5. Python virtual environment

```powershell
py -3.12 -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt bedrock-agentcore
```

`bedrock-agentcore` is needed for `serve` mode (the HTTP entry point inside the runtime).

## 6. Seed data

```powershell
$env:PYTHONUTF8=1            # REQUIRED on Windows, see troubleshooting
python infrastructure/seed_data.py
```

Result:
- 4 customers: CUST-001 Alice (Premium), CUST-002 Bob (Standard), CUST-003 Carol (Premium), CUST-004 David (Standard)
- 11 orders with dates relative to today (e.g. ORD-27176 = 12 days ago)
- 6 policy documents in `s3://<policy bucket>/policies/{returns,shipping,warranty}/`

Seeding is **idempotent** — safe to re-run, and useful to reset order status
(a return test changes ORD-27176 to `return_initiated`).

## 7. The `.env` file

```powershell
copy .env.example .env
python config.py
```

`config.py` reads resource names from **CloudFormation exports** automatically.
`.env` only holds values that exist after a given task:

| Variable | Filled after |
|---|---|
| `RETURNS_KB_ID`, `SHIPPING_KB_ID`, `WARRANTY_KB_ID` | Task 5 (Knowledge Bases) |
| `GUARDRAIL_ID`, `GUARDRAIL_VERSION` | Task 3 (guardrail) |
| `AGENTCORE_RUNTIME_ARN` | Task 3 (deploy) |

`(not yet created)` in the `config.py` output before a task is done is **normal**.

## 8. Windows pitfalls (all of these actually happened)

| Problem | Symptom | Fix |
|---|---|---|
| cp1252 console encoding | `UnicodeEncodeError: 'charmap' codec can't encode character '✓'` | `$env:PYTHONUTF8=1` before running Python |
| Windows clock out of sync | `SignatureDoesNotMatch: Signature expired` with fresh credentials | Settings → Time & language → Date & time → *Set time automatically* + **Sync now** |
| Background process with stdin open | `agentcore --version` hangs forever when called from a script | Run from a normal terminal, or close stdin (`< /dev/null` in bash) |
