# Task 5 — Membuat 3 Bedrock Knowledge Base (manual, AWS Console)

> Bagian ini **tidak ada kodenya** — dikerjakan di AWS Console. Hasil akhirnya
> adalah 3 Knowledge Base ID yang ditaruh di `.env`.

## Konsep singkat

**Knowledge Base (KB)** = layanan RAG terkelola dari Bedrock. Kita tunjuk sebuah
folder (prefix) di S3, lalu Bedrock otomatis:

1. **Parsing & chunking** — memecah dokumen jadi potongan kecil (chunk).
2. **Embedding** — mengubah tiap chunk jadi vektor angka dengan model
   *Titan Text Embeddings V2* (1024 dimensi).
3. **Indexing** — menyimpan vektor ke **S3 Vectors** (vector store serverless, murah,
   tanpa cluster OpenSearch).

Saat agent bertanya, `bedrock-agent-runtime.retrieve()` mengubah pertanyaan jadi vektor,
mencari chunk yang paling mirip (cosine similarity), dan mengembalikan top-k passage.

Kenapa **3 KB terpisah**, bukan satu? Karena PolicyAgent memakai pola *multi-agent RAG*:
3 retriever sub-agent (Returns, Shipping, Warranty) masing-masing punya KB sendiri dan
berjalan **paralel**. Tiap domain jadi terisolasi — hasil pencarian "garansi" tidak
tercampur dengan dokumen pengiriman.

Semua bahan sudah dibuat oleh CloudFormation stack + `seed_data.py`:

| Bahan | Nilai (cek dengan `python config.py`) |
|---|---|
| S3 bucket dokumen | `udacity-agentcore-policy-docs-<ACCOUNT_ID>-<suffix>` |
| Vector bucket (S3 Vectors) | `udacity-agentcore-vectors-<ACCOUNT_ID>-<suffix>` |
| Vector index | `returns-policy-index`, `shipping-policy-index`, `warranty-policy-index` (1024 dimensi, cosine, float32) |

## Langkah 0 — Cek dokumen di S3

```bash
python config.py        # catat "Policy Bucket" dan "Vector Bucket"
aws s3 ls s3://<Policy Bucket>/policies/ --recursive
```

Harus ada 6 file: `return_policy.txt`, `shipping_policy.txt`, `warranty_policy.txt`,
dan `customer_tiers.txt` di ketiga folder.

## Langkah 1 — Buat Returns KB

1. Buka **AWS Console** → pastikan region **N. Virginia (us-east-1)** (pojok kanan atas).
2. Cari **Amazon Bedrock** → menu kiri **Build → Knowledge Bases**.
3. Klik **Create** → pilih **Knowledge Base with vector store**.
   > ⚠️ Jangan pilih *Managed Knowledge Base* — itu membuat vector bucket baru
   > yang berbeda dari milik stack, dan backing store-nya tidak bisa diganti.
4. **Step 1 – Provide Knowledge Base details**
   - Knowledge Base name: `novamart-returns-policy-kb`
   - IAM permissions: **Create and use a new service role** (biarkan nama default)
   - Data source: **Amazon S3**
   - Klik **Next**
5. **Step 2 – Configure data source**
   - Data source name: biarkan default (atau `returns-docs`)
   - S3 URI: klik **Browse S3** → pilih Policy Bucket → masuk `policies/` → pilih folder
     `returns/` → **Choose**. Hasilnya: `s3://<Policy Bucket>/policies/returns/`
   - Parsing & chunking: biarkan **Default**
   - Klik **Next**
6. **Step 3 – Configure data storage and processing**
   - Embeddings model: klik **Select model** → **Amazon → Titan Text Embeddings V2** → Apply
     (Embedding type: *Floating-point vector embeddings*, dimensions: **1024**)
   - Vector store creation method: **Use an existing vector store**
   - Vector store: **Amazon S3 Vectors**
   - S3 vector bucket ARN: pilih `udacity-agentcore-vectors-…`
   - S3 vector index ARN: pilih **`returns-policy-index`**
   - Klik **Next**
7. **Step 4 – Review and create** → **Create Knowledge Base**. Tunggu ±1 menit sampai status *Available*.
8. Di halaman KB, bagian **Data source** → centang data source → klik **Sync**.
   Tunggu status sync **Available / Completed** (±1 menit).
9. Salin **Knowledge Base ID** (10 karakter, contoh `ABCD1234EF`) dari bagian *Knowledge Base overview*.

## Langkah 2 & 3 — Shipping KB dan Warranty KB

Ulangi Langkah 1 dengan perbedaan berikut (yang lain sama persis):

| Setting | Shipping KB | Warranty KB |
|---|---|---|
| Name | `novamart-shipping-policy-kb` | `novamart-warranty-policy-kb` |
| S3 URI | `s3://<Policy Bucket>/policies/shipping/` | `s3://<Policy Bucket>/policies/warranty/` |
| Vector index | `shipping-policy-index` | `warranty-policy-index` |

Jangan lupa **Sync** masing-masing.

## Langkah 4 — Isi `.env`

```
RETURNS_KB_ID=<ID returns>
SHIPPING_KB_ID=<ID shipping>
WARRANTY_KB_ID=<ID warranty>
```

## Langkah 5 — Verifikasi

```bash
python config.py                   # 3 KB ID sudah terisi
python tests/test_agent.py task5   # target 20/20
```

Opsional, uji langsung di Console: buka KB → tombol **Test** → pilih model apa saja →
tanya *"How long is the return window for Premium customers?"* → harus menjawab 60 hari
dengan sumber `return_policy.txt` / `customer_tiers.txt`.

## Troubleshooting

| Gejala | Penyebab / solusi |
|---|---|
| Vector index tidak muncul di dropdown | Region salah (harus us-east-1), atau stack belum `CREATE_COMPLETE` |
| Error dimensi saat create | Embeddings model bukan Titan V2 1024 dimensi — index stack dibuat 1024 |
| `retrieve()` mengembalikan kosong | Belum klik **Sync**, atau ID di `.env` tertukar |
| test task5 bilang "no completed ingestion job" | Sync belum selesai / gagal — lihat tab *Sync history* |
| Sudah deploy runtime sebelum KB dibuat | Jalankan ulang `python src/agent_orchestrator.py deploy` — mengubah `.env` saja tidak mengubah runtime yang sudah ter-deploy |
