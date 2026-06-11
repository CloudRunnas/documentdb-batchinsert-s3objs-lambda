import json
import logging
import os
import re
from typing import Any
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_S3_URI_PATTERN = re.compile(r"^s3://([^/]+)/(.+)$")
_DEFAULT_TLS_CA_FILE = "/var/task/global-bundle.pem"


def _aws_region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "eu-central-1"
    )


def _default_bucket_name() -> str | None:
    for key in ("S3_BUCKET", "DEFAULT_S3_BUCKET"):
        value = os.environ.get(key)
        if value:
            return value.strip()
    return None


def parse_s3_path(path: str, default_bucket: str | None = None) -> tuple[str, str]:
    """Parse an S3 path into bucket and object key."""
    normalized = path.strip()
    if not normalized:
        raise ValueError("S3 path must not be empty")

    match = _S3_URI_PATTERN.match(normalized)
    if match:
        bucket = match.group(1)
        key = unquote(match.group(2))
        if not bucket or not key:
            raise ValueError(f"Invalid S3 URI: {path!r}")
        return bucket, key

    if "/" not in normalized:
        raise ValueError(
            f"Invalid S3 path {path!r}: expected s3://bucket/key or bucket/key"
        )

    bucket, key = normalized.split("/", 1)
    if not bucket or not key:
        raise ValueError(f"Invalid S3 path: {path!r}")

    if bucket.count(".") == 0 and default_bucket:
        return default_bucket, normalized

    return bucket, key


def parse_request_paths(event: dict[str, Any] | list[str]) -> list[str]:
    """Extract the list of S3 paths from a Lambda/API Gateway event."""
    if isinstance(event, list):
        paths = event
    elif isinstance(event, dict):
        if "paths" in event and isinstance(event["paths"], list):
            paths = event["paths"]
        elif "body" in event:
            body = event["body"]
            if body is None:
                raise ValueError("Request body is required")
            if isinstance(body, str):
                if not body.strip():
                    raise ValueError("Request body is required")
                body = json.loads(body)
            if not isinstance(body, list):
                raise ValueError("Request body must be a JSON array of S3 paths")
            paths = body
        else:
            raise ValueError("Request must contain a JSON array of S3 paths")
    else:
        raise ValueError("Request must contain a JSON array of S3 paths")

    if not paths:
        raise ValueError("At least one S3 path is required")
    if not all(isinstance(path, str) and path.strip() for path in paths):
        raise ValueError("All S3 paths must be non-empty strings")
    return paths


def load_json_array_from_s3(
    s3_client: Any,
    bucket: str,
    key: str,
) -> list[dict[str, Any]]:
    """Download and parse a JSON file that contains an array of objects."""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"].get("Message", code)
        raise RuntimeError(f"S3 get_object failed for s3://{bucket}/{key}: {message}") from exc

    raw_body = response["Body"].read()
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON in s3://{bucket}/{key}") from exc

    if not isinstance(payload, list):
        raise ValueError(f"S3 object s3://{bucket}/{key} must contain a JSON array")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError(
            f"S3 object s3://{bucket}/{key} must contain an array of JSON objects"
        )
    return payload


def prepare_documents(
    items: list[dict[str, Any]],
    source_path: str,
) -> list[dict[str, Any]]:
    """Attach the originating S3 path to every document."""
    documents: list[dict[str, Any]] = []
    for item in items:
        document = dict(item)
        document["_source"] = source_path
        documents.append(document)
    return documents


def insert_documents(collection: Collection, documents: list[dict[str, Any]]) -> int:
    if not documents:
        return 0
    result = collection.insert_many(documents)
    return len(result.inserted_ids)


def process_s3_paths(
    paths: list[str],
    *,
    s3_client: Any,
    collection: Collection,
    default_bucket: str | None = None,
) -> dict[str, Any]:
    """Read all S3 objects and insert their array items into DocumentDB."""
    per_path_results: list[dict[str, Any]] = []
    total_documents = 0

    for source_path in paths:
        bucket, key = parse_s3_path(source_path, default_bucket=default_bucket)
        canonical_source = f"s3://{bucket}/{key}"
        items = load_json_array_from_s3(s3_client, bucket, key)
        documents = prepare_documents(items, canonical_source)
        inserted = insert_documents(collection, documents)
        total_documents += inserted
        per_path_results.append(
            {
                "source": canonical_source,
                "items": len(items),
                "inserted": inserted,
            }
        )

    return {
        "pathsProcessed": len(paths),
        "documentsInserted": total_documents,
        "results": per_path_results,
    }


def _documentdb_settings() -> dict[str, str]:
    uri = os.environ.get("DOCUMENTDB_URI", "").strip()
    database = os.environ.get("DOCUMENTDB_DATABASE", "").strip()
    collection = os.environ.get("DOCUMENTDB_COLLECTION", "").strip()
    missing = [
        name
        for name, value in (
            ("DOCUMENTDB_URI", uri),
            ("DOCUMENTDB_DATABASE", database),
            ("DOCUMENTDB_COLLECTION", collection),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )
    return {
        "uri": uri,
        "database": database,
        "collection": collection,
        "tls_ca_file": os.environ.get("DOCUMENTDB_TLS_CA_FILE", _DEFAULT_TLS_CA_FILE),
    }


def _create_documentdb_collection(settings: dict[str, str]) -> Collection:
    client = MongoClient(
        settings["uri"],
        tls=True,
        tlsCAFile=settings["tls_ca_file"],
        retryWrites=False,
    )
    return client[settings["database"]][settings["collection"]]


def _http_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def handler(event: dict[str, Any] | list[str], context: Any) -> dict[str, Any]:
    try:
        paths = parse_request_paths(event)
    except ValueError as exc:
        logger.warning("Invalid request: %s", exc)
        return _http_response(400, {"error": str(exc)})

    try:
        settings = _documentdb_settings()
        collection = _create_documentdb_collection(settings)
        s3_client = boto3.client("s3", region_name=_aws_region())
        summary = process_s3_paths(
            paths,
            s3_client=s3_client,
            collection=collection,
            default_bucket=_default_bucket_name(),
        )
    except (RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return _http_response(400, {"error": str(exc)})
    except PyMongoError as exc:
        logger.exception("DocumentDB insert failed")
        return _http_response(500, {"error": f"DocumentDB error: {exc}"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error while processing S3 paths")
        return _http_response(500, {"error": str(exc)})

    return _http_response(200, summary)
