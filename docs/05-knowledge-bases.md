# 05 — Task 5: Three Bedrock Knowledge Bases (manual, AWS Console)

> There is **no code** in this task — it is done in the AWS Console. The result is
> three Knowledge Base IDs in `.env`.

## Concept

A **Knowledge Base (KB)** is Bedrock's managed RAG service. Point it at a folder
(prefix) in S3 and Bedrock automatically:

1. **Parses & chunks** the documents into small pieces.
2. **Embeds** each chunk into a vector with *Titan Text Embeddings V2* (1024 dimensions).
3. **Indexes** the vectors in **S3 Vectors** (serverless, cheap, no OpenSearch cluster).

At query time `bedrock-agent-runtime.retrieve()` embeds the question, finds the most
similar chunks (cosine similarity) and returns the top-k passages.

Why **three separate KBs** instead of one? The PolicyAgent uses *multi-agent RAG*:
three retriever sub-agents (Returns, Shipping, Warranty) each own a KB and run **in
parallel**. Each domain stays isolated — a "warranty" search is never polluted by
shipping documents.

Everything needed was created by the CloudFormation stack + `seed_data.py`:

| Ingredient | Value (see `python config.py`) |
|---|---|
| Document bucket | `udacity-agentcore-policy-docs-<ACCOUNT_ID>-<suffix>` |
| Vector bucket (S3 Vectors) | `udacity-agentcore-vectors-<ACCOUNT_ID>-<suffix>` |
| Vector indexes | `returns-policy-index`, `shipping-policy-index`, `warranty-policy-index` (1024 dims, cosine, float32) |

## Step 0 — Check the documents in S3

```bash
python config.py        # note "Policy Bucket" and "Vector Bucket"
aws s3 ls s3://<Policy Bucket>/policies/ --recursive
```

You should see 6 files: `return_policy.txt`, `shipping_policy.txt`,
`warranty_policy.txt`, and `customer_tiers.txt` in all three folders.

## Step 1 — Create the Returns KB

1. Open the **AWS Console** → make sure the region is **N. Virginia (us-east-1)** (top right).
2. Search **Amazon Bedrock** → left menu **Build → Knowledge Bases**.
3. **Create** → **Knowledge Base with vector store**. When asked for the data type,
   choose **Unstructured data** (plain text documents).
   > ⚠️ Do not choose *Managed Knowledge Base* — it creates a different vector
   > bucket from the stack's, and the backing store cannot be changed later.
4. **Step 1 – Knowledge Base details**
   - Name: `novamart-returns-policy-kb`
   - IAM permissions: **Create and use a new service role** (default name)
   - Data source: **Amazon S3** → **Next**
5. **Step 2 – Data source**
   - S3 URI: **Browse S3** → Policy Bucket → `policies/` → select the radio button of
     `returns/` → **Choose** → `s3://<Policy Bucket>/policies/returns/`
   - Parsing & chunking: **Default** → **Next**
6. **Step 3 – Storage and processing**
   - Embeddings model: **Select model** → **Amazon → Titan Text Embeddings V2** → Apply
     (floating-point vectors, **1024** dimensions)
   - Vector store creation method: **Use an existing vector store**
   - Vector store: **Amazon S3 Vectors**
   - S3 vector bucket ARN: `udacity-agentcore-vectors-…`
   - S3 vector index ARN: **`returns-policy-index`** → **Next**
7. **Step 4 – Review and create** → **Create Knowledge Base** (~1 minute).
8. On the KB page → **Data source** → tick the data source → **Sync** → wait for
   **Available / Completed**.
9. Copy the **Knowledge Base ID** (10 characters, e.g. `ABCD1234EF`).

## Steps 2 & 3 — Shipping and Warranty KBs

Repeat Step 1 with these differences (everything else identical — same embedding
model, same vector bucket, same *Unstructured* choice):

| Setting | Shipping KB | Warranty KB |
|---|---|---|
| Name | `novamart-shipping-policy-kb` | `novamart-warranty-policy-kb` |
| S3 URI | `s3://<Policy Bucket>/policies/shipping/` | `s3://<Policy Bucket>/policies/warranty/` |
| Vector index | `shipping-policy-index` | `warranty-policy-index` |

Remember to **Sync** each one.

## Step 4 — Fill in `.env`

```
RETURNS_KB_ID=<returns ID>
SHIPPING_KB_ID=<shipping ID>
WARRANTY_KB_ID=<warranty ID>
```

## Step 5 — Verify

```bash
python config.py                   # three KB IDs filled in
python tests/test_agent.py task5   # target 25/25
```

Optional console check: open a KB → **Test** → any model → ask *"How long is the
return window for Premium customers?"* → should answer 60 days citing
`return_policy.txt` / `customer_tiers.txt`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Structured or unstructured? | **Unstructured** (text documents) |
| Vector index not in the dropdown | Wrong region (must be us-east-1) or stack not `CREATE_COMPLETE` |
| Dimension error on create | Embedding model is not Titan V2 at 1024 dims — the stack's indexes are 1024 |
| `retrieve()` returns nothing | **Sync** not run, or IDs swapped in `.env` |
| task5 says "no completed ingestion job" | Sync still running / failed — check *Sync history* |
| Runtime deployed before the KBs existed | Re-run `python src/agent_orchestrator.py deploy` — editing `.env` doesn't update the runtime |
