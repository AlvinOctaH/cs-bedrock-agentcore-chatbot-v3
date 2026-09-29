# 09 — Troubleshooting (masalah yang benar-benar ditemui)

Setiap baris di bawah ini **benar-benar terjadi** saat mengerjakan project ini,
lengkap dengan penyebab dan solusinya.

## Environment & kredensial

| Gejala | Penyebab | Solusi |
|---|---|---|
| File starter berbeda dengan instruksi (Claude 3 vs 4.5; `agentcore_cli.py`, `agent_observability.py`, `cleanup.py` tidak ada) | Arsip `.tar.gz` yang diunduh adalah versi lama | Ambil starter terbaru dari `github.com/udacity/cd14764-aws-agentic-c3-classroom` → folder `project/starter` |
| `ExpiredToken` | Kredensial sementara Udacity habis | Buka sesi lab baru → pasang ulang 3 nilai kredensial |
| `AccessDenied ... explicit deny ... voc-cancel-cred` | Sesi lab sudah ditutup; kredensial lama dicabut | Sama dengan di atas. Resource yang sudah dibuat tetap ada |
| `SignatureDoesNotMatch: Signature expired` padahal kredensial baru | **Jam Windows terlambat 7 jam** (tidak sinkron). AWS menolak request dengan selisih waktu > 15 menit | Settings → Time & language → Date & time → *Set time automatically* → **Sync now** |
| `UnicodeEncodeError: 'charmap' codec can't encode character '✓'` | Console Windows memakai cp1252 dan tidak bisa mencetak ✓ | `$env:PYTHONUTF8=1` sebelum menjalankan Python |
| `git clone` repo Udacity gagal: *Filename too long* | Batas path 260 karakter di Windows | Ambil subfolder saja: `git archive HEAD project \| tar -x`, atau `git config core.longpaths true` |

## Deploy AgentCore

| Gejala | Penyebab | Solusi |
|---|---|---|
| `python src/agent_orchestrator.py deploy` diam selamanya setelah header | Dijalankan sebagai proses background dengan stdin terbuka; `agentcore --version` (Node) menunggu input | Jalankan dari terminal biasa, atau tutup stdin (`< /dev/null`) |
| Deploy pertama sangat lama (±15 menit) | Bootstrap CDK (`CDKToolkit`) + download wheel arm64 dengan `uv` | Normal. Deploy berikutnya ±3–5 menit |
| Warning `NodeVersionSupportWarning ... require node >=22` | AWS SDK JS akan meninggalkan Node 20 mulai 2027 | Aman diabaikan untuk sekarang; upgrade ke Node 22 jika sempat |
| Runtime tidak punya KB ID | Deploy dijalankan sebelum Task 5 | Buat KB dulu, isi `.env`, lalu `deploy` lagi |

## Agent & guardrail

| Gejala | Penyebab | Solusi |
|---|---|---|
| Prompt injection ("Ignore all previous instructions…") lolos guardrail | Policy wajib rubric tidak mencakup prompt attack | Tambah filter `PROMPT_ATTACK` (input HIGH, output NONE) → guardrail v2 |
| Balasan customer berisi `{EMAIL}@...` | CommunicationAgent menulis alamat email support; guardrail (email = ANONYMIZE) menyamarkannya | Prompt: jangan menulis email/telepon, arahkan customer untuk membalas pesan |
| Balasan hitungan memakai `**bold**` | Model cenderung memakai markdown | Prompt dipertegas: plain text, tanpa `**` / `#` |
| Soal matematika bisa terblokir sebagai "pricing negotiation" | Definisi topik terlalu luas (terjadi di tier Classic) | Topic tier STANDARD + definisi sempit ("haggling / changing an advertised price… calculating a total is not negotiation") |
| (Potensi) data customer bocor antar-request | Worker agent dibangun sekali & menyimpan riwayat pesan | `_run_worker()` menghapus `agent.messages` sebelum setiap pemanggilan |
| (Potensi) Refund jalan sebelum Inventory selesai | Strands default bisa mengeksekusi tool paralel | `tool_executor=SequentialToolExecutor()` pada Orchestrator |
| `initialize_session` error pada pesan kedua di sesi yang sama | `_create_workflow_state` memakai `attribute_not_exists` | Tangani `ConditionalCheckFailedException` → mulai "turn" baru (kolom agent dikosongkan) |
| `The provided key element does not match the schema` | Tabel orders memakai composite key | `get_item(Key={'customer_id': ..., 'order_id': ...})` |
| `ValidationException ... reserved keyword: status` | `status` adalah reserved word DynamoDB | `ExpressionAttributeNames={'#status': 'status'}` |
| `TypeError: Object of type Decimal is not JSON serializable` | boto3 mengembalikan angka sebagai `Decimal` | Helper `_json_safe()` |

## Knowledge Base

| Gejala | Solusi |
|---|---|
| Structured atau unstructured? | **Unstructured** (dokumen teks) |
| Vector index tidak muncul | Region harus us-east-1; stack harus `CREATE_COMPLETE` |
| `retrieve()` kosong | Klik **Sync**; pastikan ID di `.env` tidak tertukar |
