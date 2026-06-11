# documentdb-batchinsert-s3objs-lambda

AWS-Lambda-Funktion, die per **PUT**-Request ein JSON-Array von S3-Pfaden entgegennimmt, die referenzierten JSON-Dateien aus S3 liest und alle enthaltenen Objekte als Dokumente in **Amazon DocumentDB** speichert.

Jedes eingefügte Dokument erhält ein zusätzliches Feld `_source` mit dem kanonischen S3-Pfad der Quelldatei.

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
3. Für jedes Array-Element ein Dokument erzeugen
4. `_source` mit dem kanonischen Pfad `s3://bucket/key` setzen
5. Alle Dokumente in die konfigurierte DocumentDB-Collection einfügen

Aus zwei S3-Dateien mit insgesamt drei Objekten entstehen **drei separate DocumentDB-Dokumente**.

### Ausgabe (erfolgreich)

```json
{
  "pathsProcessed": 2,
  "documentsInserted": 3,
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

### Erwartete DocumentDB-Dokumente

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
| DocumentDB-Fehler | 500 | `{"error": "DocumentDB error: ..."}` |

## Umgebungsvariablen

| Variable | Pflicht | Beschreibung |
|----------|---------|--------------|
| `DOCUMENTDB_URI` | ja | MongoDB-Connection-String für DocumentDB |
| `DOCUMENTDB_DATABASE` | ja | Zieldatenbank |
| `DOCUMENTDB_COLLECTION` | ja | Ziel-Collection |
| `DOCUMENTDB_TLS_CA_FILE` | nein | Pfad zum RDS-TLS-Bundle (Default: `/var/task/global-bundle.pem`) |
| `S3_BUCKET` | nein | Default-Bucket für reine Objekt-Keys ohne `s3://`-Präfix |
| `AWS_REGION` | nein | AWS-Region (Default: `eu-central-1`) |

## Projektstruktur

- `Dockerfile` – Lambda-Runtime-Image (Python 3.11) inkl. RDS-TLS-Bundle
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
- Netzwerkzugriff auf DocumentDB (typischerweise innerhalb desselben VPC)
- Optional: `ec2:CreateNetworkInterface` usw., wenn die Funktion in einem VPC läuft
