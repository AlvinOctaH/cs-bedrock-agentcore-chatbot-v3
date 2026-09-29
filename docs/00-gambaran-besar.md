# 00 — Gambaran Besar: Apa yang Dibangun dan Kenapa

Catatan belajar ini menjelaskan project **NovaMart Multi-Agent Customer Support**
(Udacity AWS Agentic AI — Course 3, Project 3) langkah demi langkah, supaya bisa
dibangun ulang secara manual tanpa bantuan.

| File | Isi |
|---|---|
| [00-gambaran-besar.md](00-gambaran-besar.md) | Konsep, arsitektur, alur request (file ini) |
| [01-setup-infrastruktur.md](01-setup-infrastruktur.md) | CloudFormation, seed data, `.env`, tools lokal |
| [02-multi-agent-graph.md](02-multi-agent-graph.md) | Task 2 — 5 agent, tool, prompt, WorkflowState |
| [03-guardrail-dan-runtime.md](03-guardrail-dan-runtime.md) | Task 3 — Bedrock Guardrail + deploy AgentCore Runtime |
| [04-memory.md](04-memory.md) | Task 4 — AgentCore Memory |
| [05-knowledge-bases.md](05-knowledge-bases.md) | Task 5 — 3 Knowledge Base di AWS Console |
| [06-observability.md](06-observability.md) | Task 6 — CloudWatch Logs + X-Ray |
| [07-testing-dan-submission.md](07-testing-dan-submission.md) | Test 120 poin, skenario E2E, screenshot, cleanup |
| [08-standout.md](08-standout.md) | Fitur tambahan agar project "stand out" |
| [09-troubleshooting.md](09-troubleshooting.md) | Semua error yang ditemui dan solusinya |

---

## 1. Masalah bisnis

NovaMart (e-commerce fiktif) menerima ribuan request support per hari: cek status
order, retur/refund, pertanyaan kebijakan. Agent manusia harus mencari data order,
membaca kebijakan, lalu menulis balasan — lambat dan tidak konsisten.

Tujuan: **lapisan AI** yang memahami request, mengambil data dari beberapa sumber,
memutuskan sesuai kebijakan, dan menulis balasan profesional — otomatis, aman
(guardrail) dan bisa diaudit (observability).

## 2. Pola arsitektur: Orchestrator → Workers

Daripada satu agent raksasa yang memegang semua tool, tugas dipecah ke
**spesialis**. Satu agent "manajer" (Orchestrator) hanya memutuskan *siapa*
yang mengerjakan.

```
Customer Request
      │
OrchestratorAgent  (Claude Haiku 4.5, temp 0.0)  ── routing + WorkflowState
      │
 ┌────┼──────────────┬──────────────┬───────────────────┐
 │    │              │              │                   │
Inventory        Policy           Refund          Communication
(DynamoDB)   (multi-agent RAG)   (keputusan)      (balasan akhir)
                  │
     ┌────────────┼────────────┐       ← berjalan PARALEL
 Returns       Shipping      Warranty      (3 retriever sub-agent)
 KB            KB            KB

Shared state: DynamoDB WorkflowState (optimistic locking)
```

Kenapa dipecah?

- **Prompt lebih fokus** → tiap agent lebih akurat di bidangnya.
- **Least privilege** → hanya RefundAgent yang bisa mengubah order.
- **Model berbeda per peran** → routing pakai Haiku (cepat & murah), reasoning pakai
  Sonnet (lebih pintar). Ini optimasi biaya/latensi yang umum di production.
- **Mudah diuji & diganti** → tiap worker bisa dites sendiri.

## 3. Peran tiap agent

| Agent | Model / temp | Tool | Tanggung jawab |
|---|---|---|---|
| **Orchestrator** | Haiku 4.5 / 0.0 | `initialize_session`, `route_to_*` ×4 | Routing + kelola WorkflowState. **Tidak pernah** menjawab customer sendiri |
| **Inventory** | Sonnet 4.5 / 0.1 | `check_order_status`, `get_customer_tier`, `list_customer_orders` | Kumpulkan fakta dari DynamoDB. Tidak memutuskan apa pun |
| **Policy** | Sonnet 4.5 / 0.2 (retriever 0.0) | `search_all_policies` | Koordinator RAG: 3 retriever paralel → sintesis jawaban |
| **Refund** | Sonnet 4.5 / 0.1 | `get_inventory_context`, `initiate_refund` | Putuskan kelayakan retur (Standard 30 hari, Premium 60 hari) |
| **Communication** | Sonnet 4.5 / 0.3 | `get_full_workflow_context` | Tulis balasan akhir yang hangat & profesional |

**Kenapa temperature berbeda?** Temperature mengatur "kreativitas" model.
Routing & retrieval harus deterministik (0.0), keputusan refund hampir
deterministik (0.1), sintesis kebijakan sedikit fleksibel (0.2), dan balasan ke
customer butuh bahasa yang natural (0.3).

## 4. Aturan routing (inti Orchestrator)

| Rule | Pemicu | Aksi |
|---|---|---|
| 1 | Setiap request | `initialize_session` dulu |
| 2 | Status order / retur / refund | Inventory → Refund |
| 3 | Pertanyaan makna kebijakan | Policy |
| 4 | Pertanyaan akun ("tier saya apa?") | Inventory — **bukan** Policy |
| 5 | Hitungan matematika | Langsung ke Communication |
| 6 | Setiap request (terakhir) | Communication menulis balasan |

## 5. Shared WorkflowState + optimistic locking

Agent-agent tidak saling memanggil langsung. Mereka berbagi **satu record DynamoDB**
per sesi (`session_id`), dan tiap worker menulis ke kolomnya sendiri:

```
version 0  initialize_session()      → {session_id, customer_id, version: 0}
version 1  InventoryAgent menulis    → + inventory_agent
version 2  RefundAgent menulis       → + refund_agent
version 3  CommunicationAgent menulis→ + communication_agent
```

**Optimistic locking**: setiap update membawa `expected_version`. DynamoDB hanya
menerima update jika `version` di tabel masih sama (`ConditionExpression`). Kalau
ada penulis lain yang lebih dulu, update gagal → baca ulang → coba lagi. Ini
mencegah dua agent saling menimpa hasil tanpa perlu "lock" yang memblokir.

## 6. Contoh alur: "I want to return my order ORD-27176" (CUST-001)

1. Orchestrator → `initialize_session` → record dibuat (v0).
2. Orchestrator → `route_to_inventory_agent` → Inventory memanggil
   `get_customer_tier` (Premium) + `check_order_status` (delivered 7 hari lalu) →
   hasil ditulis ke `inventory_agent` (v1).
3. Orchestrator → `route_to_refund_agent` → Refund membaca `get_inventory_context`,
   menerapkan jendela 60 hari → eligible → `initiate_refund` mengubah order jadi
   `return_initiated` + nomor RMA → keputusan ditulis ke `refund_agent` (v2).
4. Orchestrator → `route_to_communication_agent` → Communication membaca seluruh
   state → menulis balasan → `communication_agent` (v3).
5. Balasan dikirim ke customer. Seluruh perjalanan terekam sebagai satu trace X-Ray.

## 7. Layanan AWS yang dipakai

| Layanan | Untuk apa |
|---|---|
| **Amazon Bedrock** (Claude Haiku 4.5 & Sonnet 4.5) | Otak semua agent |
| **Strands Agents SDK** | Framework Python untuk membuat agent + `@tool` |
| **DynamoDB** | Tabel orders, customers, workflow-state |
| **S3 + S3 Vectors** | Dokumen kebijakan + penyimpanan vektor |
| **Bedrock Knowledge Bases** | RAG terkelola (chunking, embedding, retrieval) |
| **Bedrock Guardrails** | Filter konten, PII, topik terlarang, profanity |
| **AgentCore Runtime** | Hosting agent sebagai HTTP service terkelola |
| **AgentCore Memory** | Ringkasan percakapan per sesi (7 hari) |
| **CloudWatch Logs + X-Ray** | Log & distributed tracing |
| **CloudFormation / CDK** | Infrastructure as code |
