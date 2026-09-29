# 04 — Task 4: AgentCore Memory

File: `src/agent_orchestrator.py` → `configure_memory()` · Test: `python tests/test_agent.py task4` (15 poin)

## Konsep

**AgentCore Memory** adalah penyimpanan percakapan terkelola. Ada dua lapisan:

| Lapisan | Isi | Masa simpan |
|---|---|---|
| **Short-term (events)** | Setiap pesan mentah (user/assistant) sebagai *event* | `eventExpiryDuration` (di sini **7 hari**) |
| **Long-term (records)** | Hasil ekstraksi dari event oleh *memory strategy* | Sampai dihapus |

Memory strategy menentukan apa yang diekstrak:

| Strategy | Hasil |
|---|---|
| `semanticMemoryStrategy` | Fakta-fakta ("customer punya anjing") |
| `userPreferenceMemoryStrategy` | Preferensi ("suka jawaban singkat") |
| **`summaryMemoryStrategy`** ← dipakai | **Ringkasan per sesi** |

Kenapa SESSION_SUMMARY? Untuk customer support, yang paling berguna adalah konteks
sesi yang sedang berjalan ("tadi saya sudah kasih nomor order"), sehingga customer
tidak perlu mengulang informasi antar-giliran.

## Implementasi

```python
response = agentcore_control.create_memory(
    name=memory_name,                                   # config.MEMORY_NAME = udacity_agentcore_memory
    description='NovaMart customer support conversation memory: ...',
    eventExpiryDuration=7,                              # hari
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

- **Nama** memakai underscore (`udacity_agentcore_memory`) karena nama resource
  AgentCore tidak boleh memakai tanda hubung.
- **Namespace** `/summaries/{actorId}/{sessionId}` → ringkasan disimpan per customer
  (`actorId`) per sesi. `{...}` adalah placeholder yang diisi AgentCore.
- **`clientToken`** → kalau request terkirim dua kali (retry jaringan), AWS tidak
  membuat memory ganda.
- Kode bawaan menunggu status `ACTIVE`, lalu mengembalikan `memoryArn`.
- Kode bawaan juga mengecek apakah memory dengan nama itu sudah ada, sehingga
  `deploy` aman dijalankan berulang kali.

## Verifikasi

```powershell
python tests/test_agent.py task4
aws bedrock-agentcore-control list-memories --query "memories[].[id,status]"
```

Test memeriksa: memory ber-prefix `udacity_agentcore_memory` ada, status `ACTIVE`,
strategy bertipe `SUMMARIZATION`, dan `eventExpiryDuration == 7`.

## Hubungan dengan WorkflowState dan session memory lokal

| Mekanisme | Cakupan | Fungsi |
|---|---|---|
| **WorkflowState** (DynamoDB) | 1 request/turn | Papan tulis bersama antar-agent |
| **AgentCore Memory** | Sesi & lintas sesi, 7 hari | Ringkasan percakapan jangka panjang di cloud |
| **Session storage lokal** (standout, lihat 08) | 1 sesi chat | Riwayat pesan Orchestrator untuk multi-turn |
