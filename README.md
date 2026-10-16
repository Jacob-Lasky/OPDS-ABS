# OPDS server for Audiobookshelf

An OPDS 1.2 catalog in front of [Audiobookshelf](https://www.audiobookshelf.org/), so e-reader
apps (Marvin, KOReader, PocketBook, Moon+ Reader, Thorium) can browse, search and download the
ebooks in an ABS library.

This is a maintained fork of [petr-prikryl/opds-abs](https://github.com/petr-prikryl/opds-abs),
which is no longer actively maintained. The fork fixed, among other things, an authentication
bypass (any password worked for a user whose token was cached), ABS tokens embedded in every feed
link, an OpenSearch description that did not parse for authenticated clients (search never worked
in Marvin), and pagination that never advanced past page 1 on authors and collections. See
[issue #1](https://github.com/Jacob-Lasky/opds-abs/issues/1).

## Run

```bash
docker run -d -p 8000:8000 \
  -e AUDIOBOOKSHELF_INTERNAL_URL=http://audiobookshelf:80 \
  -e AUDIOBOOKSHELF_EXTERNAL_URL=https://abs.example.com \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v ./data:/app/opds_abs/data \
  ghcr.io/jacob-lasky/opds-abs:latest
```

or `docker compose up -d` with the included `docker-compose.yml`. Images are published from
`master` as `:latest` and `:sha-<commit>`; pin the sha tag if you want to choose when to upgrade.

Point the reader at `https://<host>/opds` and give it your Audiobookshelf username and password.

## Authentication

Readers authenticate with HTTP Basic, checked against Audiobookshelf's `/login`. Every catalog
request without valid credentials gets a `401` Basic challenge, which is what makes readers send
their stored credentials. The resulting ABS token is cached in memory, keyed by a hash of the
username **and** password, and never written to disk or into a URL.

With `API_KEY_AUTH_ENABLED=true` an Audiobookshelf API key (created under ABS Settings, API Keys)
is also accepted, as the Basic password, as `Authorization: Bearer <key>`, or as `?token=<key>`
for clients that cannot send headers. Leave it off unless a client needs it.

`/opds/<user>/libraries/<id>/search.xml` (the OpenSearch description) is served without auth: it
contains nothing that is not already in its URL, and some readers fetch it without credentials.

The `/admin/cache/*` endpoints require an ABS `root` or `admin` account.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `AUDIOBOOKSHELF_INTERNAL_URL` | `http://localhost` | URL this server uses to call ABS. |
| `AUDIOBOOKSHELF_EXTERNAL_URL` | internal URL | URL readers use for covers and images. |
| `AUDIOBOOKSHELF_URL` | | Legacy: sets both of the above. |
| `AUTH_ENABLED` | `true` | Require ABS credentials. |
| `API_KEY_AUTH_ENABLED` | `true` | Also accept ABS API keys (see above). |
| `AUTH_TOKEN_CACHING` | `true` | Cache ABS tokens in memory per credential. |
| `AUTH_CACHE_EXPIRY` | `3600` | Seconds verified credentials are reused before checking ABS again; also how long an old password keeps working after a change. |
| `PAGINATION_ENABLED` | `true` | Paginate large feeds. |
| `ITEMS_PER_PAGE` | `25` | Page size; `0` disables pagination. |
| `CACHE_PERSISTENCE_ENABLED` | `true` | Persist the ABS response cache to `data/cache.pkl`. |
| `OPDS_LOG_LEVEL` | `INFO` | `DEBUG` adds upstream calls and cache hits. |
| `OPDS_LOG_FORMAT` | `timestamped` | `plain` drops timestamps for collectors that add their own. |
| `FORWARDED_ALLOW_IPS` | `*` | Proxies trusted for `X-Forwarded-Proto/For`. Needed behind a reverse proxy so the search template advertises `https`. Narrow it if the port is reachable directly. |
| `PUID` / `PGID` | `1000` | Ownership of the data directory. |

## Logging

One line per request on stdout:

```
2026-10-16T14:59:11+0000 INFO    req=097696c5 opds_abs.access: GET /opds/alice/libraries/<id>/search?q=harry+potter -> 200 14000B 52ms user=alice auth=cache ip=203.0.113.7 ua="Marvin/3.6"
```

`auth=` is `password`, `apikey`, `cache` (credentials matched a cached login), `none` (no
credentials sent, answered with a challenge), `basic-rejected`, or `abs-unavailable`. Detail lines
from the same request (search terms and result counts, downloads, upstream errors) carry the same
`req=` id, which is also returned as the `X-Request-ID` response header and as the `<id>` of XML
error responses. Secrets in query strings are redacted.

## Development

```bash
uv venv --python 3.15 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q          # runs against an in-process fake Audiobookshelf
.venv/bin/python -m pyflakes opds_abs run.py tests
```

Runtime dependencies are declared in `requirements.in` and hash-pinned in `requirements.txt`
(regenerate with the command at the top of `requirements.in`).

To check a live deployment the way a reader experiences it, walk every paginated feed and the
search path:

```bash
OPDS_USER=alice OPDS_PASSWORD=... python3 scripts/walk_feeds.py \
  --base https://opds.example.com --library <library-id> --search "harry potter"
```

It fails on pages that repeat, counts that disagree with `opensearch:totalResults`, a search
description that does not parse, and auth that accepts missing or wrong credentials.
