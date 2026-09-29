# 03 — Task 3: Bedrock Guardrail + Deploy ke AgentCore Runtime

File: `src/agent_orchestrator.py` → `create_guardrail()`, `_guardrail_policies()`,
`deploy_to_agentcore_runtime()` · Test: `python tests/test_agent.py task3` (20 poin)

---

## Bagian A — Bedrock Guardrail

### Konsep

Guardrail adalah **filter keamanan** yang dievaluasi Bedrock pada setiap panggilan
model, baik pada **input** (pesan customer) maupun **output** (jawaban model).
Kalau melanggar, pesan diganti dengan "blocked message" yang ramah.

Di project ini guardrail tidak dipasang ke runtime, melainkan ke **setiap
`BedrockModel`** milik 5 agent. Fungsi bawaan `_apply_guardrail()` memanggil
`model.update_config(guardrail_id=..., guardrail_version=...)` jika `GUARDRAIL_ID`
dan `GUARDRAIL_VERSION` tersedia (dari `.env` saat lokal, dari environment variable
runtime saat ter-deploy).

### Kebijakan yang dikonfigurasi

| Policy | Konfigurasi | Alasan |
|---|---|---|
| **Content filter** | SEXUAL, VIOLENCE, HATE = **HIGH**; INSULTS, MISCONDUCT = **MEDIUM** (input & output) | Blokir konten berbahaya; insult/misconduct sedikit lebih longgar agar customer yang kesal tidak langsung terblokir |
| **PII** | Kartu kredit & SSN = **BLOCK**; email & telepon = **ANONYMIZE** | Data pembayaran/identitas tidak boleh diproses sama sekali; kontak cukup disamarkan (`{EMAIL}`, `{PHONE}`) |
| **Denied topics** | Competitor Products, Pricing Negotiations, Legal Threats | Menjaga agent tetap on-topic dan menghindari komitmen bisnis/legal |
| **Word policy** | Managed **PROFANITY** list | Kata kasar |
| **Prompt attack** ⭐ (tambahan) | PROMPT_ATTACK = HIGH (input saja) | Hasil uji adversarial: injection lolos tanpa filter ini (lihat 08-standout) |

### Detail penting: topic tier STANDARD + definisi "pricing negotiation" yang sempit

```python
topicPolicyConfig={'topicsConfig': topics_config,
                   'tierConfig': {'tierName': 'STANDARD'}},
crossRegionConfig={'guardrailProfileIdentifier': 'us.guardrail.v1:0'},
```

- **Tier STANDARD** memakai model deteksi topik yang lebih akurat dibanding tier
  *Classic*. Tier ini **wajib** memakai *cross-region inference profile*
  (`us.guardrail.v1:0`), sehingga evaluasi bisa diproses di beberapa region US.
- Definisi topik harus hati-hati. Kalau "pricing negotiations" didefinisikan
  terlalu luas ("apa pun tentang harga dan diskon"), pertanyaan
  *"How much are 5 items at $29.99 with 10% off?"* ikut terblokir (false positive).
  Karena itu definisinya dibuat sempit:
  > *Haggling or asking NovaMart to lower, match or change an advertised price or to
  > grant an unoffered discount. Calculating a total from a stated price and discount
  > is not negotiation.*

Setiap topik juga diberi 3–4 **contoh kalimat** (`examples`) agar klasifikasinya lebih tepat.

### Kenapa harus "numbered version", bukan DRAFT?

`create_guardrail()` menghasilkan versi **DRAFT** yang masih bisa berubah.
`create_guardrail_version()` membekukannya menjadi versi **1, 2, ...** (immutable).
Production harus menunjuk versi bernomor supaya perubahan pada DRAFT tidak diam-diam
mengubah perilaku agent yang sudah berjalan. Kode juga menunggu status `READY`.

### Refactor: `_guardrail_policies()`

Semua policy dikumpulkan di satu helper yang mengembalikan `dict` argumen. Dipakai oleh:
- `create_guardrail()` → `bedrock.create_guardrail(name=..., **_guardrail_policies())`
- pembaruan guardrail yang sudah ada → `bedrock.update_guardrail(..., **_guardrail_policies())`
  lalu `create_guardrail_version()` → versi 2.

Riwayat versi di akun ini:
| Versi | Isi |
|---|---|
| 1 | Semua policy wajib |
| **2** (dipakai) | + filter PROMPT_ATTACK setelah uji adversarial |

### Cara memvalidasi

```powershell
python standout/guardrail_adversarial_test.py --report   # 18 kasus, target 18/18
```

---

## Bagian B — Deploy ke AgentCore Runtime

### Konsep

**AgentCore Runtime** = layanan terkelola yang menjalankan kode agent sebagai HTTP
service (serverless, auto-scaling, isolasi per sesi). Kontraknya sederhana:
`POST /invocations` dan `GET /ping` di port 8080. `BedrockAgentCoreApp` di
`run_serve()` sudah menyediakannya.

AgentCore **tidak menyimpan objek Python**. Yang di-upload adalah **kode**, lalu
runtime menjalankan `agent_orchestrator.py` (mode `serve`) dan membangun ulang
graph 5 agent pada request pertama.

### Alur deploy (AgentCore CLI)

```
stage_runtime_code()   → salin src/*.py + config.py ke build/runtime/ + pyproject.toml
configure_runtime()    → tulis setting ke agentcore/agentcore.json
deploy()               → `agentcore deploy -y`:
                           uv download wheel arm64 / Python 3.12
                           zip kode + dependency ("direct code deployment")
                           CDK → CloudFormation stack "AgentCore-udacity-default"
deployed_runtime_arn() → baca ARN dari agentcore/.cli/deployed-state.json
```

### Yang diimplementasikan

```python
runtime_env = {
    'AWS_REGION': ..., 'PROJECT_NAME': ...,
    'RETURNS_KB_ID': ..., 'SHIPPING_KB_ID': ..., 'WARRANTY_KB_ID': ...,
    'AGENT_LOG_GROUP': ...,
    'GUARDRAIL_ID': guardrail_id, 'GUARDRAIL_VERSION': guardrail_version,
}
agentcore_cli.configure_runtime(env_vars=runtime_env, network_mode='PUBLIC',
                                protocol='HTTP',
                                execution_role_arn=config.AGENTCORE_ROLE_ARN)
agentcore_cli.deploy()
runtime_arn = agentcore_cli.deployed_runtime_arn()
```

| Setting | Nilai | Artinya |
|---|---|---|
| `network_mode` | `PUBLIC` | Runtime bisa mengakses endpoint AWS publik (tanpa VPC) |
| `protocol` | `HTTP` | Kontrak `/invocations` biasa (alternatif: MCP, A2A) |
| `execution_role_arn` | role dari stack | IAM role yang di-assume runtime → izin Bedrock, DynamoDB, KB, X-Ray |
| env vars | 8 variabel | Di dalam runtime tidak ada `.env`, jadi config dan guardrail dibaca dari sini |

Kode juga memberi peringatan jika ada variabel kosong (misalnya KB ID belum ada),
karena `configure_runtime` melewati nilai kosong.

### Menjalankan

```powershell
python src/agent_orchestrator.py deploy
```

Pipeline 6 langkah: build graph → guardrail → **deploy runtime** → memory →
observability (deploy ke-2 untuk env var logging/tracing) → gateway (opsional, di-skip).
Deploy pertama ±5–15 menit (bootstrap CDK). Setelah selesai, salin nilai yang dicetak ke `.env`:

```
AGENTCORE_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-east-1:<account>:runtime/udacity_agentcore_runtime-XXXX
GUARDRAIL_ID=...
GUARDRAIL_VERSION=2
```

Cek:

```powershell
python tests/test_agent.py task3
python src/agent_orchestrator.py invoke "What is the return policy for premium customers?" CUST-002
agentcore status        # detail runtime
agentcore logs          # log runtime
```

### Urutan yang benar (penting!)

Task 5 (KB) **harus selesai sebelum deploy**, karena test Task 3 memeriksa bahwa
runtime memiliki `RETURNS_KB_ID` dkk. Jika sudah terlanjur deploy sebelum KB ada,
jalankan ulang `deploy`. Mengubah `.env` saja **tidak** mengubah runtime yang sudah berjalan.
