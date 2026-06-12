import json
import logging
import os
import re
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_S3_URI_PATTERN = re.compile(r"^s3://([^/]+)/(.+)$")
_DEFAULT_DYNAMODB_TABLE_ARN = "arn:aws:dynamodb:eu-central-1:423623826655:table/data"
_DEFAULT_DYNAMODB_TABLE_NAME = "data"
_DEFAULT_DYNAMODB_PARTITION_KEY = "id"


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


def _table_name_from_arn(arn: str) -> str | None:
    parts = arn.split(":")
    if len(parts) < 6 or parts[2] != "dynamodb" or not parts[5].startswith("table/"):
        return None
    return parts[5].split("/", 1)[1]


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


def _convert_for_dynamodb(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _convert_for_dynamodb(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_convert_for_dynamodb(item) for item in value]
    return value


def _ensure_partition_key(document: dict[str, Any], partition_key: str) -> dict[str, Any]:
    item = dict(document)
    if partition_key not in item or item[partition_key] in (None, ""):
        item[partition_key] = str(uuid.uuid4())
    return item


def insert_items(
    table: Any,
    documents: list[dict[str, Any]],
    partition_key: str,
) -> int:
    if not documents:
        return 0

    inserted = 0
    with table.batch_writer() as batch:
        for document in documents:
            item = _convert_for_dynamodb(
                _ensure_partition_key(document, partition_key)
            )
            batch.put_item(Item=item)
            inserted += 1
    return inserted


def process_s3_paths(
    paths: list[str],
    *,
    s3_client: Any,
    table: Any,
    partition_key: str,
    default_bucket: str | None = None,
) -> dict[str, Any]:
    """Read all S3 objects and insert their array items into DynamoDB."""
    per_path_results: list[dict[str, Any]] = []
    total_documents = 0

    for source_path in paths:
        bucket, key = parse_s3_path(source_path, default_bucket=default_bucket)
        canonical_source = f"s3://{bucket}/{key}"
        items = load_json_array_from_s3(s3_client, bucket, key)
        documents = prepare_documents(items, canonical_source)
        inserted = insert_items(table, documents, partition_key)
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
        "table": table.name,
        "results": per_path_results,
    }


def _dynamodb_settings() -> dict[str, str]:
    table_name = os.environ.get("DYNAMODB_TABLE", "").strip()
    if not table_name:
        table_arn = os.environ.get(
            "DYNAMODB_TABLE_ARN",
            _DEFAULT_DYNAMODB_TABLE_ARN,
        ).strip()
        table_name = _table_name_from_arn(table_arn) or _DEFAULT_DYNAMODB_TABLE_NAME

    partition_key = (
        os.environ.get("DYNAMODB_PARTITION_KEY", _DEFAULT_DYNAMODB_PARTITION_KEY).strip()
        or _DEFAULT_DYNAMODB_PARTITION_KEY
    )
    return {
        "table_name": table_name,
        "partition_key": partition_key,
        "region": _aws_region(),
    }


def _get_dynamodb_table(settings: dict[str, str]) -> Any:
    resource = boto3.resource("dynamodb", region_name=settings["region"])
    return resource.Table(settings["table_name"])


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
        settings = _dynamodb_settings()
        table = _get_dynamodb_table(settings)
        s3_client = boto3.client("s3", region_name=settings["region"])
        summary = process_s3_paths(
            paths,
            s3_client=s3_client,
            table=table,
            partition_key=settings["partition_key"],
            default_bucket=_default_bucket_name(),
        )
    except (RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        return _http_response(400, {"error": str(exc)})
    except ClientError as exc:
        logger.exception("DynamoDB write failed")
        message = exc.response["Error"].get("Message", str(exc))
        return _http_response(500, {"error": f"DynamoDB error: {message}"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error while processing S3 paths")
        return _http_response(500, {"error": str(exc)})

    return _http_response(200, summary)
