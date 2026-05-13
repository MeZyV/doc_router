# OpenWebUI Document Parsing Router

Ce projet met en place une brique d'extraction documentaire pour OpenWebUI avec un objectif simple : garder l'UX synchrone d'OpenWebUI, mais réduire au maximum le temps bloquant en envoyant chaque document vers le parser le plus adapté.

## Contexte et arbitrages

OpenWebUI attend que le document soit extrait, chunké et vectorisé avant de pouvoir l'utiliser dans le chat. L'idée n'est donc pas de rendre OpenWebUI asynchrone, mais de rendre l'extraction aussi rapide et robuste que possible.

Le contrat `External Document Extractor` d'OpenWebUI est le suivant :

- OpenWebUI appelle `PUT /process` sur l'URL configurée.
- Le body est le fichier brut, pas du `multipart/form-data`.
- Les headers utiles sont notamment `Content-Type`, `X-Filename` et éventuellement `Authorization`.
- La réponse attendue est un objet ou une liste d'objets de type `{"page_content": "...", "metadata": {...}}`.

MinerU expose de son côté une API FastAPI avec un endpoint synchrone `POST /file_parse` et un router multi-GPU (`mineru-router`) qui expose la même interface que `mineru-api`. Le router peut lancer des workers locaux via `--local-gpus auto`, ce qui évite de gérer manuellement un container par GPU au début.

## Choix techniques

### 1. MinerU Router pour les documents lourds

MinerU est gardé comme moteur premium pour :

- PDF scannés ;
- images ;
- PDF avec layout complexe ;
- OCR ;
- tableaux/formules complexes ;
- fallback quand un parser léger échoue.

Le compose utilise :

```text
mineru-router --local-gpus auto
```

sur les deux L40. C'est plus simple que de gérer deux `mineru-api` upstream séparés. Le mode upstream reste une évolution possible si on veut des logs par GPU, du pinning strict ou des réglages différents par carte.

### 2. PyMuPDF4LLM pour les PDF textuels

PyMuPDF4LLM est utilisé comme fast path lightweight pour les PDF qui contiennent déjà du texte exploitable. Il tourne sur CPU et n'a pas besoin de GPU.

Pourquoi ne pas le mettre sur GPU ? Parce que PyMuPDF/PyMuPDF4LLM fait principalement de l'analyse PDF, extraction de blocs, lecture de structure et génération Markdown. Ces opérations sont surtout CPU/RAM/I/O, pas de l'inférence deep learning.

**Depuis la 0.2.0, PyMuPDF4LLM tourne en in-process dans `doc-router`** : la détection scanné/textuel et l'extraction Markdown partagent un seul `fitz.Document`, ce qui supprime la double ouverture (et le round-trip HTTP intra-Docker) qui pesait sur la latence. Le `Document` est fermé en `finally` pour garantir la libération du file descriptor, même en cas d'exception (prévention des erreurs "too many open files" sous charge).

Le microservice `pymupdf4llm-api` reste packagé dans le dépôt comme **fallback opt-in** sous le profil Docker Compose `remote-pymupdf`. Pour le réactiver :

```bash
USE_REMOTE_PYMUPDF=true docker compose --profile remote-pymupdf up -d --build
```

### 3. Tika pour Office / texte générique

Tika reste utile comme parser CPU générique pour :

- DOCX ;
- PPTX ;
- XLSX ;
- HTML ;
- TXT ;
- formats bureautiques divers.

On ne l'utilise pas comme chemin principal pour PDF, car PyMuPDF4LLM est plus adapté aux PDF textuels et MinerU aux PDF complexes/scannés.

### 4. doc-router comme API External OpenWebUI

`doc-router` expose l'API compatible OpenWebUI :

```http
PUT /process
```

Il lit le body brut envoyé par OpenWebUI, choisit le bon parser, normalise la réponse en `page_content`, ajoute des métadonnées utiles, puis renvoie le résultat à OpenWebUI.

## Architecture

```text
OpenWebUI
  |
  | PUT /process, raw bytes
  v
doc-router FastAPI
  |-- PDF textuel --------> pymupdf4llm (in-process, single fitz.open)
  |-- PDF scanné/image ---> mineru-router -> GPUs L40
  |-- Office/HTML/TXT ----> Tika CPU
  |-- fallback -----------> MinerU ou Tika selon type

  (opt-in: USE_REMOTE_PYMUPDF=true → délègue à pymupdf4llm-api via HTTP)
```

## Routing

Le routing par défaut est :

| Document | Parser |
|---|---|
| PDF avec texte suffisant | PyMuPDF4LLM |
| PDF scanné ou très pauvre en texte | MinerU |
| Images | MinerU |
| DOCX/PPTX/XLSX/HTML/TXT | Tika |
| Fallback PDF | MinerU |
| Fallback Office/TXT | Tika |

La détection de PDF scanné est volontairement simple et rapide : `doc-router` échantillonne quelques pages avec PyMuPDF et mesure la quantité de texte extractible. Si le texte est trop faible, le document part vers MinerU.

Variables utiles :

```env
PDF_MIN_TEXT_CHARS=150
PDF_SCAN_SAMPLE_PAGES=3
```

## Structure du projet

```text
owui-doc-parsing/
├── docker-compose.yml
├── README.md
├── .env.example
├── doc-router/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── pymupdf4llm-api/
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
└── tmp/
    ├── doc-router/
    └── pymupdf4llm/
```

## Configuration OpenWebUI

Dans OpenWebUI :

```env
CONTENT_EXTRACTION_ENGINE=external
EXTERNAL_DOCUMENT_EXTRACTOR_URL=http://doc-router:8000
```

Important : ne pas ajouter `/process` dans l'URL. OpenWebUI l'ajoute lui-même.

Si OpenWebUI tourne hors du réseau Docker, utiliser plutôt :

```env
EXTERNAL_DOCUMENT_EXTRACTOR_URL=http://<host>:8000
```

## Lancement

```bash
docker compose up -d --build
```

Puis vérifier :

```bash
curl http://localhost:8000/health/live
# Si EXTERNAL_API_KEY est défini :
curl -H "Authorization: Bearer $EXTERNAL_API_KEY" http://localhost:8000/health/ready
curl http://localhost:8002/health
curl http://localhost:9998/tika
```

## Test manuel OpenWebUI-compatible

```bash
curl -X PUT "http://localhost:8000/process" \
  -H "Content-Type: application/pdf" \
  -H "X-Filename: sample.pdf" \
  --data-binary "@sample.pdf"
```

Réponse attendue :

```json
{
  "page_content": "...markdown ou texte extrait...",
  "metadata": {
    "filename": "sample.pdf",
    "parser": "pymupdf4llm",
    "router": "doc-router"
  }
}
```

## Healthchecks

### doc-router

- `GET /health/live` : process vivant. **Toujours public** (utilisé par le healthcheck Docker).
- `GET /health/ready` : dépendances prêtes. **Requiert l'API key si `EXTERNAL_API_KEY` est défini** (la réponse expose les statuts internes des upstreams).
- `GET /health` : alias de `/health/ready` (mêmes règles d'auth).

### pymupdf4llm-api

- `GET /health/live`
- `GET /health/ready`
- `GET /health`

### MinerU

- `GET /health` sur `mineru-router:8002`.

### Tika

- `GET /tika` sur `tika:9998`.

## Concurrence

Le router FastAPI est lancé avec 3 workers Gunicorn/Uvicorn. Ce n'est pas un mapping strict vers les parsers : un worker FastAPI ne correspond pas à un parser. Les vraies limites de concurrence sont dans le code via sémaphores :

```env
MINERU_MAX_CONCURRENCY=2
PYMUPDF_MAX_CONCURRENCY=4
TIKA_MAX_CONCURRENCY=4
```

Ces limites sont par process Gunicorn. Pour des garanties globales strictes entre workers, il faudrait ajouter Redis ou un autre coordinateur. Pour un premier déploiement synchrone, c'est volontairement plus simple.

## Pourquoi pas de queue async ?

Parce qu'OpenWebUI attend quand même la réponse du parser avant de continuer. Ajouter Redis/Celery ne supprimerait pas le blocage utilisateur sans modifier l'expérience d'ingestion. La priorité est donc :

1. éviter MinerU quand ce n'est pas nécessaire ;
2. utiliser PyMuPDF4LLM pour les PDF texte ;
3. réserver les L40 aux vrais cas OCR/layout lourds ;
4. garder Tika pour les formats Office et texte générique.

## Points à ajuster en prod

### MinerU

Si la VRAM est trop sollicitée :

```yaml
- --gpu-memory-utilization
- "0.75"
```

ou moins selon besoin.

### Uploads

```env
MAX_UPLOAD_MB=200
```

À augmenter si vous avez beaucoup de gros PDF. Le body est streamé directement vers un fichier temporaire dans `TMP_DIR` (pas de double allocation en RAM), donc le facteur limitant pour 500 MB est l'espace disque sur `TMP_DIR`, pas la mémoire. Prévoir au minimum `MAX_UPLOAD_MB × MINERU_MAX_CONCURRENCY` (par défaut 200 × 2 = 400 Mo) d'espace libre, ou plus si plusieurs workers Gunicorn sont actifs. La vérification de taille est faite à la fois sur le header `Content-Length` (rejet immédiat) et au fil du stream (sécurité contre un client mentant sur la taille annoncée).

### Timeouts

Le `doc-router` a un timeout long (`1800s`) car MinerU peut prendre du temps sur gros documents.

### Sécurité

Le compose n'expose que les ports utiles pour debug local. En production, il est préférable de :

- ne pas exposer `mineru-router`, `tika` et `pymupdf4llm-api` publiquement ;
- exposer uniquement `doc-router` à OpenWebUI ;
- activer `EXTERNAL_API_KEY` si besoin ;
- utiliser un réseau Docker interne ou un reverse proxy privé.

### File descriptors

Les services définissent `ulimits.nofile=65536`. PyMuPDF garde un FD ouvert par `Document` ; pour éviter les erreurs `too many open files` sous charge :

- les `fitz.Document` sont systématiquement fermés en `finally`, y compris si l'extraction Markdown ou la détection lèvent une exception ;
- le client `httpx.AsyncClient` est partagé via le `lifespan` FastAPI (réutilisation des connexions keep-alive) plutôt que recréé à chaque requête ;
- les limites par défaut httpx sont `max_keepalive_connections=20`, `max_connections=50` (override via `HTTPX_MAX_KEEPALIVE` / `HTTPX_MAX_CONNECTIONS`).

## Limites connues

- La réponse MinerU peut varier selon la version. Le code tente d'extraire Markdown/texte depuis plusieurs formes JSON courantes via `extract_text_recursively()`, mais il faudra peut-être l'ajuster après inspection d'une vraie réponse `/file_parse`.
- Les sémaphores sont locaux à chaque process Gunicorn.
- La détection PDF scanné est volontairement rapide, donc imparfaite. En cas de doute, le fallback vers MinerU couvre les échecs PyMuPDF4LLM.
- En mode in-process (par défaut), un crash de `pymupdf4llm.to_markdown` impacte le worker FastAPI lui-même — c'est rattrapé proprement, mais à monitorer si le taux de fallback MinerU augmente sans raison apparente.
