# Review de code — `doc_router` (v2)

Date : 2026-05-11
Périmètre : `doc-router/`, `pymupdf4llm-api/`, `docker-compose.yml`, `tests/`, `README.md`, `.env.example`, `.gitignore`.
Focus : **performance/mémoire**, **robustesse/fiabilité**, **qualité du code**. (Sécurité non revue dans cette passe.)

## Résumé exécutif

Le projet est passé d'un POC propre à un service **prêt pour un pilote en production**. Tous les points bloquants de la première review (du matin) ont été corrigés, et la couverture de tests (unitaires + intégration avec `respx` + e2e shell) est désormais sérieuse. Le code reste compact, lisible, bien typé.

Les points restants sont **mineurs** : un timeout orphelin, une duplication de code logging entre les deux services, un buffering multipart potentiel côté MinerU, l'absence de retry sur erreurs upstream transitoires, et l'image `mineru:latest` toujours non pinnée.

**Note globale : prêt pour un pilote en production.** Les améliorations restantes sont de l'ordre du polissage.

---

## 1. Ce qui a été corrigé depuis la review précédente

Pour mémoire (passage rapide) :

- ✅ Streaming du body vers fichier temporaire ([doc-router/main.py:244-281](doc-router/main.py#L244-L281)) — plus de double allocation RAM.
- ✅ `httpx.AsyncClient` partagé via `lifespan` ([doc-router/main.py:125-136](doc-router/main.py#L125-L136)).
- ✅ Timeouts différenciés par upstream (`PYMUPDF_TIMEOUT_SECONDS=300`, `TIKA_TIMEOUT_SECONDS=300`, `MINERU_TIMEOUT_SECONDS=1700`).
- ✅ `JsonFormatter` custom et middleware logging par requête (`filename`, `parser`, `size_bytes`, `duration_ms`).
- ✅ `hmac.compare_digest` ([doc-router/main.py:208](doc-router/main.py#L208)).
- ✅ `fitz.open(...)` en `with`, `_process_pdf_sync` fusionne détection + extraction sur un **seul** `fitz.Document` ([doc-router/main.py:301-341](doc-router/main.py#L301-L341)).
- ✅ `.env.example` complet et bien commenté.
- ✅ `merge_metadata` helper ([doc-router/main.py:112-119](doc-router/main.py#L112-L119)) factorise le pattern précédent.
- ✅ `RouterResult` NamedTuple (typage cohérent).
- ✅ `extract_text_recursively` retourne `""` sur les scalaires non-string ([doc-router/main.py:241](doc-router/main.py#L241)) et arrête au premier hit non-vide ([doc-router/main.py:233-237](doc-router/main.py#L233-L237)).
- ✅ `tempfile` n'est plus un import mort — utilisé pour `mkstemp`.
- ✅ Dossier renommé `doc_router` (typo résolue), structure cohérente.
- ✅ `.gitignore` présent et complet (secrets, Python, venvs, caches, `tmp/`).
- ✅ Suite de tests complète : `test_unit.py` (helpers, fitz, formatter), `test_routing.py` (FastAPI + `respx` pour mocker MinerU/Tika), `test_pymupdf_api.py`, et `tests/e2e.sh` (smoke tests sur containers).

---

## 2. Performance / mémoire

### 2.1. ⚠️ Multipart MinerU buffer probablement encore le fichier en RAM

À [doc-router/main.py:402-410](doc-router/main.py#L402-L410) :

```python
with open(pdf_path, "rb") as fh:
    files = {"files": (filename, fh, mime or "application/octet-stream")}
    response = await client.post(
        f"{MINERU_ROUTER_URL}/file_parse",
        files=files,
        ...
    )
```

`httpx` accepte un file handle dans `files=` mais **construit le body multipart en mémoire avant l'envoi** (pas de streaming multipart). Pour un PDF de 200 Mo, on a une allocation RAM côté `doc-router` à chaque appel MinerU.

PyMuPDF (remote, [doc-router/main.py:352-358](doc-router/main.py#L352-L358)) et Tika (raw PUT, [doc-router/main.py:380-386](doc-router/main.py#L380-L386)) utilisent `content=fh`, qui lui streame. Bien.

**Reco** : envisager un streaming multipart custom (`client.stream("POST", ...)` avec construction du body), ou plus simplement documenter que pour MinerU la RAM ≈ `MAX_UPLOAD_MB × MINERU_MAX_CONCURRENCY` au pire (par défaut 200 × 2 = 400 Mo).

### 2.2. Sémaphore + thread pool : interaction à valider

`_route_pdf` ([doc-router/main.py:474-477](doc-router/main.py#L474-L477)) prend `pymupdf_sem` autour de `asyncio.to_thread(_process_pdf_sync, ...)`. Si `PYMUPDF_MAX_CONCURRENCY=4` et que le default thread pool d'asyncio est limité (par défaut `min(32, os.cpu_count() + 4)`), il n'y a pas de famine sur les VMs L40, mais sur un CPU faible (4 cœurs → 8 threads pool) ça peut serrer si on consomme des threads ailleurs.

**Reco** : non bloquant, mais préciser dans le README que `PYMUPDF_MAX_CONCURRENCY` est borné en pratique par le default thread pool d'asyncio.

### 2.3. Détection scanné : 3 premières pages

`PDF_SCAN_SAMPLE_PAGES=3` ([doc-router/main.py:293, 318](doc-router/main.py#L293)) échantillonne les 3 premières pages. Sur un PDF type rapport avec 3 pages de couverture/sommaire blanches puis du texte, on classera à tort comme "scanné" → fallback MinerU. Trade-off déjà documenté ; en cas de plaintes sur des PDF perçus comme lents, c'est le premier paramètre à ajuster (échantillonnage milieu/fin, ou pages aléatoires).

### 2.4. Pas de cache (volontaire)

Aucun cache résultat. Cohérent avec un service synchrone unique sans clé déduplicante côté OpenWebUI. Pas une critique.

---

## 3. Robustesse / fiabilité

### 3.1. ⚠️ Pas de retry sur erreurs transitoires upstream

Toujours absent. Une 502/503/504 ponctuelle côté MinerU (qui arrive en pratique : GPU sous pression, worker en cours de redémarrage) tombe immédiatement en 502 côté OpenWebUI, donc l'utilisateur voit l'échec.

**Reco** : un retry simple (1-2 essais, backoff exponentiel court, uniquement sur 502/503/504 et `httpx.TransportError`) améliorerait fortement l'UX sans alourdir le code. À mettre derrière un flag `UPSTREAM_RETRIES=1` pour rester déterministe en test.

### 3.2. ⚠️ Healthcheck Docker n'observe pas la dégradation

Le healthcheck Docker pour `doc-router` ([docker-compose.yml:158](docker-compose.yml#L158)) utilise `/health/live` (trivial). Conséquence : si MinerU ou Tika tombent, `doc-router` reste "healthy" pour Docker et tout container avec `depends_on: doc-router` croit l'API saine.

C'est un trade-off **volontaire** (commentaire lignes 156-157 du compose : `/health/ready` requiert l'API key quand elle est définie). Mais ça mérite d'être explicité dans le README — le monitoring de la dégradation doit venir d'ailleurs (Prometheus, ping externe sur `/health/ready` avec la clé).

### 3.3. Pas de circuit breaker

Si MinerU est down depuis 5 minutes, chaque requête `/process` PDF scanné/image tentera quand même un appel + timeout. Sans valeur ajoutée pour un service synchrone court, mais à reconsidérer si on observe des cascades.

### 3.4. `stream_body_to_tempfile` : flux d'exceptions redondant

À [doc-router/main.py:260-275](doc-router/main.py#L260-L275) :

```python
try:
    with os.fdopen(fd, "wb") as fh:
        ...
except HTTPException:
    tmp_path.unlink(missing_ok=True)
    raise
except Exception:
    tmp_path.unlink(missing_ok=True)
    raise
```

Les deux branches font la même chose. Simplifiable en une seule branche `except Exception` (HTTPException en hérite). Très mineur.

### 3.5. `REQUEST_TIMEOUT_SECONDS` est mort

Variable définie ([doc-router/main.py:81](doc-router/main.py#L81)) et présente dans `.env.example` ligne 42, mais **jamais utilisée** dans le code (les timeouts effectifs sont `PYMUPDF_/TIKA_/MINERU_TIMEOUT_SECONDS`). À supprimer pour éviter la confusion utilisateur.

### 3.6. `pymupdf4llm-api` sans `Authorization`

Le microservice opt-in `pymupdf4llm-api` n'a aucune authentification. Si quelqu'un active le profil `remote-pymupdf` et expose accidentellement `8001:8000`, le service est ouvert. **Mineur** car opt-in et pour un usage interne — mais à mentionner explicitement dans le README de la section "Sécurité".

### 3.7. Bonne robustesse sur les PDF malformés

`_process_pdf_sync` ([doc-router/main.py:301-341](doc-router/main.py#L301-L341)) attrape les exceptions `fitz.open()` et `pymupdf4llm.to_markdown()` et retourne `(True, "", meta)` pour fallback MinerU. Un client qui PUT un faux PDF avec `Content-Type: application/pdf` part vers MinerU plutôt que de générer une 500. Testé ([tests/test_unit.py:165-170](tests/test_unit.py#L165-L170)). ✓

---

## 4. Qualité du code

### 4.1. Duplication du `JsonFormatter` et du middleware de logging

Le `JsonFormatter` et `log_requests` sont **strictement identiques** entre [doc-router/main.py:22-44, 142-169](doc-router/main.py#L22-L44) et [pymupdf4llm-api/main.py:14-36, 65-92](pymupdf4llm-api/main.py#L14-L36).

Sans monorepo Python (pas de package commun), trois options :

- **A.** Accepter la duplication (≈ 70 lignes par service), c'est petit et stable.
- **B.** Créer un mini package `_common/logging.py` partagé via `COPY` dans les deux Dockerfiles.
- **C.** Extraire dans un package PyPI interne (overkill).

L'option **A** est probablement la bonne tant que le projet reste à deux services.

### 4.2. `stream_body_to_tempfile` aussi dupliqué

Idem ([doc-router/main.py:244-281](doc-router/main.py#L244-L281) vs [pymupdf4llm-api/main.py:101-138](pymupdf4llm-api/main.py#L101-L138)). Même remarque.

### 4.3. `_detect_scanned_sync` n'est plus utilisé qu'en mode remote

[doc-router/main.py:287-298](doc-router/main.py#L287-L298) — uniquement appelé dans la branche `USE_REMOTE_PYMUPDF=True`. Le mode local utilise `_process_pdf_sync` qui re-fait la détection. La fonction reste utile pour les tests unitaires et pour la branche remote, donc à garder, mais ajouter un docstring qui précise ce périmètre éviterait la confusion.

### 4.4. `route_document` : fallback Tika → MinerU sur unknown-type

À [doc-router/main.py:513-521](doc-router/main.py#L513-L521), si Tika renvoie `"   "`, on tombe sur MinerU. Bien ([tests/test_routing.py:206-220](tests/test_routing.py#L206-L220) couvre ce cas). ✓

### 4.5. `app.state.http_client` accédé via `request.app.state.http_client`

[doc-router/main.py:547, 587](doc-router/main.py#L547) — fonctionnel. Pour le typage, une variable de module ou un `Depends` rendrait l'IDE plus heureux. Pas critique.

### 4.6. Gunicorn `--timeout 1800` côté `doc-router/Dockerfile`

[doc-router/Dockerfile:21](doc-router/Dockerfile#L21) — 1800 s vs `MINERU_TIMEOUT_SECONDS=1700` → 100 s de marge, OK. À noter que si quelqu'un override `MINERU_TIMEOUT_SECONDS` à 1850, Gunicorn coupera avant. Ajouter un commentaire dans le Dockerfile, ou rendre le timeout configurable via un entrypoint qui lit les env.

### 4.7. `pymupdf4llm-api/Dockerfile` : Gunicorn `--timeout 600`

[pymupdf4llm-api/Dockerfile:21](pymupdf4llm-api/Dockerfile#L21) — incohérent avec `PYMUPDF_TIMEOUT_SECONDS=300` côté doc-router. Pas un bug (le serveur peut être plus tolérant que le client), mais à documenter ou aligner.

### 4.8. `extract_text_recursively` : agrégation des valeurs en cas d'absence des clés prioritaires

[doc-router/main.py:238-240](doc-router/main.py#L238-L240) — si aucune clé prioritaire n'est trouvée, on concatène **toutes** les valeurs du dict. Si MinerU change son format et renvoie un dict avec plusieurs champs textuels, on peut récupérer du bruit (timestamps stringifiés, IDs…). Le code semble accepter ce risque pour rester résilient. **Reco** : ajouter un test qui documente ce comportement (`test_extract_text_recursively_unknown_shape_aggregates_all_values`).

### 4.9. `_process_pdf_sync` : nommage du flag de retour

Le tuple est `(is_scanned_or_failed, ...)` — la métadonnée `pymupdf4llm_error` distingue scanné vs échec parseur. C'est propre, mais le nom `is_scanned_or_failed` est ambigu. Un docstring (ou un alias `should_fallback_to_mineru`) clarifierait l'intention.

### 4.10. `merge_metadata(None, {}, {...})` accepté

[doc-router/main.py:112-119](doc-router/main.py#L112-L119) — `if source` ignore `None` et `{}`. Testé. ✓

---

## 5. Tests

Bond qualitatif énorme. Couvre :

- **Unitaires** ([tests/test_unit.py](tests/test_unit.py)) : `normalize_filename` (path traversal), `parse_mime`, `is_pdf`/`is_image`/`is_tika_type`, `extract_text_recursively` (formes connues + scalaires + fallback values), `ensure_authorized` (no key / match / mismatch / missing / **vérifie que `hmac.compare_digest` est bien appelé**), `_detect_scanned_sync` (PDF text / vide / invalide), `_process_pdf_sync` (success / scanned / invalid / to_markdown crash), `RouterResult`, `merge_metadata`, `JsonFormatter`.
- **Intégration FastAPI + respx** ([tests/test_routing.py](tests/test_routing.py)) : empty body 400, oversize 413 (content-length + stream), 401 unauthorized, 200 valid token, variantes MIME PDF, chemin local PyMuPDF, fallback scanné → MinerU, fallback empty → MinerU, fallback crash → MinerU, **mode remote**, DOCX → Tika, image → MinerU, unknown → Tika then MinerU, **all-fail → 502**, **cleanup tempfile success + upstream failure**, healthchecks (live/ready/auth/local-mode/degraded), **middleware logging capture**.
- **pymupdf4llm-api** ([tests/test_pymupdf_api.py](tests/test_pymupdf_api.py)) : healthchecks, empty/oversize, cleanup, invalid PDF → 500, valid PDF, filename normalization.
- **E2E shell** ([tests/e2e.sh](tests/e2e.sh)) : healthchecks + PUT /process avec PDF généré + chemins négatifs (empty body, wrong API key).

**Manques mineurs** :

- Pas de test de `_route_pdf` avec mode remote sur PDF vide (le chemin couvre `is_scanned=True → fallback direct MinerU`).
- Pas de test sur le format de réponse JsonFormatter quand `LOG_FORMAT=json` est configuré au démarrage (testé sur le formatter directement, pas via la config app).
- Pas de test de chargement de `_configure_logging` (mais facile à valider visuellement).

Ce sont des polissages, pas des trous.

---

## 6. Suggestions d'amélioration prioritaires (restantes)

Ordre proposé :

1. **Retry sur erreurs upstream transitoires** (502/503/504, `httpx.TransportError`) — meilleur UX/fiabilité, peu de code.
2. **Supprimer `REQUEST_TIMEOUT_SECONDS`** (variable morte) + nettoyer `.env.example` ligne 42.
3. **Pinner `mineru:latest`** vers une version explicite + documenter comment l'image est construite/tirée.
4. **Documenter le healthcheck Docker `/health/live` "trivial"** dans le README (et indiquer comment monitorer la dégradation depuis l'extérieur).
5. **Aligner ou commenter le `Gunicorn --timeout`** entre les deux Dockerfiles et les env timeouts.
6. **Documenter le buffering multipart MinerU** dans la section "Uploads" du README (RAM ≈ `MAX_UPLOAD_MB × MINERU_MAX_CONCURRENCY`).
7. **Simplifier les deux branches identiques** dans `stream_body_to_tempfile` (mineur).
8. **Mentionner `pymupdf4llm-api` sans auth** dans la section sécurité du README.

Aucune de ces actions n'est bloquante.

---

## 7. Conclusion

Le projet est **mûr pour un pilote en production**. La trajectoire entre la première review (matin) et celle-ci (après-midi) montre une exécution de très bon niveau. Les points restants sont du polissage opérationnel — ils peuvent être traités au fil de l'eau ou regroupés dans un PR de finalisation.
