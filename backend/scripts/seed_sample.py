"""
Seed di dati di esempio per provare l'API.

Crea il bucket configurato e ci carica un parquet `sample/vendite.parquet`,
così puoi subito chiamare /tasks/preview e /tasks/transform-data.

Uso (dalla cartella backend/, con rclone raggiungibile):
    python scripts/seed_sample.py
"""
import io

import polars as pl

from app.core.config import get_settings
from app.utils import get_storage_service

SAMPLE_KEY = "sample/vendite.parquet"


def main() -> None:
    settings = get_settings()
    storage = get_storage_service()
    bucket = settings.storage.bucket

    print(f"Storage endpoint: {settings.storage.endpoint}")
    print(f"Bucket: {bucket}")

    info = storage.create_bucket(bucket)
    print(f"Bucket: {'creato' if info.get('created') else 'gia esistente'}")

    df = pl.DataFrame(
        {
            "id": [1, 2, 3, 4, 5, 6],
            "paese": ["IT", "IT", "FR", "FR", "DE", "DE"],
            "prodotto": ["A", "B", "A", "B", "A", "B"],
            "vendite": [100, 200, 150, None, 300, 250],
        }
    )

    buf = io.BytesIO()
    df.write_parquet(buf)
    buf.seek(0)
    storage.upload_fileobj(buf, bucket, SAMPLE_KEY)

    print(f"Caricato {SAMPLE_KEY} ({df.height} righe, colonne: {df.columns})")
    print("\nOra puoi provare, es.:")
    print(f'  curl -s localhost:8000/tasks/operations')
    print(
        "  curl -s -X POST localhost:8000/tasks/preview -H 'Content-Type: application/json' \\\n"
        f"    -d '{{\"bucket\":\"{bucket}\",\"input_key\":\"{SAMPLE_KEY}\",\"operations\":[]}}'"
    )


if __name__ == "__main__":
    main()
