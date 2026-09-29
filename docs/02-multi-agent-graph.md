# 02 — Task 2: Multi-Agent Graph (5 Agent)

File: `src/agent_orchestrator.py` · Test: `python tests/test_agent.py task2` (40 poin)

## 1. Konsep dasar Strands Agents

Strands membuat agent dari 3 bahan: **model**, **system prompt**, **tools**.

```python
from strands import Agent
from strands.models import BedrockModel
from agent_observability import tool        # = strands @tool + X-Ray subsegment

@tool
def get_customer_tier(customer_id: str) -> dict:
    """Retrieve a customer's tier ... (docstring = "manual" yang dibaca LLM)"""
    ...

model = BedrockModel(model_id=config.WORKER_MODEL_ID,
                     region_name=config.AWS_REGION, temperature=0.1)
agent = Agent(name='InventoryAgent', model=model,
              system_prompt="You are ...", tools=[get_customer_tier])
result = agent("What tier is CUST-001?")    # dipanggil seperti fungsi
```

Poin penting:
- **Docstring tool = kontrak dengan LLM.** Model memutuskan kapan memanggil tool
  hanya dari nama, parameter, dan docstring. Docstring yang jelas (tujuan, `Args`,
  `Returns`) = pemanggilan tool yang benar. Ini juga dinilai rubric.
- `@tool` diimpor dari `agent_observability` (bukan `strands`) supaya setiap
  pemanggilan tool otomatis tercatat di X-Ray.
- Model **tidak di-hardcode**: selalu `config.WORKER_MODEL_ID` /
  `config.ORCHESTRATOR_MODEL_ID` (rubric).
- Tool didefinisikan **di dalam** fungsi `build_*_agent()` → builder
  self-contained dan mengembalikan tepat satu `Agent`.

## 2. Helper bersama yang ditambahkan

| Helper | Fungsi | Kenapa |
|---|---|---|
| `RETURN_WINDOW_DAYS = {'Standard': 30, 'Premium': 60}` | Satu sumber kebenaran jendela retur | Dipakai prompt Refund **dan** pengecekan di `initiate_refund` |
| `_json_safe()` | Ubah `Decimal` DynamoDB → int/float | boto3 mengembalikan angka sebagai `Decimal`, tidak bisa di-serialize ke JSON |
| `_days_since()` | Hitung selisih hari dari tanggal ISO | LLM sering salah hitung tanggal → dihitung di kode, LLM tinggal membaca angka |
| `_evaluate_return_eligibility()` | Aturan kelayakan retur deterministik | Backstop: order tidak layak **tidak akan pernah** diproses walau LLM keliru |
| `_run_worker()` | Hapus riwayat percakapan worker lalu panggil | Agent dibangun sekali & dipakai ulang; tanpa ini data customer A bisa bocor ke request customer B |

## 3. 2.A — InventoryAgent (pengumpul fakta)

Model: Sonnet 4.5, temperature **0.1**.

| Tool | Operasi DynamoDB | Catatan |
|---|---|---|
| `check_order_status(customer_id, order_id)` | `get_item` pada tabel orders | Tabel orders punya **composite key** (`customer_id` partition + `order_id` sort) → `get_item` wajib dua-duanya. Tool juga menambahkan `days_since_order` dan `days_since_delivery` |
| `get_customer_tier(customer_id)` | `get_item` pada tabel customers | Mengembalikan profil + tier |
| `list_customer_orders(customer_id)` | `query` dengan `Key('customer_id').eq(...)` | Untuk request tanpa nomor order ("headphone saya") |

System prompt menekankan: **hanya melaporkan fakta, tidak pernah memutuskan
kelayakan retur** — keputusan itu milik RefundAgent. Outputnya berformat tetap
(`CUSTOMER / ORDER / DATES / TRACKING / NOTES`) sehingga mudah dibaca agent berikutnya.

## 4. 2.B — RefundAgent (pengambil keputusan)

Model: Sonnet 4.5, temperature **0.1**.

| Tool | Fungsi |
|---|---|
| `get_inventory_context(session_id)` | Baca kolom `inventory_agent` dari WorkflowState |
| `initiate_refund(customer_id, order_id, reason)` | Ubah order jadi `return_initiated`, buat nomor `RMA-XXXXXXXX` |

Proses keputusan di prompt: (1) selalu baca inventory context dulu → (2) ambil tier,
status, hari sejak delivery → (3) terapkan jendela 30/60 hari → (4) kalau layak
**dan** customer minta retur → `initiate_refund` → (5) kalau tidak layak jangan
dipanggil. Output: `DECISION: APPROVED | DENIED | NO_ACTION | NEEDS_INFO`.

Pengaman di dalam `initiate_refund` (praktik production):
- Cek ulang kelayakan secara deterministik sebelum menulis.
- `ConditionExpression='#status = :delivered'` → update atomik, hanya jika order
  masih `delivered` (mencegah retur ganda). `status` adalah **reserved word**
  DynamoDB sehingga harus memakai `ExpressionAttributeNames`.
- Idempotent: permintaan kedua untuk order yang sama mengembalikan nomor RMA yang sudah ada.

Jendela retur dihitung dari **tanggal delivery** (kebijakan: *"within 30 days of delivery"*),
memakai `estimated_delivery` di data seed.

## 5. 2.C — PolicyAgent (multi-agent RAG paralel) ⭐

Di dalam `build_policy_agent()` dibuat **3 sub-agent retriever**:

| Sub-agent | Tool | Knowledge Base |
|---|---|---|
| `ReturnsPolicyRetrieverAgent` | `retrieve_returns_policy` | `config.RETURNS_KB_ID` |
| `ShippingPolicyRetrieverAgent` | `retrieve_shipping_policy` | `config.SHIPPING_KB_ID` |
| `WarrantyPolicyRetrieverAgent` | `retrieve_warranty_policy` | `config.WARRANTY_KB_ID` |

Retriever memakai temperature **0.0** (deterministik) dan `callback_handler=None`
(tiga agent berjalan bersamaan; output streaming mereka akan tercampur di terminal).

Tool koordinator `search_all_policies(query)` menjalankan ketiganya **paralel**:

```python
with ThreadPoolExecutor(max_workers=3) as executor:
    futures = {executor.submit(_run_retriever, domain, agent, query): domain
               for domain, agent in retrievers.items()}
    for future in as_completed(futures):       # ambil hasil sesuai urutan selesai
        domain, text = future.result()
        results[domain] = text
```

Kenapa paralel? Tiap retriever = 1 panggilan LLM + 1 panggilan KB (±5–10 detik).
Berurutan = jumlah ketiganya; paralel = hanya selama yang paling lambat.
Operasi ini **I/O-bound** (menunggu jaringan), jadi thread Python efektif walau ada GIL.

Satu retriever gagal tidak menggagalkan yang lain (`try/except` per retriever).
Hasil digabung dengan urutan tetap (Returns → Shipping → Warranty).

Koordinator (temperature **0.2**) diinstruksikan: selalu panggil
`search_all_policies` dulu, jawab **hanya** dari passage yang ditemukan, sebutkan
sumber kebijakannya, gabungkan duplikat (dokumen tier ada di ketiga KB), dan kalau
ada aturan yang bertentangan (contoh: garansi elektronik 2 tahun vs tier Standard
1 tahun) jelaskan aturan mana yang lebih spesifik.

## 6. 2.D — CommunicationAgent (penulis balasan)

Model: Sonnet 4.5, temperature **0.3** (bahasa lebih natural).
Tool: `get_full_workflow_context(session_id)` → seluruh WorkflowState.

Aturan di prompt:
- Gunakan **semua** temuan yang relevan (keputusan, alasan, nomor RMA, langkah berikutnya).
- Kalau retur ditolak: jelaskan dengan empati + tawarkan alternatif (klaim garansi).
- Soal matematika: hitung langkah demi langkah, **bulatkan ke sen hanya di akhir**
  (round half up): 5 × $29.99 = $149.95 → diskon $14.995 → $134.955 → **$134.96**.
- Jangan pernah menyebut detail internal (nama agent, session ID, DynamoDB).
- Jangan menulis email/nomor telepon — guardrail akan menyamarkannya jadi `{EMAIL}`.
- Plain text, tanpa markdown.

## 7. 2.E — OrchestratorAgent (router)

Model: **Haiku 4.5**, temperature **0.0** (routing harus deterministik).

Setiap routing tool mengikuti pola yang sama (fungsi `_invoke_and_record`):

```
1. state = _read_workflow_state(session_id)          ← baca dulu
2. result = worker(prompt)                           ← panggil worker
3. _update_workflow_state(session_id,
       {'<kolom>': result},
       expected_version=state['version'])            ← tulis dengan optimistic locking
```

| Tool | Kolom yang ditulis |
|---|---|
| `initialize_session` | membuat record (version 0) |
| `route_to_inventory_agent` | `inventory_agent` |
| `route_to_policy_agent` | `policy_agent` |
| `route_to_refund_agent` | `refund_agent` |
| `route_to_communication_agent` | `communication_agent` |

System prompt memuat **6 aturan routing** + contoh urutan tool untuk tiap jenis
request, dan aturan KRITIS: Orchestrator tidak boleh menulis balasan sendiri;
`route_to_communication_agent` selalu panggilan terakhir, lalu teksnya diteruskan apa adanya.

Detail production yang ditambahkan:
- `tool_executor=SequentialToolExecutor()` — default Strands bisa mengeksekusi
  beberapa tool **bersamaan** jika model memanggilnya sekaligus. Routing ini
  harus berurutan (Refund butuh hasil Inventory), jadi dipaksa sekuensial.
- **Multi-turn**: `initialize_session` pada sesi yang sudah ada tidak error, tetapi
  memulai "turn" baru: kolom agent dikosongkan dan balasan sebelumnya disimpan di
  `previous_response`.
- Kalau model lupa `initialize_session`, routing tool membuat state-nya sendiri
  (defensive). Prompt Rule 1 juga dipertegas "termasuk pesan lanjutan", karena saat
  diuji dengan memori percakapan, Haiku sempat melewatinya di turn kedua.
- **Pertanyaan kebijakan personal** ("berapa lama garansi *headphone saya*?") →
  Inventory dulu (tier & order), baru Policy, sehingga jawabannya spesifik untuk
  customer itu. Pertanyaan umum ("return policy for premium customers") tetap Policy saja.
- **Pesan lanjutan** yang merujuk turn sebelumnya ("it", "that order") diteruskan ke
  worker sebagai request lengkap yang menyebut nomor order/produk.
- Parameter opsional `session_manager` → riwayat Orchestrator bisa disimpan di
  DynamoDB (lihat [08-standout.md](08-standout.md)).

## 8. Cara menguji

```powershell
$env:PYTHONUTF8=1
python tests/test_agent.py task2          # 40/40 — cek struktur (tool, model, ThreadPool)
python src/demo.py                        # 1 skenario refund end-to-end dengan trace
python src/agent_orchestrator.py chat     # chat interaktif, pilih CUST-001..004
```

Hasil uji lokal yang sudah dijalankan:

| Request | Rute | Hasil |
|---|---|---|
| Retur ORD-27176 (CUST-001, Premium, 7 hari) | Inventory → Refund → Communication | APPROVED + RMA |
| Retur ORD-28001 (CUST-002, Standard, 40 hari) | Inventory → Refund → Communication | DENIED, ditawari klaim garansi |
| "Am I a premium member?" (CUST-002) | Inventory → Communication | Standard (bukan lewat Policy) |
| 5 × $29.99 diskon 10% | Communication saja | $134.96 |
