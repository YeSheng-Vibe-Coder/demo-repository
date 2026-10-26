#!/bin/sh
# Owner-side step 1 of the local data pack: export the live-investigation slice from the Zetaris VM.
#
#   AML_VM_SSH="ssh -i ~/.ssh/<key>.pem <user>@<vm-host>" scripts/export_local_pack.sh
#
# Writes .runtime/local-data/raw/: the in-scope transactions of 2022-08-05..2022-09-25 (ACH/Wire, no self
# transfers, no label column) and the feature shards of those days, exactly as Zetaris stores them.
# Step 2 is `python -m aml_agent.cli local-pack build`.
set -u
: "${AML_VM_SSH:?set AML_VM_SSH to the ssh command for the VM}"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
RAW="$ROOT/.runtime/local-data/raw"
VOLUME=/var/lib/docker/volumes/zetaris_customer-data/_data/shared/ibm
WORK=/tmp/aml-export
DAYS=$(python3 -c "
from datetime import date, timedelta
d = date(2022, 8, 5)
while d < date(2022, 9, 26):
    print(f'day={d}'); d += timedelta(days=1)")
mkdir -p "$RAW" && cd "$RAW" || exit 1

# 1. Transactions, filtered on the VM with DuckDB's CLI (downloaded there once) and read straight from the Parquet source.
$AML_VM_SSH "mkdir -p $WORK && cd $WORK && { [ -x duckdb ] || { curl -sL -o duckdb.zip https://github.com/duckdb/duckdb/releases/download/v1.3.2/duckdb_cli-linux-amd64.zip && unzip -oq duckdb.zip; }; } && sudo ./duckdb -c \"COPY (SELECT transaction_id, transaction_time, from_bank, from_account, to_bank, to_account, amount_received, receiving_currency, amount_paid, payment_currency, payment_format FROM read_parquet('$VOLUME/hi_large_parquet/*.parquet') WHERE transaction_time >= TIMESTAMP '2022-08-05' AND transaction_time < TIMESTAMP '2022-09-26' AND payment_format IN ('ACH','Wire') AND (from_bank <> to_bank OR from_account <> to_account) ORDER BY transaction_time) TO 'transactions.parquet' (FORMAT parquet, COMPRESSION zstd)\"" || exit 1
$AML_VM_SSH "cat $WORK/transactions.parquet" > transactions.parquet || exit 1
echo "transactions $(du -h transactions.parquet | cut -f1)"

# 2. Feature shards as stored. Days without a shard (the gap between development and validation) are skipped by tar.
for shard in day_aggregates_v1 day_transit_v1 day_history_v1; do
  mkdir -p "$shard"
  $AML_VM_SSH "sudo tar -C $VOLUME/aml_agent/$shard -cf - $(echo $DAYS) 2>/dev/null" | tar -xf - -C "$shard" 2>/dev/null
  echo "$shard $(ls "$shard" | wc -l | tr -d ' ') days, $(du -sh "$shard" | cut -f1)"
done
