import logging
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Endpoints where the batch delete (POST ?delete, up to 1000 keys) does NOT exist:
# Google Cloud Storage's XML API does not implement it, S3/MinIO/Scaleway do. Found at
# the first error and not tried again in that process.
_batch_delete_unsupported: set[str] = set()
_GONE = {"NoSuchKey", "404", "NotFound"}


def _is_gone(e: ClientError) -> bool:
    err = e.response.get("Error", {})
    return str(err.get("Code", "")) in _GONE or e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404


def delete_keys(client, bucket: str, keys: list[str]) -> tuple[int, list[dict]]:
    """Deletes several keys: in batches where available, one at a time where not (GCS).
    S3 semantics everywhere: deleting a key that does not exist is NOT an error (GCS
    answers 404 to a single DELETE, S3 does not). Returns (deleted, per-key errors in the
    batch's format: [{Key, Code, Message}])."""
    keys = [k for k in keys if k]
    if not keys:
        return 0, []
    endpoint = str(getattr(getattr(client, "meta", None), "endpoint_url", "") or "")
    deleted = 0
    errors: list[dict] = []
    start = 0
    if endpoint not in _batch_delete_unsupported:
        try:
            for i in range(0, len(keys), 1000):  # the S3 batch's limit
                resp = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]]}) or {}
                deleted += len(resp.get("Deleted", []))
                errors.extend(resp.get("Errors", []))
                start = i + 1000
            return deleted, errors
        except ClientError as e:
            # the batch itself did not go through (not a per-key error): from here on
            # this endpoint goes one key at a time
            _batch_delete_unsupported.add(endpoint)
            logger.info("batch delete not available on %s (%s): single DELETEs from now on",
                        endpoint or "?", e.response.get("Error", {}).get("Code", "?"))
    for k in keys[start:]:
        try:
            client.delete_object(Bucket=bucket, Key=k)
            deleted += 1
        except ClientError as e:
            if _is_gone(e):
                deleted += 1  # missing already: deleted as far as we are concerned
                continue
            err = e.response.get("Error", {})
            errors.append({"Key": k, "Code": str(err.get("Code", "")), "Message": str(err.get("Message", ""))})
            if len(errors) == 1 and k == keys[start]:
                # the single one fails at once too (AccessDenied): no use insisting on
                # hundreds of keys, the error is the same
                raise
    return deleted, errors


# Multipart uploads and copies: Scaleway's object storage takes at most 1,000 parts (AWS
# 10,000), and with boto3's default 8 MiB parts anything above 8 GiB was refused there —
# a one-billion-row import failed on its upload (load test of 2026-10-15). Parts grow
# with the object instead, so any size fits in fewer than 1,000 of them.
MAX_UPLOAD_PARTS = 900
MIN_PART_BYTES = 8 * 1024**2
# a stream whose size is unknown: parts large enough for 900 × 64 MiB ≈ 56 GiB
STREAM_PART_BYTES = 64 * 1024**2


def part_size(size_bytes: int | None) -> int:
    """The multipart part size for an object of this size: at least 8 MiB, in whole
    MiB, and never more than MAX_UPLOAD_PARTS parts."""
    if not size_bytes:
        return STREAM_PART_BYTES
    per_part = max(MIN_PART_BYTES, -(-size_bytes // MAX_UPLOAD_PARTS))
    return -(-per_part // 1024**2) * 1024**2


def transfer_config(size_bytes: int | None, **kw):
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(multipart_chunksize=part_size(size_bytes), **kw)


class StorageService:
    """
    A service to work with the S3-compatible storage (Rclone/MinIO).

    The bucket is passed to the methods as an argument, for flexibility.
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
                s3={"addressing_style": "path"},  # Important for Rclone!
                # boto3 >= 1.36 adds chunked CRC checksums (aws-chunked) to every PUT by
                # default: Google Cloud Storage refuses them with SignatureDoesNotMatch.
                # "when_required" = only where the API demands them; nothing changes on
                # AWS/MinIO/Scaleway.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def create_bucket(self, bucket: str, location: str | None = None) -> dict:
        """
        Creates an S3 bucket.

        Args:
            bucket: the name of the bucket to create
            location: the bucket's region (optional, the default when None)

        Returns:
            A dict describing the bucket created
        """
        try:
            # Whether it exists already
            self.client.head_bucket(Bucket=bucket)
            return {"bucket": bucket, "exists": True, "created": False}
        except self.client.exceptions.ClientError:
            # The bucket does not exist: create it
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
        Whether a bucket exists.

        Args:
            bucket: the name of the bucket to check

        Returns:
            True when the bucket exists, False otherwise
        """
        try:
            self.client.head_bucket(Bucket=bucket)
            return True
        except self.client.exceptions.ClientError:
            return False

    def upload_file(self, file_path: str, bucket: str, object_key: str) -> dict:
        """
        Uploads a file.

        Args:
            file_path: the file's local path
            bucket: the S3 bucket's name
            object_key: the key of the object in the bucket

        Returns:
            A dict describing the upload
        """
        self.client.upload_file(file_path, bucket, object_key, Config=transfer_config(os.path.getsize(file_path)))
        return {"bucket": bucket, "key": object_key, "status": "uploaded"}

    def upload_fileobj(self, file_obj, bucket: str, object_key: str) -> dict:
        """
        Uploads a file-like object (a BytesIO).

        Args:
            file_obj: the file-like object to upload
            bucket: the S3 bucket's name
            object_key: the key of the object in the bucket

        Returns:
            A dict describing the upload
        """
        size = None
        try:  # a file or a buffer says its size; a stream does not
            here = file_obj.tell()
            size = file_obj.seek(0, os.SEEK_END) - here
            file_obj.seek(here)
        except (AttributeError, OSError, ValueError):
            pass
        self.client.upload_fileobj(file_obj, bucket, object_key, Config=transfer_config(size))
        return {"bucket": bucket, "key": object_key, "status": "uploaded"}

    def download_file(self, bucket: str, object_key: str, file_path: str) -> dict:
        """
        Downloads a file.

        Args:
            bucket: the S3 bucket's name
            object_key: the key of the object in the bucket
            file_path: the local path to save the file to

        Returns:
            A dict describing the download
        """
        self.client.download_file(bucket, object_key, file_path)
        return {"bucket": bucket, "key": object_key, "status": "downloaded"}

    def download_fileobj(self, bucket: str, object_key: str, file_obj) -> dict:
        """
        Downloads a file into a file-like object.

        Args:
            bucket: the S3 bucket's name
            object_key: the key of the object in the bucket
            file_obj: the file-like object to write the data to

        Returns:
            A dict describing the download
        """
        self.client.download_fileobj(bucket, object_key, file_obj)
        return {"bucket": bucket, "key": object_key, "status": "downloaded"}

    def list_objects(self, bucket: str, prefix: str = "") -> list[dict]:
        """
        Lists the objects in the bucket.

        Args:
            bucket: the S3 bucket's name
            prefix: a prefix filtering the objects (optional)

        Returns:
            A list of dicts describing the objects
        """
        response = self.client.list_objects_v2(
            Bucket=bucket,
            Prefix=prefix,
        )
        return response.get("Contents", [])

    def prefix_stats(self, bucket: str, prefix: str) -> tuple[int, int]:
        """
        Total bytes and number of objects under a prefix (paginated, so it counts beyond
        1000 objects). Returns (total_bytes, object_count).
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
        Deletes an object.

        Args:
            bucket: the S3 bucket's name
            object_key: the key of the object to delete

        Returns:
            A dict describing the deletion
        """
        try:
            self.client.delete_object(Bucket=bucket, Key=object_key)
        except ClientError as e:
            if not _is_gone(e):  # GCS: 404 on a missing key; S3 not. Idempotent everywhere
                raise
        return {"bucket": bucket, "key": object_key, "status": "deleted"}

    def copy_object(self, bucket: str, source_key: str, target_key: str) -> None:
        """Copies an object within the bucket on the storage itself: nothing is
        downloaded. One CopyObject up to 5 GB (boto3's default would switch to a
        multipart copy above 8 MB, which not every S3-compatible storage serves);
        above, parts sized to stay under the 1,000-part limit."""
        size = self.client.head_object(Bucket=bucket, Key=source_key)["ContentLength"]
        self.client.copy({"Bucket": bucket, "Key": source_key}, bucket, target_key,
                         Config=transfer_config(size, multipart_threshold=5 * 1024**3))

    def delete_objects(self, bucket: str, object_keys: list[str]) -> dict:
        """
        Deletes several objects at once.

        Args:
            bucket: the S3 bucket's name
            object_keys: the keys of the objects to delete

        Returns:
            A dict describing the deletion
        """
        deleted, errors = delete_keys(self.client, bucket, list(object_keys))
        return {"bucket": bucket, "deleted": deleted, "errors": errors}

    def get_object(self, bucket: str, object_key: str) -> dict:
        """
        Gets an object (metadata + content).

        Args:
            bucket: the S3 bucket's name
            object_key: the key of the object to get

        Returns:
            A dict with the object's metadata and content
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
        Makes a presigned URL for a download or an upload.

        Args:
            bucket: the S3 bucket's name
            object_key: the object's key
            expiration: how long it is valid, in seconds (default: 1 hour)
            operation: the operation ("get_object" or "put_object")

        Returns:
            The presigned URL
        """
        return self.client.generate_presigned_url(
            operation,
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expiration,
        )

    def bucket_location(self, bucket: str) -> str:
        """The bucket's region as the storage reports it (GCS: 'europe-west8', 'EU'; S3:
        'eu-west-1'; MinIO: ''). Empty when it cannot be told."""
        try:
            loc = self.client.get_bucket_location(Bucket=bucket).get("LocationConstraint")
        except Exception:  # noqa: BLE001 — informative: the caller decides the fallback
            return ""
        return str(loc or "")

    def object_exists(self, bucket: str, object_key: str) -> bool:
        """True when the object exists (HEAD, no download). 404/NoSuchKey → False; any other
        error (credentials, network) is propagated."""
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=bucket, Key=object_key)
            return True
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("404", "NoSuchKey", "NotFound") or status == 404:
                return False
            raise

    def head_object(self, bucket: str, object_key: str) -> dict:
        """
        Gets an object's metadata without downloading it.

        Args:
            bucket: the S3 bucket's name
            object_key: the object's key

        Returns:
            A dict with the object's metadata
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
    Returns the storage service's singleton instance.

    Returns:
        The StorageService instance
    """
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service