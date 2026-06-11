import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from function.main import (
    handler,
    insert_items,
    load_json_array_from_s3,
    parse_request_paths,
    parse_s3_path,
    prepare_documents,
    process_s3_paths,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
S3_FIXTURES_DIR = FIXTURES_DIR / "s3_objects"


def _load_fixture(name: str):
    with (FIXTURES_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _load_s3_fixture(name: str):
    with (S3_FIXTURES_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _mock_dynamodb_table():
    table = MagicMock()
    table.name = "data"
    batch = MagicMock()
    table.batch_writer.return_value.__enter__.return_value = batch
    table.batch_writer.return_value.__exit__.return_value = None
    return table, batch


class TestParseS3Path:
    def test_parses_s3_uri(self):
        assert parse_s3_path("s3://my-bucket/path/to/file.json") == (
            "my-bucket",
            "path/to/file.json",
        )

    def test_parses_bucket_and_key(self):
        assert parse_s3_path("my-bucket/path/to/file.json") == (
            "my-bucket",
            "path/to/file.json",
        )

    def test_uses_default_bucket_for_key_only_path(self):
        assert parse_s3_path(
            "feeds/example/file.json",
            default_bucket="default-bucket",
        ) == ("default-bucket", "feeds/example/file.json")


class TestParseRequestPaths:
    def test_parses_api_gateway_put_body(self):
        event = {
            "body": json.dumps(
                [
                    "s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json",
                    "s3://news-archive-bucket/feeds/example/2026-06-01-batch-2.json",
                ]
            )
        }
        assert len(parse_request_paths(event)) == 2

    def test_parses_direct_lambda_payload(self):
        event = ["s3://bucket/a.json", "s3://bucket/b.json"]
        assert parse_request_paths(event) == event

    def test_rejects_empty_array(self):
        with pytest.raises(ValueError, match="At least one S3 path"):
            parse_request_paths({"body": "[]"})


class TestPrepareDocuments:
    def test_adds_source_to_each_document(self):
        items = _load_s3_fixture("articles_batch_1.json")
        source = "s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json"
        documents = prepare_documents(items, source)

        assert len(documents) == 2
        assert documents[0]["_source"] == source
        assert documents[1]["_source"] == source
        assert documents[0]["id"] == "article-001"


class TestLoadJsonArrayFromS3:
    def test_loads_json_array(self):
        payload = _load_s3_fixture("articles_batch_1.json")
        s3_client = MagicMock()
        s3_client.get_object.return_value = {
            "Body": BytesIO(json.dumps(payload).encode("utf-8"))
        }

        result = load_json_array_from_s3(
            s3_client,
            "news-archive-bucket",
            "feeds/example/2026-06-01-batch-1.json",
        )

        assert result == payload

    def test_rejects_non_array_payload(self):
        s3_client = MagicMock()
        s3_client.get_object.return_value = {
            "Body": BytesIO(json.dumps({"id": "not-an-array"}).encode("utf-8"))
        }

        with pytest.raises(ValueError, match="must contain a JSON array"):
            load_json_array_from_s3(s3_client, "bucket", "key.json")

    def test_maps_s3_client_error(self):
        s3_client = MagicMock()
        s3_client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )

        with pytest.raises(RuntimeError, match="S3 get_object failed"):
            load_json_array_from_s3(s3_client, "bucket", "missing.json")


class TestProcessS3Paths:
    def test_processes_multiple_paths_and_inserts_documents(self):
        batch_1 = _load_s3_fixture("articles_batch_1.json")
        batch_2 = _load_s3_fixture("articles_batch_2.json")
        expected_documents = _load_fixture("expected_documents.json")

        s3_client = MagicMock()

        def get_object_side_effect(*, Bucket, Key):
            if Key.endswith("batch-1.json"):
                body = json.dumps(batch_1).encode("utf-8")
            elif Key.endswith("batch-2.json"):
                body = json.dumps(batch_2).encode("utf-8")
            else:
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                    "GetObject",
                )
            return {"Body": BytesIO(body)}

        s3_client.get_object.side_effect = get_object_side_effect

        table, batch = _mock_dynamodb_table()
        inserted_items: list[dict] = []

        def put_item_side_effect(*, Item):
            inserted_items.append(Item)

        batch.put_item.side_effect = put_item_side_effect

        paths = [
            "s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json",
            "s3://news-archive-bucket/feeds/example/2026-06-01-batch-2.json",
        ]
        summary = process_s3_paths(
            paths,
            s3_client=s3_client,
            table=table,
            partition_key="id",
        )

        assert summary == {
            "pathsProcessed": 2,
            "documentsInserted": 3,
            "table": "data",
            "results": [
                {
                    "source": paths[0],
                    "items": 2,
                    "inserted": 2,
                },
                {
                    "source": paths[1],
                    "items": 1,
                    "inserted": 1,
                },
            ],
        }

        assert inserted_items == expected_documents


class TestInsertItems:
    def test_returns_zero_for_empty_list(self):
        table, batch = _mock_dynamodb_table()
        assert insert_items(table, [], "id") == 0
        table.batch_writer.assert_not_called()


class TestHandler:
    def test_returns_200_with_summary(self, monkeypatch):
        table, batch = _mock_dynamodb_table()
        monkeypatch.setattr("function.main._get_dynamodb_table", lambda settings: table)

        batch = _load_s3_fixture("articles_batch_1.json")
        s3_client = MagicMock()
        s3_client.get_object.return_value = {
            "Body": BytesIO(json.dumps(batch).encode("utf-8"))
        }

        def client_factory(service_name, region_name=None):
            if service_name == "s3":
                return s3_client
            raise AssertionError(f"Unexpected client: {service_name}")

        monkeypatch.setattr("function.main.boto3.client", client_factory)

        event = {
            "body": json.dumps(
                ["s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json"]
            )
        }
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["pathsProcessed"] == 1
        assert body["documentsInserted"] == 2
        assert body["table"] == "data"

    def test_returns_400_for_invalid_request(self):
        response = handler({"body": "{}"}, None)

        assert response["statusCode"] == 400
        assert "error" in json.loads(response["body"])
