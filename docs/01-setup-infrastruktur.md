# 01 — Setup Infrastruktur & Environment

Tujuan: menyiapkan semua resource AWS dasar + environment lokal sebelum menulis kode.

## 1. Prasyarat lokal

| Tool | Versi | Cek |
|---|---|---|
| Python | **3.12** (sama dengan runtime AgentCore) | `py -3.12 --version` |
| AWS CLI | v2 | `aws --version` |
| Node.js | 20+ (dipakai AgentCore CLI) | `node --version` |
| uv | terbaru (dipakai `agentcore deploy` untuk download wheel arm64) | `uv --version` |
| AgentCore CLI | **0.30.0** | `agentcore --version` |

Install AgentCore CLI:

```bash
npm install -g @aws/agentcore@0.30.0
agentcore --version
```

> Kalau sebelumnya pernah install `bedrock-agentcore-starter-toolkit` (Python), hapus
> dulu: `pip uninstall bedrock-agentcore-starter-toolkit` — keduanya memakai nama
> perintah `agentcore` yang sama.

## 2. Kredensial AWS (Udacity Cloud Lab)

Akun Udacity memakai **kredensial sementara** (access key + secret + session token)
yang habis setelah beberapa jam.

1. Udacity → **Cloud Resources / Launch AWS Gateway** → salin 3 nilai kredensial.
2. Pasang:
   ```bash
   aws configure set aws_access_key_id     <ACCESS_KEY>
   aws configure set aws_secret_access_key <SECRET_KEY>
   aws configure set aws_session_token     <SESSION_TOKEN>
   aws configure set region us-east-1
   aws sts get-caller-identity          # harus menampilkan Account ID
   ```
3. Jangan pernah commit kredensial ke Git (`.env` dan `~/.aws` tidak ikut di-commit).

Tanda kredensial habis: `ExpiredToken` atau `explicit deny ... voc-cancel-cred` →
buka sesi lab baru dan ulangi langkah di atas. Resource yang sudah dibuat tetap ada.

## 3. Deploy CloudFormation stack (fondasi)

`infrastructure/starter_stack.yaml` membuat:

| Resource | Nama |
|---|---|
| DynamoDB | `udacity-agentcore-orders` (key: `customer_id` + `order_id`), `-customers`, `-workflow-state` |
| S3 bucket | `udacity-agentcore-policy-docs-<account>-<suffix>` (dokumen kebijakan) |
| S3 Vectors | vector bucket + 3 index (`returns/shipping/warranty-policy-index`, 1024 dimensi, cosine) |
| IAM role | `udacity-agentcore-agentcore-role` (execution role runtime) |
| CloudWatch | log group `/aws/bedrock/agentcore/udacity-agentcore` |

Deploy via CLI (atau Console → CloudFormation → Create stack → upload template,
nama stack `udacity-agentcore`, centang *acknowledge IAM*):

```bash
aws cloudformation deploy \
  --template-file infrastructure/starter_stack.yaml \
  --stack-name udacity-agentcore \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1

aws cloudformation describe-stacks --stack-name udacity-agentcore \
  --query "Stacks[0].StackStatus"          # tunggu CREATE_COMPLETE
```

> `CAPABILITY_NAMED_IAM` wajib karena template membuat IAM role dengan nama tetap.

## 4. Virtual environment Python

```powershell
py -3.12 -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt bedrock-agentcore
```

`bedrock-agentcore` dibutuhkan untuk mode `serve` (entry point HTTP di dalam runtime).

## 5. Seed data

```powershell
$env:PYTHONUTF8=1            # WAJIB di Windows, lihat troubleshooting
python infrastructure/seed_data.py
```

Hasilnya:
- 4 customer: CUST-001 Alice (Premium), CUST-002 Bob (Standard), CUST-003 Carol (Premium), CUST-004 David (Standard)
- 11 order dengan tanggal relatif terhadap hari ini (misalnya ORD-27176 = 12 hari lalu)
- 6 dokumen kebijakan di `s3://<policy bucket>/policies/{returns,shipping,warranty}/`

Seed bersifat **idempotent** — aman dijalankan ulang, dan berguna untuk me-reset
status order (misalnya setelah uji retur mengubah ORD-27176 jadi `return_initiated`).

## 6. File `.env`

```powershell
copy .env.example .env
python config.py
```

`config.py` membaca nama resource dari **CloudFormation exports** secara otomatis.
`.env` hanya berisi nilai yang baru ada setelah task tertentu selesai:

| Variabel | Diisi setelah |
|---|---|
| `RETURNS_KB_ID`, `SHIPPING_KB_ID`, `WARRANTY_KB_ID` | Task 5 (Knowledge Base) |
| `GUARDRAIL_ID`, `GUARDRAIL_VERSION` | Task 3 (guardrail) |
| `AGENTCORE_RUNTIME_ARN` | Task 3 (deploy) |

Tanda `(not yet created)` di output `config.py` sebelum task terkait selesai itu **normal**.

## 7. Jebakan khas Windows (yang benar-benar terjadi)

| Masalah | Gejala | Solusi |
|---|---|---|
| Encoding console cp1252 | `UnicodeEncodeError: 'charmap' codec can't encode character '✓'` | `$env:PYTHONUTF8=1` sebelum menjalankan Python |
| Jam Windows tidak sinkron | `SignatureDoesNotMatch: Signature expired` walau kredensial baru | Settings → Time & language → Date & time → *Set time automatically* + **Sync now** |
| Proses background tanpa stdin tertutup | `agentcore --version` menggantung selamanya saat dipanggil dari script | Jalankan dengan stdin tertutup (`< /dev/null` di bash) atau dari terminal interaktif biasa |
