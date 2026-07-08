import boto3
from botocore.config import Config
from app.core.config import get_settings


class StorageService:
    """
    Servizio per interagire con lo storage S3-compatible (Rclone/MinIO).
    
    Il bucket è passato come argomento ai metodi per maggiore flessibilità.
    """

    def __init__(self):
        settings = get_settings()
        
        self.settings = settings
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.storage.endpoint,
            aws_access_key_id=settings.storage.access_key,
            aws_secret_access_key=settings.storage.secret_key.get_secret_value(),
            region_name=settings.storage.region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},  # Importante per Rclone!
            ),
        )

    def create_bucket(self, bucket: str, location: str | None = None) -> dict:
        """
        Crea un bucket S3.
        
        Args:
            bucket: Nome del bucket da creare
            location: Region per il bucket (opzionale, usa default se None)
        
        Returns:
            Dict con informazioni sul bucket creato
        """
        try:
            # Verifica se esiste già
            self.client.head_bucket(Bucket=bucket)
            return {"bucket": bucket, "exists": True, "created": False}
        except self.client.exceptions.ClientError:
            # Bucket non esiste, crealo
            if location:
                self.client.create_bucket(
                    Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": location},
                )
            else:
                self.client.create_bucket(Bucket=bucket)
            return {"bucket": bucket, "exists": True, "created": True}

    def bucket_exists(self, bucket: str) -> bool:
        """
        Verifica se un bucket esiste.
        
        Args:
            bucket: Nome del bucket da verificare
        
        Returns:
            True se il bucket esiste, False altrimenti
        """
        try:
            self.client.head_bucket(Bucket=bucket)
            return True
        except self.client.exceptions.ClientError:
            return False

    def upload_file(self, file_path: str, bucket: str, object_key: str) -> dict:
        """
        Upload di un file.
        
        Args:
            file_path: Percorso locale del file
            bucket: Nome del bucket S3
            object_key: Chiave/oggetto nel bucket
        
        Returns:
            Dict con informazioni sull'upload
        """
        self.client.upload_file(file_path, bucket, object_key)
        return {"bucket": bucket, "key": object_key, "status": "uploaded"}

    def upload_fileobj(self, file_obj, bucket: str, object_key: str) -> dict:
        """
        Upload di un file-like object (es. BytesIO).
        
        Args:
            file_obj: File-like object da uploadare
            bucket: Nome del bucket S3
            object_key: Chiave/oggetto nel bucket
        
        Returns:
            Dict con informazioni sull'upload
        """
        self.client.upload_fileobj(file_obj, bucket, object_key)
        return {"bucket": bucket, "key": object_key, "status": "uploaded"}

    def download_file(self, bucket: str, object_key: str, file_path: str) -> dict:
        """
        Download di un file.
        
        Args:
            bucket: Nome del bucket S3
            object_key: Chiave/oggetto nel bucket
            file_path: Percorso locale dove salvare il file
        
        Returns:
            Dict con informazioni sul download
        """
        self.client.download_file(bucket, object_key, file_path)
        return {"bucket": bucket, "key": object_key, "status": "downloaded"}

    def download_fileobj(self, bucket: str, object_key: str, file_obj) -> dict:
        """
        Download di un file in un file-like object.
        
        Args:
            bucket: Nome del bucket S3
            object_key: Chiave/oggetto nel bucket
            file_obj: File-like object dove scrivere i dati
        
        Returns:
            Dict con informazioni sul download
        """
        self.client.download_fileobj(bucket, object_key, file_obj)
        return {"bucket": bucket, "key": object_key, "status": "downloaded"}

    def list_objects(self, bucket: str, prefix: str = "") -> list[dict]:
        """
        Lista oggetti nel bucket.
        
        Args:
            bucket: Nome del bucket S3
            prefix: Prefisso per filtrare gli oggetti (opzionale)
        
        Returns:
            Lista di dict con informazioni sugli oggetti
        """
        response = self.client.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
        )
        return response.get("Contents", [])

    def prefix_stats(self, bucket: str, prefix: str) -> tuple[int, int]:
        """
        Somma byte e conteggio oggetti sotto un prefisso (con paginazione, così
        conta oltre i 1000 oggetti). Ritorna (byte_totali, numero_oggetti).
        """
        paginator = self.client.get_paginator("list_objects_v2")
        total_bytes = 0
        count = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                total_bytes += obj["Size"]
                count += 1
        return total_bytes, count

    def delete_object(self, bucket: str, object_key: str) -> dict:
        """
        Elimina un oggetto.
        
        Args:
            bucket: Nome del bucket S3
            object_key: Chiave/oggetto da eliminare
        
        Returns:
            Dict con informazioni sull'eliminazione
        """
        self.client.delete_object(Bucket=bucket, Key=object_key)
        return {"bucket": bucket, "key": object_key, "status": "deleted"}

    def delete_objects(self, bucket: str, object_keys: list[str]) -> dict:
        """
        Elimina multiple oggetti in una volta.
        
        Args:
            bucket: Nome del bucket S3
            object_keys: Lista di chiavi/oggetti da eliminare
        
        Returns:
            Dict con informazioni sull'eliminazione
        """
        objects = [{"Key": key} for key in object_keys]
        response = self.client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": objects},
        )
        return {
            "bucket": bucket,
            "deleted": len(response.get("Deleted", [])),
            "errors": response.get("Errors", []),
        }

    def get_object(self, bucket: str, object_key: str) -> dict:
        """
        Ottieni un oggetto (metadati + contenuto).
        
        Args:
            bucket: Nome del bucket S3
            object_key: Chiave/oggetto da ottenere
        
        Returns:
            Dict con metadati e contenuto dell'oggetto
        """
        response = self.client.get_object(Bucket=bucket, Key=object_key)
        return {
            "bucket": bucket,
            "key": object_key,
            "content": response["Body"].read(),
            "metadata": {
                "content_type": response.get("ContentType"),
                "content_length": response.get("ContentLength"),
                "last_modified": response.get("LastModified"),
            },
        }

    def get_presigned_url(
        self, bucket: str, object_key: str, expiration: int = 3600, operation: str = "get_object"
    ) -> str:
        """
        Genera URL pre-firmato per download/upload.
        
        Args:
            bucket: Nome del bucket S3
            object_key: Chiave/oggetto
            expiration: Tempo di validità in secondi (default: 1 ora)
            operation: Operazione ("get_object" o "put_object")
        
        Returns:
            URL pre-firmato
        """
        return self.client.generate_presigned_url(
            operation,
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expiration,
        )

    def head_object(self, bucket: str, object_key: str) -> dict:
        """
        Ottieni metadati di un oggetto senza scaricarlo.
        
        Args:
            bucket: Nome del bucket S3
            object_key: Chiave/oggetto
        
        Returns:
            Dict con metadati dell'oggetto
        """
        response = self.client.head_object(Bucket=bucket, Key=object_key)
        return {
            "bucket": bucket,
            "key": object_key,
            "content_type": response.get("ContentType"),
            "content_length": response.get("ContentLength"),
            "last_modified": response.get("LastModified"),
            "etag": response.get("ETag"),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────
_storage_service: StorageService | None = None


def get_storage_service() -> StorageService:
    """
    Restituisce istanza singleton del servizio storage.
    
    Returns:
        Istanza di StorageService
    """
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service