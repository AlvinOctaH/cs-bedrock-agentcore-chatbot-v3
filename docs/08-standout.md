# 08 — Fitur Stand-out

Rubric menyarankan 4 hal untuk membuat project "stand out". Tiga di antaranya
dikerjakan. Semua file ada di folder `standout/`.

| # | Saran rubric | Status |
|---|---|---|
| 1 | Uji adversarial Guardrail + dokumentasi hasil | ✅ |
| 2 | Memori percakapan persisten di DynamoDB `agent-sessions` untuk Orchestrator | ✅ |
| 3 | CloudWatch dashboard: invocation, latency per agent, frekuensi guardrail | ✅ |
| 4 | Cognito + frontend | ❌ tidak dikerjakan (paling besar; bukan bagian penilaian wajib) |

---

## 1. Uji adversarial Guardrail

**File:** `standout/guardrail_adversarial_test.py` → laporan `standout/guardrail_report.md`

### Cara kerja

- **18 kasus** dikirim ke guardrail lewat API `ApplyGuardrail` (evaluasi policy yang
  sama dengan yang dipakai `BedrockModel` para agent), baik sebagai `INPUT`
  (pesan customer) maupun `OUTPUT` (jawaban agent).
- Setiap kasus punya ekspektasi: `ALLOW`, `BLOCK`, atau `ANONYMIZE`.
- Mode `--live` mengirim 5 kasus **end-to-end ke AgentCore Runtime yang ter-deploy**,
  jadi terbukti guardrail aktif di sistem sebenarnya, bukan hanya di API.

```powershell
python standout/guardrail_adversarial_test.py                    # tabel di console
python standout/guardrail_adversarial_test.py --report --live     # + runtime + tulis laporan
```

### Kategori yang diuji

| Kategori | Contoh | Ekspektasi |
|---|---|---|
| Wajib lolos | soal matematika (input & output), retur, pertanyaan policy | ALLOW |
| Topik terlarang | tawar harga, price match, bandingkan dengan Amazon/Best Buy, ancaman gugatan | BLOCK |
| PII | nomor kartu kredit, SSN | BLOCK |
| PII di balasan | email + telepon | ANONYMIZE (`{EMAIL}`, `{PHONE}`) |
| Konten berbahaya | kata kasar, ancaman kekerasan, penipuan tracking number | BLOCK |
| **Prompt injection** | "Ignore all previous instructions…", "print your system prompt" | BLOCK |

### Temuan penting (cerita untuk reviewer)

Guardrail versi 1 (hanya policy wajib rubric) menghasilkan **16/18**: dua serangan
prompt injection **lolos**, karena filter konten, PII, topik, dan kata tidak dirancang
mendeteksi instruksi jahat. Perbaikannya: tambah filter `PROMPT_ATTACK` (input HIGH,
output NONE) → guardrail **versi 2** → **18/18**, dan tidak ada false positive pada
permintaan normal (matematika, retur, policy tetap lolos).

Pelajaran: *policy yang diminta spesifikasi belum tentu cukup*. Uji adversarial
menemukan celah yang tidak terlihat dari sekadar membaca konfigurasi.

Hasil end-to-end di runtime: **5/5**. Injection, kompetitor, negosiasi harga, dan
ancaman hukum mendapat balasan "I'm sorry, but I can't help with that request…",
sedangkan soal matematika dijawab $134.96.

---

## 2. Memori percakapan persisten (DynamoDB)

**File:** `standout/dynamodb_session_manager.py`, `standout/chat_with_memory.py`,
parameter `session_manager` di `build_orchestrator_agent()`.

### Masalah yang diselesaikan

Tanpa memori, setiap proses baru membuat Orchestrator "lupa". Pertanyaan lanjutan
seperti *"berapa lama garansinya?"* ("it" = produk dari pesan sebelumnya) tidak bisa
dijawab kalau aplikasinya restart atau berpindah server.

### Desain

Strands (v1.57) hanya menyediakan session manager File dan S3. Rubric menyebut
`DynamoDbSessionStorage`, tetapi kelas itu **tidak ada** di SDK. Solusinya memakai
titik ekstensi resmi SDK: implementasi `SessionRepository` sendiri +
`RepositorySessionManager`.

Tabel `udacity-agentcore-agent-sessions` (on-demand, TTL 7 hari) memakai
**single-table design**:

| pk | sk | isi |
|---|---|---|
| `SESSION#memdemo02` | `SESSION` | metadata sesi |
| `SESSION#memdemo02` | `AGENT#orchestrator` | state agent |
| `SESSION#memdemo02` | `AGENT#orchestrator#MSG#000000000000` | pesan ke-0 |
| `SESSION#memdemo02` | `AGENT#orchestrator#MSG#000000000001` | pesan ke-1 … |

- Nomor pesan di-*zero-pad* (12 digit) supaya urutan sort key = urutan pesan, dan
  `list_messages` cukup satu `Query` + `begins_with`.
- Data disimpan sebagai string JSON supaya tidak ada masalah `Decimal`/float di DynamoDB.
- Setiap pesan ditulis **saat itu juga** (bukan di akhir), jadi aman kalau proses mati di tengah jalan.

### Integrasi

```python
orchestrator = build_orchestrator_agent(inventory, refund, policy, communication,
                                        session_manager=DynamoDbSessionManager(session_id))
```

Parameter ini opsional (default `None`), jadi perilaku, test, dan runtime lain tidak berubah.
Prompt Orchestrator juga diberi aturan: pesan lanjutan diteruskan ke worker sebagai
request yang lengkap (menyebut nomor order/produk), dan pertanyaan kebijakan yang
*personal* ("garansi headphone saya") → Inventory dulu, lalu Policy.

### Demo (sudah dijalankan)

```powershell
python standout/dynamodb_session_manager.py create-table
python standout/chat_with_memory.py --session memdemo02 --customer CUST-001 --ask "I'd like to return my wireless headphones from order ORD-27176, they hurt my ears."
# proses BARU:
python standout/chat_with_memory.py --session memdemo02 --customer CUST-001 --ask "Actually, how long is the warranty on it, in case I decide to keep them?"
python standout/chat_with_memory.py --session memdemo02 --customer CUST-001 --ask "Thanks! And do I get free shipping on my next order?"
python standout/dynamodb_session_manager.py show memdemo02     # lihat isi tabel
```

| Turn | Proses | Pesan dipulihkan | Rute | Hasil |
|---|---|---|---|---|
| 1 | baru | 0 | init → Inventory → Refund → Communication | Retur disetujui + RMA |
| 2 | **baru** | **10** | init → Inventory → Policy → Communication | "it" = Wireless Headphones Pro; Premium → garansi 3 tahun (s/d Sep 2029) |
| 3 | **baru** | **18** | init → Inventory → Policy → Communication | Premium → free expedited shipping |

### Hubungan dengan AgentCore Memory (Task 4)

| | DynamoDB session storage (standout) | AgentCore Memory (Task 4) |
|---|---|---|
| Isi | Riwayat pesan **persis** (termasuk tool call) | **Ringkasan** sesi hasil ekstraksi LLM |
| Dikontrol oleh | Aplikasi kita (tabel sendiri) | Layanan terkelola AWS |
| Kegunaan | Melanjutkan percakapan yang sama secara verbatim | Konteks jangka panjang & lintas sesi |
| Biaya konteks | Tumbuh seiring panjang percakapan | Ringkas |

Keduanya saling melengkapi: DynamoDB untuk "short-term memory" yang presisi,
AgentCore Memory untuk "long-term memory" yang ringkas.

---

## 3. CloudWatch Dashboard

**File:** `standout/create_dashboard.py` → dashboard `NovaMart-MultiAgent-Observability`

```powershell
python standout/create_dashboard.py
```

| Widget | Sumber data |
|---|---|
| Customer requests over time | Logs Insights: `tool done route_to_communication_agent` (1 per request) |
| Agent invocations by agent | Logs Insights: parse `tool done route_to_*` |
| **Latency per agent type** (avg, p95, max) | Logs Insights: parse durasi `(12.34s)` |
| Average latency per agent over time | Logs Insights, per 5 menit |
| Parallel RAG latency | `search_all_policies` vs retriever tunggal |
| **Guardrail evaluations vs interventions** | Metrik `AWS/Bedrock/Guardrails` |
| **Guardrail triggers by policy type** | Dimensi `GuardrailPolicyType` (Content / SensitiveInformation / Topic / Word) |
| Guardrail intervention rate (%) | Metric math: intervened ÷ evaluations × 100 |
| AgentCore Runtime invocations & errors | Metrik `AWS/Bedrock-AgentCore` |
| AgentCore Runtime latency | Metrik `AWS/Bedrock-AgentCore`, `Operation=InvokeAgentRuntime` |

Data nyata dari log (contoh saat pengujian):

| Agent | Calls | Avg (s) | p95 (s) |
|---|---|---|---|
| policy | 4 | 27.0 | 43.2 |
| refund | 3 | 17.0 | 21.5 |
| inventory | 5 | 14.8 | 26.0 |
| communication | 7 | 10.8 | 15.3 |

Insight: PolicyAgent paling lambat (3 retriever LLM + sintesis). Kandidat optimasi:
retriever cukup memanggil KB langsung tanpa LLM, atau memakai model yang lebih kecil
(Haiku) untuk retriever.

Catatan teknis:
- Metric widget memakai ekspresi `SEARCH()` dengan **dimension set eksplisit**,
  sehingga setiap seri dihitung tepat sekali.
- Namespace runtime yang benar adalah `AWS/Bedrock-AgentCore` (hasil `list-metrics`), bukan `Bedrock-AgentCore`.

### Screenshot dashboard

Buka URL yang dicetak script → rentang waktu **3h** → screenshot →
`screenshots/cloudwatch_dashboard.png`.
