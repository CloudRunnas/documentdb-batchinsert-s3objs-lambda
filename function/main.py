import json
import logging
import os
import re
import uuid
from decimal import Decimal
from typing import Any
from urllib.parse import unquote, urlparse

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_S3_URI_PATTERN = re.compile(r"^s3://([^/]+)/(.+)$")
_DEFAULT_DYNAMODB_TABLE_ARN = "arn:aws:dynamodb:eu-central-1:423623826655:table/data"
_DEFAULT_DYNAMODB_TABLE_NAME = "data"
_DEFAULT_LINK_GSI_NAME = "Link"
# Table primary key (live schema): HASH=_source, RANGE=link
_TABLE_HASH_KEY = "_source"
_TABLE_RANGE_KEY = "link"


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


def parse_request(event: dict[str, Any] | list[str]) -> tuple[list[str], dict[str, Any]]:
    """
    Extract S3 paths and optional additional_attrs values from the Lambda event.

    Supported shapes:
    - ["s3://..."]  (legacy)
    - {"paths": [...], "additional_attrs": ["_timestamp", "_date"], "_timestamp": "...", "_date": "..."}
    - API Gateway {"body": "<json>"} with either of the above
    """
    payload: Any = event
    if isinstance(event, dict) and "body" in event and "paths" not in event:
        body = event["body"]
        if body is None or (isinstance(body, str) and not body.strip()):
            raise ValueError("Request body is required")
        if isinstance(body, str):
            body = json.loads(body)
        payload = body

    additional: dict[str, Any] = {}

    if isinstance(payload, list):
        paths = payload
    elif isinstance(payload, dict):
        if "paths" in payload and isinstance(payload["paths"], list):
            paths = payload["paths"]
            attr_names = payload.get("additional_attrs") or []
            if not isinstance(attr_names, list):
                raise ValueError("additional_attrs must be a list of attribute names")
            for name in attr_names:
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("additional_attrs entries must be non-empty strings")
                # _source is always the S3 path of the feed JSON — never from payload
                if name == "_source":
                    continue
                if name not in payload:
                    raise ValueError(f"Missing value for additional attribute {name!r}")
                additional[name] = payload[name]
        else:
            raise ValueError(
                "Request must contain a JSON array of S3 paths or an object with 'paths'"
            )
    else:
        raise ValueError("Request must contain a JSON array of S3 paths")

    if not paths:
        raise ValueError("At least one S3 path is required")
    if not all(isinstance(path, str) and path.strip() for path in paths):
        raise ValueError("All S3 paths must be non-empty strings")
    return paths, additional


def parse_request_paths(event: dict[str, Any] | list[str]) -> list[str]:
    """Backward-compatible helper: return only the S3 paths."""
    paths, _ = parse_request(event)
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


def normalize_link(link: Any) -> str | None:
    """
    Strip scheme and leading www. so the value starts with the FQDN.
    Example: https://www.zeit.de/foo → zeit.de/foo
    """
    if link is None:
        return None
    if not isinstance(link, str):
        return None
    value = link.strip()
    if not value:
        return None

    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or ""
    if parsed.params:
        path = f"{path};{parsed.params}"
    query = f"?{parsed.query}" if parsed.query else ""
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    if not host:
        # Relative or malformed — strip scheme-like prefix manually
        stripped = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
        stripped = re.sub(r"^www\.", "", stripped, flags=re.IGNORECASE)
        return stripped or None
    return f"{host}{path}{query}{fragment}"


def prepare_documents(
    items: list[dict[str, Any]],
    source_path: str,
    additional_attrs: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Attach _source (S3 path), normalize link, apply additional_attrs, ensure id."""
    extra = additional_attrs or {}
    documents: list[dict[str, Any]] = []
    for item in items:
        document = dict(item)
        document["_source"] = source_path
        if "link" in document:
            normalized = normalize_link(document["link"])
            if normalized is not None:
                document["link"] = normalized
        for key, value in extra.items():
            if key == "_source":
                continue
            document[key] = value
        if "id" not in document or document["id"] in (None, ""):
            document["id"] = str(uuid.uuid4())
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


def link_already_exists(table: Any, link: str, index_name: str) -> bool:
    """Return True if any item with this link exists in the Link GSI."""
    from boto3.dynamodb.conditions import Key  # noqa: PLC0415

    response = table.query(
        IndexName=index_name,
        KeyConditionExpression=Key("link").eq(link),
        Limit=1,
        Select="COUNT",
    )
    return int(response.get("Count", 0)) > 0


def insert_items(
    table: Any,
    documents: list[dict[str, Any]],
    *,
    link_gsi_name: str = _DEFAULT_LINK_GSI_NAME,
) -> tuple[int, int]:
    """
    Insert documents into DynamoDB.

    Returns (inserted_count, skipped_duplicates_count).
    Skips items whose normalized `link` already exists in the Link GSI.
    """
    if not documents:
        return 0, 0

    inserted = 0
    skipped = 0
    with table.batch_writer() as batch:
        for document in documents:
            link = document.get("link")
            if not isinstance(link, str) or not link.strip():
                logger.warning("Skipping document without link: id=%s", document.get("id"))
                skipped += 1
                continue

            try:
                if link_already_exists(table, link, link_gsi_name):
                    skipped += 1
                    continue
            except ClientError:
                logger.exception("Link GSI query failed for link=%s", link)
                raise

            item = _convert_for_dynamodb(document)
            if _TABLE_HASH_KEY not in item or _TABLE_RANGE_KEY not in item:
                logger.warning(
                    "Skipping document missing table keys %s/%s",
                    _TABLE_HASH_KEY,
                    _TABLE_RANGE_KEY,
                )
                skipped += 1
                continue
            batch.put_item(Item=item)
            inserted += 1
    return inserted, skipped


def process_s3_paths(
    paths: list[str],
    *,
    s3_client: Any,
    table: Any,
    default_bucket: str | None = None,
    additional_attrs: dict[str, Any] | None = None,
    link_gsi_name: str | None = None,
) -> dict[str, Any]:
    """Read all S3 objects and insert their array items into DynamoDB."""
    gsi = link_gsi_name or os.environ.get("DYNAMODB_LINK_GSI", _DEFAULT_LINK_GSI_NAME)
    per_path_results: list[dict[str, Any]] = []
    total_documents = 0
    total_skipped = 0

    for source_path in paths:
        bucket, key = parse_s3_path(source_path, default_bucket=default_bucket)
        canonical_source = f"s3://{bucket}/{key}"
        items = load_json_array_from_s3(s3_client, bucket, key)
        documents = prepare_documents(
            items,
            canonical_source,
            additional_attrs=additional_attrs,
        )
        inserted, skipped = insert_items(table, documents, link_gsi_name=gsi)
        total_documents += inserted
        total_skipped += skipped
        per_path_results.append(
            {
                "source": canonical_source,
                "items": len(items),
                "inserted": inserted,
                "skippedDuplicates": skipped,
            }
        )

    return {
        "pathsProcessed": len(paths),
        "documentsInserted": total_documents,
        "duplicatesSkipped": total_skipped,
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

    return {
        "table_name": table_name,
        "region": _aws_region(),
        "link_gsi": os.environ.get("DYNAMODB_LINK_GSI", _DEFAULT_LINK_GSI_NAME).strip()
        or _DEFAULT_LINK_GSI_NAME,
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
        paths, additional_attrs = parse_request(event)
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
            default_bucket=_default_bucket_name(),
            additional_attrs=additional_attrs,
            link_gsi_name=settings["link_gsi"],
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
