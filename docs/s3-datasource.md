<!-- {% raw %} GitHub Pages: no Liquid in this page -->
# S3: the files of a bucket as a datasource

A datasource **«S3 folder»** reads the files of a folder of a bucket that match a glob,
stacks them into one table and keeps it as a snapshot, refreshed by hand or on a
schedule like every other imported datasource. It reads through an **S3 connection** —
the one the S3 Output uses: endpoint (empty = AWS), access key and secret, region,
default bucket — and so reaches any S3-compatible object storage: AWS, MinIO, Cloudflare
R2, Wasabi, Scaleway, Google Cloud Storage through its S3 API. It is the
[SFTP folder](sftp.md) on object storage; the other sources are listed in
[Sources](sources/README.md).

Like the other connections it lives in a folder and needs **CONNECT** on that folder to
be used; the secret is encrypted at rest (Fernet), travels encrypted to the worker and
is decrypted only to sign the requests. The credentials need **list** and **read**
rights on the bucket — nothing else: the datasource never writes or deletes there.

## Datasource «S3 folder»

From a folder: **New → Datasource from a database**, choose the S3 connection, then:

| Field | Meaning |
|---|---|
| Bucket | Empty = the connection's default bucket. |
| Folder | A key prefix taken as a folder: `exports/vendite` (no leading `/`; `in` never takes `inbox/`). Empty = the bucket's root. |
| Files | A name or a glob over the keys below the folder: `vendite_*.csv`; `*` and `?` within a name, `**` for any depth (`**/*.parquet`). Case-sensitive. `..` is refused. **Check the files** lists what it takes. |
| Format | *From each file's extension* (`.csv` `.txt` `.tsv`, `.parquet` `.pq`, `.xlsx` `.xlsm` `.xls`, `.ndjson` `.jsonl`), or CSV, Parquet, Excel, NDJSON for every file. |
| CSV separator, encoding, header; Excel sheet | As on the SFTP datasource. |
| Time zone, variables, initial last refresh | Folder and files may hold [placeholders](placeholders.md) and variables: `daily/{day}/sales.parquet` with `day = today-7 .. today step 1d`. |
| Refresh | **Snapshot**, or **append by key**: the rows whose key columns come again replace the old ones. |

The listing starts at the folder plus the glob's fixed leading folders (`2026/*.csv`
lists `folder/2026/`), so a narrow glob on a big bucket lists little. Keys whose name
starts with a dot and «folder» markers (keys ending in `/`) are left out.

Every object that matches is read at **every refresh** and the rows are stacked into
one table, united by column name. Every row says where it comes from: `_file` is the key
below the folder (`vendite_01.csv`, `2026/q1/a.csv`; with a dated folder `in/{day}`, the
key below the fixed part `in`), `_file_modified_at` the object's last modification time
(UTC) — the two last columns; `_param_<name>` the values of the variables. The files
must be **machine readable** (header in the first row, one name per column, no
repeats): anything else is refused, naming the file.

With placeholders or variables, a value whose files are missing is **skipped and
listed** on the run; no file at all across the values **fails** the refresh (see
[placeholders.md](placeholders.md#files-one-expansion-per-value)).

**Nothing outside the bucket and the folder.** Only the configured bucket is listed,
only keys the listing returned below the folder are read, and `..` is refused in the
folder, in the glob and in what their placeholders resolve to. No message — errors, the
run history, the file check, OpenLineage — holds the secret.

### Caps

Engine settings (backend and workers), checked on the **listing's sizes before
anything is downloaded**:

| Variable | Default | Meaning |
|---|---|---|
| `S3_SOURCE__MAX_FILE_BYTES` | 536870912 (512 MB) | Size of one object (downloaded to the worker's `/tmp`, read in memory). An object that grows past it while being read stops the import. |
| `S3_SOURCE__MAX_FILES` | 500 | Objects read by one refresh. |
| `S3_SOURCE__MAX_TOTAL_BYTES` | 2147483648 (2 GB) | All the matching objects together. |
| `S3_SOURCE__MAX_LISTED` | 100000 | Keys the globs of one refresh may scan below their fixed folders, all the values of its variables together: `**` on a huge bucket stops instead of listing it all. |
| `S3_SOURCE__MAX_REQUEST_SECONDS` | 600 | Wall-clock time of one object's download (each wait on the network is bounded by botocore's 60 s read timeout): a storage that drips the bytes stops the import. |

The import reports its progress (file *i* of *n*, rows read, saving); the database
datasource rules apply (snapshot swap at success, one refresh at a time, schedule,
data contract, no sort keys).

## Lineage, dbt

OpenLineage: an S3 refresh reads `s3://bucket` + `/folder/glob` (the templates as
written); an S3 Output whose key has placeholders is one dataset named after the
template, the key a run wrote being its version. The dbt export turns an S3 folder
into a **seed**, like the SFTP and SharePoint ones.

## Proofs

`backend/tests/test_s3_source.py` (against an in-memory S3 client); the gateway's part
in `gateway/tests/test_placeholders_gateway.py`; against a real MinIO and through the
dev stack, `e2e/placeholders/`.
<!-- {% endraw %} -->
