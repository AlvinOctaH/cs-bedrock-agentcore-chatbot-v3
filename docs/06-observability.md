# 06 — Task 6: Observability (CloudWatch Logs + X-Ray)

File: `src/agent_orchestrator.py` → `configure_observability()` · Test: `python tests/test_agent.py task6` (20 poin)

## Konsep

Sistem multi-agent sulit di-debug: satu request melewati 3–5 agent, belasan
panggilan LLM, DynamoDB, dan Knowledge Base. Observability menjawab
*"apa yang terjadi, di mana, dan berapa lama?"*.

| Pilar | Layanan | Isi |
|---|---|---|
| **Logs** | CloudWatch Logs | Pemanggilan tool, argumen, durasi, trace id (level INFO) |
| **Traces** | AWS X-Ray | Pohon pemanggilan per request: Orchestrator → Worker → Knowledge Base, dengan latensi tiap node |

**Sampling rate** = persentase request yang di-trace. Dev: **1.0 (100%)** agar setiap
request terlihat. Production biasanya ~0.05 (5%) untuk menghemat biaya.

## Implementasi

```python
logging_configuration = {
    'cloudWatchConfig': {'logGroupName': config.AGENT_LOG_GROUP,
                         'logLevel': 'INFO', 'enabled': True},
    'xRayConfig':       {'enabled': True, 'samplingRate': 1.0},
}
try:
    summary = apply_observability_config(runtime_arn, logging_configuration)
    print(...log group dan sampling rate...)
except Exception as e:
    print(f"  [Note] Observability configuration failed: {e}")
```

`try/except` diminta rubric: kegagalan observability tidak boleh menggagalkan
deploy agent. Agent tetap melayani customer walaupun tracing bermasalah.

`apply_observability_config()` (sudah disediakan) lalu:
1. Membuat log group jika belum ada.
2. Mengaktifkan **CloudWatch Transaction Search** (mekanisme yang dipakai AgentCore
   Observability): tujuan segment X-Ray → CloudWatch Logs `aws/spans`, dengan
   indexing 100%.
3. Menyimpan setting sebagai env var runtime (`AGENT_LOG_LEVEL`,
   `AGENT_LOG_TO_CLOUDWATCH`, `AGENT_TRACING_ENABLED`, `AGENT_TRACE_SAMPLING_RATE`)
   dan menjalankan `agentcore deploy` kedua kalinya.

## Bagaimana trace terbentuk

`agent_observability.py` membungkus decorator `@tool`:

| Tool | Node di Service Map |
|---|---|
| `route_to_inventory_agent` | **InventoryAgent** |
| `route_to_policy_agent` | **PolicyAgent** |
| `route_to_refund_agent` | **RefundAgent** |
| `route_to_communication_agent` | **CommunicationAgent** |
| `retrieve_from_knowledge_base()` | **KnowledgeBase:returns / shipping / warranty** |

Hasilnya di X-Ray Service Map:

```
NovaMart-Orchestrator ─┬─ InventoryAgent
                       ├─ RefundAgent
                       ├─ PolicyAgent ─┬─ KnowledgeBase:returns
                       │               ├─ KnowledgeBase:shipping
                       │               └─ KnowledgeBase:warranty
                       └─ CommunicationAgent
```

Retriever berjalan di thread lain (ThreadPoolExecutor). Thread baru tidak
mewarisi *context* trace, jadi tracer memakai aturan fallback: node KB "diadopsi"
oleh node `search_all_policies` yang sedang terbuka. Karena itu graph tetap tersambung.

## Verifikasi dan screenshot

```powershell
python tests/test_agent.py task6
python src/agent_orchestrator.py test        # 3 skenario → cetak X-Ray trace id
```

Tunggu 30–60 detik → **CloudWatch → Application Signals (APM) → Trace Map** (nama lama: X-Ray traces → Service map) → rentang
*Last 5 minutes* → screenshot seluruh graph (deliverable wajib).

Log agent bisa dilihat di **CloudWatch → Log groups →
`/aws/bedrock/agentcore/udacity-agentcore`**.

> Tips screenshot: pilih rentang waktu yang mencakup **skenario retur dan skenario
> policy**. Rentang 5 menit setelah menjalankan skenario policy/matematika saja
> tidak akan menampilkan InventoryAgent dan RefundAgent. Pakai 15m atau 1h, lalu
> perbesar peta (+) sampai nama node terbaca.
