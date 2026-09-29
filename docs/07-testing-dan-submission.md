# 07 — Testing, Uji End-to-End, Submission & Cleanup

## 1. Test otomatis (120 poin)

```powershell
cd cs-bedrock-agentcore-chatbot-v3
.\venv\Scripts\activate
$env:PYTHONUTF8=1
python tests/test_agent.py all
```

| Task | Yang dicek | Poin |
|---|---|---|
| 2 | 5 builder mengembalikan Agent, jumlah tool (3 / 1 / 5), model Haiku vs Sonnet, ThreadPoolExecutor | 40 |
| 3 | Guardrail ada, policy sesuai spesifikasi, versi bernomor READY, runtime READY + PUBLIC/HTTP + 8 env var | 20 |
| 4 | Memory ACTIVE, strategy SUMMARIZATION, retensi 7 hari | 15 |
| 5 | 3 KB ID terisi, ACTIVE, Titan V2, sudah sync, retrieval paralel mengembalikan hasil | 25 |
| 6 | Env var logging INFO di runtime, log group ada, X-Ray 100% + Transaction Search aktif | 20 |
| **Total** | | **120** |

Semua test Task 3–6 **membaca kondisi nyata dari AWS**, tidak ada yang di-mock.

Hasil: **120/120 (100%)**.

## 2. Tiga skenario end-to-end wajib

| Skenario | Customer | Rute yang diharapkan |
|---|---|---|
| "I want to return my order ORD-27176" | CUST-001 | Orchestrator → Inventory → Refund → Communication |
| "What is the return policy for premium customers?" | CUST-002 | Orchestrator → Policy (3 KB paralel) → Communication |
| "How much are 5 items at $29.99 with 10% off?" | CUST-003 | Orchestrator → Communication (tanpa Inventory/Policy/Refund) |

Cara menjalankan:

```powershell
# Lokal (3 skenario sekaligus, masing-masing mencetak X-Ray trace id)
python src/agent_orchestrator.py test

# Chat interaktif dengan trace berwarna per agent
python src/agent_orchestrator.py chat

# Lewat runtime yang ter-deploy di AgentCore
python src/agent_orchestrator.py invoke "I want to return my order ORD-27176" CUST-001
python src/agent_orchestrator.py invoke "What is the return policy for premium customers?" CUST-002
python src/agent_orchestrator.py invoke "How much are 5 items at `$29.99 with 10% off?" CUST-003
```

> Di PowerShell, tanda `$` di dalam string berkutip dua harus di-escape dengan backtick
> (`` `$29.99 ``), atau gunakan kutip tunggal.

> Uji retur mengubah status ORD-27176 menjadi `return_initiated`. Sebelum demo atau
> screenshot, reset data dengan `python infrastructure/seed_data.py`.

## 3. Deliverable screenshot

### A. Skor test 120/120
1. Jalankan `python tests/test_agent.py all`.
2. Screenshot bagian daftar ✓ PASS dan baris `Score: 120/120 pts (100%)`.
3. Simpan sebagai `screenshots/test_all_120.png`.

### B. X-Ray Service Map
1. `python src/agent_orchestrator.py test` → tunggu sampai muncul
   "X-Ray trace ... published successfully".
2. Tunggu 30–60 detik.
3. AWS Console → **CloudWatch** → menu kiri **X-Ray traces → Service map**.
4. Rentang waktu: **Last 5 minutes** (atau 15 menit).
5. Pastikan terlihat `NovaMart-Orchestrator` → InventoryAgent, RefundAgent,
   PolicyAgent (→ KnowledgeBase:returns/shipping/warranty), CommunicationAgent.
6. Simpan sebagai `screenshots/xray_service_map.png`.

## 4. Checklist submission

- [x] `src/agent_orchestrator.py` — semua TODO Task 2, 3, 4, 6 terisi
- [x] 3 Knowledge Base dibuat & di-sync (Task 5)
- [x] `.env` berisi 3 KB ID, `AGENTCORE_RUNTIME_ARN`, `GUARDRAIL_ID`, `GUARDRAIL_VERSION`
      (file `.env` tidak di-commit; nilainya didokumentasikan di README)
- [ ] Screenshot test 120/120
- [ ] Screenshot X-Ray Service Map

## 5. Cleanup (SETELAH submit & dinilai)

```powershell
python infrastructure/cleanup.py            # dry run: daftar yang akan dihapus
python infrastructure/cleanup.py --yes      # hapus semuanya
python standout/create_dashboard.py --delete
python standout/dynamodb_session_manager.py delete-table
```

`cleanup.py` menghapus KB (beserta service role-nya), stack runtime
`AgentCore-udacity-default`, Memory, Guardrail, isi bucket, stack utama, dan
log group. Penghapusan KB dan AgentCore berjalan asinkron, jadi jalankan dua kali
untuk memastikan semuanya sudah bersih.

> Tunggu sampai project dinilai. Kalau reviewer meminta revisi, resource-nya masih dibutuhkan.
