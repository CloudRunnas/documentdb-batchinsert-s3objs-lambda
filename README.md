# documentdb-batchinsert-s3objs-lambda

AWS-Lambda-Funktion, die per **PUT**-Request ein JSON-Array von S3-Pfaden entgegennimmt, die referenzierten JSON-Dateien aus S3 liest und alle enthaltenen Objekte als Items in **Amazon DynamoDB** speichert.

Jedes eingefügte Item erhält ein zusätzliches Feld `_source` mit dem kanonischen S3-Pfad der Quelldatei.

Standard-Zieltabelle: `arn:aws:dynamodb:eu-central-1:423623826655:table/data`

## Verhalten (Spezifikation)

### Eingabe

Die Lambda wird über API Gateway oder direktes Invoke mit einem JSON-Array von S3-Pfaden aufgerufen.

**HTTP PUT (API Gateway):**

```http
PUT /batch-insert
Content-Type: application/json

[
  "s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json",
  "s3://news-archive-bucket/feeds/example/2026-06-01-batch-2.json"
]
```

**Direktes Lambda-Invoke:**

```json
[
  "s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json",
  "s3://news-archive-bucket/feeds/example/2026-06-01-batch-2.json"
]
```

Alternativ ist auch das Format `bucket/key` möglich. Reine Objekt-Keys (`feeds/example/file.json`) werden nur verwendet, wenn die Umgebungsvariable `S3_BUCKET` gesetzt ist.

### S3-Dateiformat

Jede referenzierte S3-Datei muss gültiges JSON enthalten, das ein **Array von Objekten** ist:

`tests/fixtures/s3_objects/articles_batch_1.json`

```json
[
  {
    "id": "article-001",
    "title": "Erster Artikel",
    "publishedAt": "2026-06-01T10:00:00Z"
  },
  {
    "id": "article-002",
    "title": "Zweiter Artikel",
    "publishedAt": "2026-06-01T11:00:00Z"
  }
]
```

### Verarbeitung

Für jeden S3-Pfad:

1. Datei aus S3 laden
2. JSON parsen und als Array validieren
3. Für jedes Array-Element ein DynamoDB-Item erzeugen
4. `_source` mit dem kanonischen Pfad `s3://bucket/key` setzen
5. Alle Items per `batch_writer` in die konfigurierte DynamoDB-Tabelle schreiben

Aus zwei S3-Dateien mit insgesamt drei Objekten entstehen **drei separate DynamoDB-Items**.

### Partition Key

Standardmäßig wird `id` als Partition Key verwendet. Fehlt `id` in einem Objekt, wird automatisch eine UUID gesetzt.

Die Tabelle `data` sollte daher mindestens einen String-Partition-Key `id` haben. Falls deine Tabelle einen anderen Key-Namen nutzt, setze `DYNAMODB_PARTITION_KEY` entsprechend.

### Ausgabe (erfolgreich)

```json
{
  "pathsProcessed": 2,
  "documentsInserted": 3,
  "table": "data",
  "results": [
    {
      "source": "s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json",
      "items": 2,
      "inserted": 2
    },
    {
      "source": "s3://news-archive-bucket/feeds/example/2026-06-01-batch-2.json",
      "items": 1,
      "inserted": 1
    }
  ]
}
```

### Erwartete DynamoDB-Items

Die vollständige erwartete Ausgabe für die Beispiel-Fixtures liegt in `tests/fixtures/expected_documents.json`:

```json
[
  {
    "id": "article-001",
    "title": "Erster Artikel",
    "publishedAt": "2026-06-01T10:00:00Z",
    "_source": "s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json"
  },
  {
    "id": "article-002",
    "title": "Zweiter Artikel",
    "publishedAt": "2026-06-01T11:00:00Z",
    "_source": "s3://news-archive-bucket/feeds/example/2026-06-01-batch-1.json"
  },
  {
    "id": "article-101",
    "title": "Dritter Artikel",
    "category": "politics",
    "_source": "s3://news-archive-bucket/feeds/example/2026-06-01-batch-2.json"
  }
]
```

### Fehlerfälle

| Situation | HTTP-Status | Beispielantwort |
|-----------|-------------|-----------------|
| Leerer oder ungültiger Request-Body | 400 | `{"error": "Request body must be a JSON array of S3 paths"}` |
| S3-Objekt nicht gefunden | 400 | `{"error": "S3 get_object failed for s3://..."}` |
| JSON ist kein Array | 400 | `{"error": "S3 object s3://... must contain a JSON array"}` |
| DynamoDB-Fehler | 500 | `{"error": "DynamoDB error: ..."}` |

## Umgebungsvariablen

| Variable | Pflicht | Default | Beschreibung |
|----------|---------|---------|--------------|
| `DYNAMODB_TABLE` | nein | `data` | Name der Ziel-Tabelle |
| `DYNAMODB_TABLE_ARN` | nein | `arn:aws:dynamodb:eu-central-1:423623826655:table/data` | Tabellen-ARN (wird genutzt, wenn `DYNAMODB_TABLE` nicht gesetzt ist) |
| `DYNAMODB_PARTITION_KEY` | nein | `id` | Attributname des Partition Keys |
| `S3_BUCKET` | nein | — | Default-Bucket für reine Objekt-Keys ohne `s3://`-Präfix |
| `AWS_REGION` | nein | `eu-central-1` | AWS-Region |

## Projektstruktur

- `Dockerfile` – Lambda-Runtime-Image (Python 3.11)
- `function/` – Anwendungscode und `requirements.txt`
- `tests/` – Unit-Tests und Beispiel-Fixtures als lebende Spezifikation
- `Jenkinsfile` – Pipeline (ECR-Login, Build, Push, Lambda-Deploy)

## Lokale Tests

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r function/requirements.txt -r tests/requirements.txt
pytest -v
```

Die Tests in `tests/test_main.py` beschreiben das erwartete Verhalten anhand der Fixtures unter `tests/fixtures/`.

## CI/CD

Die Jenkins-Pipeline entspricht dem Muster aus [news-archive-awslambda](https://github.com/mjairuobe/news-archive-awslambda.git):

1. Checkout
2. AWS ECR Login
3. Docker Build
4. Push nach ECR
5. Lambda-Deploy aus Container-Image (Create oder Update)

Angepasste Werte in der `Jenkinsfile`:

- `ECR_REPOSITORY`: `dflowp/documentdb-batchinsert-s3objs-lambda`
- `LAMBDA_FUNCTION_NAME`: `documentdb-batchinsert-s3objs-lambda`

## IAM-Berechtigungen (Lambda-Rolle)

Die Lambda-Rolle benötigt mindestens:

- `s3:GetObject` auf die relevanten Buckets/Keys
- `dynamodb:BatchWriteItem` und `dynamodb:PutItem` auf `arn:aws:dynamodb:eu-central-1:423623826655:table/data`
