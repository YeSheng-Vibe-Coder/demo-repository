# IBM HI-Large in Zetaris

Loaded on 22 October 2026 into the Azure `zetaris-vm` instance.

## Data and verification

- Source: https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml
- Publisher project: https://github.com/IBM/AML-Data
- Release: version 8, `HI-Large_Trans.csv`, 17,052,760,651 bytes.
- License: CDLA-Sharing-1.0. This is synthetic financial data.
- Original CSV: `/home/azureuser/ibm-aml/HI-Large_Trans.csv` on the VM.
- SHA-256: `d13635e297c64673826217631fb88d635c4f506052e1bde833895eda2a65c3f2`.
- CSV, converted Parquet, and Zetaris each contain **179,702,229 transactions**.
- Ground-truth laundering label count: **225,546**, independently verified in Zetaris.
- Actual timestamps: 2022-08-01 00:00:00 through 2023-01-12 16:49:00. Do not silently discard the tail beyond the publisher's primary date period.
- Parquet: 30 files, 3,840,462,057 bytes, ZSTD compressed.
- Cloud disk expanded from 30 GiB to 128 GiB; VM remains 4 vCPU / 16 GiB.

## Query surfaces

`IBM_AML.hi_large_transactions` is the full typed table including the publisher's `is_laundering` label.
`aml.transactions` is the investigation view without that label, suitable for agents and rule development.
Do not treat a publisher label as evidence available to an investigator.

```sql
SELECT * FROM aml.transactions LIMIT 10;
```

```sql
SELECT COUNT(*) AS transaction_count
FROM IBM_AML.hi_large_transactions;
```

```sql
SELECT transaction_id, transaction_time, from_bank, from_account,
       to_bank, to_account, amount_paid, payment_currency, payment_format
FROM aml.transactions
WHERE from_bank = '020' AND from_account = '800104D70'
LIMIT 20;
```

Bank/account fields are strings and preserve leading zeroes. An account is identified by its bank plus account identifier.
Amounts use `DECIMAL(38,8)`; `transaction_time` is a timestamp.
`transaction_id` is the one-based row position in the original CSV, excluding the header.
Unordered `LIMIT` queries may return rows from any Parquet file; use `transaction_id` for a specific original row.

## Operations

From the Mac:

```sh
ssh -i <your-private-key> <user>@<your-vm-host>
```

On the VM:

```sh
cd ~/zetaris-platform
sudo docker compose ps
sudo ./verify.sh
```

The Parquet dataset is stored in the existing persistent `zetaris_customer-data` Docker volume at
`/home/zetaris/customer-data/shared/ibm/hi_large_parquet` inside the driver container.
Do not run `docker compose down -v`: it deletes this volume and Zetaris metadata.

Local client settings remain in `getting-started/.env.local` and require the SSH tunnel on ports 3000 and 8888.
No API keys or passwords are recorded in this document.

Implementation and audit files:

- `scripts/prepare_ibm_hi_large.py`
- `analysis/ibm_hi_large_register.sql`
- `sources/ibm_hi_large_manifest.json`
- `analysis/ibm_hi_large_probe.sql` (20-row connector validation table)
