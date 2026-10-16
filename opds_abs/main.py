"""Routes for the OPDS feed.

This module contains all the FastAPI route definitions for the OPDS-ABS application
and configures the logging for the application.

Application Architecture:
----------------------
OPDS-ABS follows a layered architecture pattern:

1. API Layer (main.py):
   - FastAPI route handlers that process HTTP requests
   - Authentication via Depends(get_authenticated_user)
   - Exception handling and conversion to OPDS-compatible responses
   - Basic validation of request parameters

2. Service Layer (feeds/*.py):
   - Feed generators that transform data into OPDS XML
   - Caching mechanisms to optimize performance
   - Integration with external APIs

3. Data Access Layer (api/client.py):
   - Communication with Audiobookshelf API
   - Request formatting and response parsing
   - Error handling for API communication

4. Utility Layer (utils/*.py):
   - Authentication utilities
   - Caching implementation
   - Error handling and logging
   - XML utilities for feed generation

OPDS Compliance:
--------------
The application implements the OPDS 1.2 catalog specification, providing:
- Navigation feeds
- Acquisition feeds
- Search functionality
- Proper MIME types and relationships
- Basic authentication support

Each route in this file corresponds to a specific OPDS feed type, with common
error handling and authentication patterns applied consistently.
"""
# Standard library imports
import logging
import time
import atexit
from email.message import Message
from email.utils import collapse_rfc2231_value
import os
import re
import unicodedata
from urllib.parse import quote
from contextlib import asynccontextmanager


# Third-party imports
import aiohttp
from lxml import etree
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# Local application imports
from opds_abs.config import (
    LOG_LEVEL, AUTH_ENABLED, CACHE_PERSISTENCE_ENABLED, AUDIOBOOKSHELF_API,
    AUDIOBOOKSHELF_INTERNAL_URL, AUDIOBOOKSHELF_EXTERNAL_URL,
    API_KEY_AUTH_ENABLED, AUTH_TOKEN_CACHING,
    PAGINATION_ENABLED, ITEMS_PER_PAGE, FORWARDED_ALLOW_IPS
)
from opds_abs.feeds.library_feed import LibraryFeedGenerator
from opds_abs.feeds.navigation_feed import NavigationFeedGenerator
from opds_abs.feeds.series_feed import SeriesFeedGenerator
from opds_abs.feeds.collection_feed import CollectionFeedGenerator
from opds_abs.feeds.author_feed import AuthorFeedGenerator
from opds_abs.feeds.search_feed import SearchFeedGenerator
from opds_abs.utils.cache_utils import _cache, clear_cache, load_cache_from_disk, save_cache_to_disk
from opds_abs.utils.auth_utils import (
    CHALLENGE_HEADERS, get_authenticated_user, invalidate_token, require_admin)
from opds_abs.utils.logging_utils import AccessLogMiddleware, configure_logging
from opds_abs.utils.error_utils import (
    APIClientError,
    AuthenticationError,
    OPDSBaseException,
    error_response,
)

configure_logging()
logger = logging.getLogger(__name__)

# Create instances of our feed generators
library_feed = LibraryFeedGenerator()
navigation_feed = NavigationFeedGenerator()
series_feed = SeriesFeedGenerator()
collection_feed = CollectionFeedGenerator()
author_feed = AuthorFeedGenerator()
search_feed = SearchFeedGenerator()

# Create startup and shutdown sequences for loading cache
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the cache from disk on application startup and log configuration."""
    logger.info("OPDS-ABS %s starting", os.environ.get("OPDS_ABS_VERSION", "dev"))
    # Log configuration settings
    logger.info("Starting OPDS-ABS with configuration:")
    logger.info(f"Audiobookshelf URL: {AUDIOBOOKSHELF_INTERNAL_URL}")
    logger.info(f"Audiobookshelf Internal URL: {AUDIOBOOKSHELF_INTERNAL_URL}")
    logger.info(f"Audiobookshelf External URL: {AUDIOBOOKSHELF_EXTERNAL_URL}")
    logger.info(f"Authentication Enabled: {AUTH_ENABLED}")
    logger.info(f"API Key Authentication Enabled: {API_KEY_AUTH_ENABLED}")
    if not API_KEY_AUTH_ENABLED:
        logger.info("API key authentication disabled: only username/password will work")
    logger.info(f"Auth Token Caching: {AUTH_TOKEN_CACHING}")
    logger.info(f"Cache Persistence Enabled: {CACHE_PERSISTENCE_ENABLED}")

    # Log pagination settings
    if PAGINATION_ENABLED:
        logger.info(f"Pagination: {PAGINATION_ENABLED} (Items per page: {ITEMS_PER_PAGE})")
    else:
        logger.info(f"Pagination: {PAGINATION_ENABLED} (Disabled - all items will be shown in feeds)")

    logger.info(f"Log Level: {LOG_LEVEL}")

    if CACHE_PERSISTENCE_ENABLED:
        logger.info("Loading cache from disk...")
        load_cache_from_disk()
    yield
    # Shutdown: persist the cache.
    if CACHE_PERSISTENCE_ENABLED:
        logger.info("Saving cache to disk...")
        save_cache_to_disk(force=True)

# Create FastAPI app
app = FastAPI(
    title="OPDS-ABS",
    description="OPDS server for Audiobookshelf",
    version=os.environ.get("OPDS_ABS_VERSION", "dev"),
    lifespan=lifespan
)

# The last middleware added is the outermost. ProxyHeaders goes outside the access
# log so the log sees the real client IP and scheme. It lives in the app rather
# than behind uvicorn's proxy_headers flag so the tests exercise it too.
app.add_middleware(AccessLogMiddleware)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=FORWARDED_ALLOW_IPS)

# Mount static files directory
app.mount("/static", StaticFiles(directory="opds_abs/static"), name="static")

# Setup templates
templates = Jinja2Templates(directory="opds_abs/templates")

# Register atexit handler as a backup for when the shutdown event doesn't fire
if CACHE_PERSISTENCE_ENABLED:
    atexit.register(save_cache_to_disk, True)

# Exception handlers. Routes do not catch exceptions themselves: known types land
# here, are logged once, and become an escaped XML error document whose <id> is
# the request id from the access log line. Anything unexpected is answered by
# AccessLogMiddleware (see the comment there for why not here).
def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


@app.exception_handler(AuthenticationError)
async def authentication_error_handler(request: Request, exc: AuthenticationError):
    """ABS rejected a token mid-request: challenge so the client re-sends credentials."""
    logger.warning("Authentication error on %s: %s", request.url.path, exc)
    return error_response(401, "Authentication required", _request_id(request),
                          headers=CHALLENGE_HEADERS)


@app.exception_handler(APIClientError)
async def api_client_error_handler(request: Request, exc: APIClientError):
    """Audiobookshelf unreachable or erroring: 503, no traceback (already logged)."""
    logger.warning("Audiobookshelf error on %s: %s", request.url.path, exc)
    return error_response(503, str(exc), _request_id(request))


@app.exception_handler(OPDSBaseException)
async def opds_exception_handler(request: Request, exc: OPDSBaseException):
    """Our own exception types: log 5xx with traceback, 4xx as a single line."""
    if exc.status_code >= 500:
        logger.error("%s on %s: %s", type(exc).__name__, request.url.path, exc, exc_info=exc)
    else:
        logger.info("%s on %s: %s", type(exc).__name__, request.url.path, exc)
    return error_response(exc.status_code, str(exc) or exc.default_message, _request_id(request))


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTPExceptions raised deliberately (401 challenges, 404s): no traceback."""
    if exc.status_code != 401:  # 401 challenges are routine and already in the access log
        logger.info("HTTP %d on %s: %s", exc.status_code, request.url.path, exc.detail)
    return error_response(exc.status_code, exc.detail, _request_id(request), headers=exc.headers)


def _canonical_user_redirect(request: Request, username: str, auth_info: tuple):
    """Redirect /opds/<username>/... to the authenticated ABS username.

    Feed URLs carry the username, but the credentials decide who the caller is.
    Returns None when they already agree (or auth is disabled). The query string
    is forwarded still encoded: rebuilding it from parsed params broke any value
    with a space or '&' in it, e.g. a search for "harry potter".
    """
    auth_username, _, display_name = auth_info
    if not (AUTH_ENABLED and auth_username) or username == display_name:
        return None
    # Split the RAW path so encoded characters in other segments stay encoded.
    raw_path = request.scope.get("raw_path", b"").decode("latin-1") or quote(request.url.path)
    segments = raw_path.split("/")
    segments[2] = quote(display_name, safe="")  # ["", "opds", <username>, ...]
    url = "/".join(segments)
    if request.url.query:
        url += f"?{request.url.query}"
    return RedirectResponse(url=url)


@app.get("/healthz")
async def healthz():
    """Liveness for the container HEALTHCHECK. Does not touch Audiobookshelf."""
    return {"status": "ok", "version": os.environ.get("OPDS_ABS_VERSION", "dev")}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Render the index page.

    Args:
        request (Request): The incoming request object.

    Returns:
        HTMLResponse: The rendered index.html template.
    """
    return templates.TemplateResponse(request, "index.html")


@app.get("/opds", response_class=RedirectResponse)
async def opds_root_redirect(auth_info: tuple = Depends(get_authenticated_user)):
    """Redirect /opds to the authenticated user's catalog root.

    Lets a reader be configured with just https://host/opds plus credentials.
    Unauthenticated requests never get here: the dependency answers with a 401
    challenge when AUTH_ENABLED.
    """
    username = auth_info[0]
    return RedirectResponse(url=f"/opds/{quote(username or 'anonymous', safe='')}")


@app.get("/opds/{username}/libraries/{library_id}/search.xml")
async def search_xml(username: str, library_id: str, request: Request):
    """Serve the OpenSearch description document for a library.

    Clients (Marvin, KOReader, Thorium...) fetch this from the feed's
    ``rel="search"`` link, then substitute ``{searchTerms}`` into the template.

    DO NOT go back to rendering this from a Jinja template with the user's token
    appended. ``...&token=<jwt>`` put a bare ``&`` in an XML attribute, so every
    authenticated client received a document that does not parse, and Marvin
    silently never searched (#1). It is built with lxml, so it is always well
    formed, and it carries no credentials: the search request authenticates the
    same way as every other feed request.

    No auth dependency: the document holds nothing that is not already in the URL,
    and some clients fetch it without sending credentials.
    """
    # Built by hand: url_for does not percent-encode path params, so a username
    # with a space or "?" produced a template that split at the wrong place.
    search_url = (f"{request.base_url}opds/{quote(username, safe='')}"
                  f"/libraries/{quote(library_id, safe='')}/search")
    osd = etree.Element(
        "OpenSearchDescription", nsmap={None: "http://a9.com/-/spec/opensearch/1.1/"})
    for tag, text in (
        ("ShortName", "OPDS-ABS"),
        ("LongName", "Audiobookshelf OPDS catalog"),
        ("Description", "Search the Audiobookshelf ebook catalog."),
        ("InputEncoding", "UTF-8"),
        ("OutputEncoding", "UTF-8"),
    ):
        etree.SubElement(osd, tag).text = text
    etree.SubElement(osd, "Url", {
        "type": "application/atom+xml;profile=opds-catalog;kind=acquisition",
        # Absolute: OpenSearch templates are not reliably resolved against the
        # description's URL by clients. Behind a proxy this needs uvicorn's
        # proxy_headers (FORWARDED_ALLOW_IPS) so the scheme is https.
        "template": f"{search_url}?q={{searchTerms}}",
    })
    body = etree.tostring(osd, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return Response(content=body, media_type="application/opensearchdescription+xml")


@app.get("/opds/{username}")
async def opds_root(
    username: str,
    request: Request,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get the root OPDS feed for a user.

    Args:
        username (str): The username path parameter.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The root feed listing available libraries.
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    return await library_feed.generate_root_feed(
        effective_username,
        token=token
    )


@app.get("/opds/{username}/libraries/{library_id}")
async def opds_nav(
    username: str,
    request: Request,
    library_id: str,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get the navigation feed for a library.

    Args:
        username (str): The username path parameter.
        library_id (str): ID of the library to get navigation for.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The navigation feed for the specified library.
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    return await navigation_feed.generate_navigation_feed(
        effective_username,
        library_id,
        token=token
    )


@app.get("/opds/{username}/libraries/{library_id}/search")
async def opds_search(
    username: str,
    library_id: str,
    request: Request,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Search for items in a library.

    Args:
        username (str): The username path parameter.
        library_id (str): ID of the library to search in.
        request (Request): The incoming request object containing search parameters.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The search results feed.
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    params = dict(request.query_params)
    return await search_feed.generate_search_feed(
        effective_username,
        library_id,
        params,
        token=token
    )


@app.get("/opds/{username}/libraries/{library_id}/items")
async def opds_library(
    username: str,
    library_id: str,
    request: Request,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get items from a specific library.

    Args:
        username (str): The username path parameter.
        library_id (str): ID of the library to get items from.
        request (Request): The incoming request object containing filter parameters.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The items feed for the specified library.

    Query Parameters:
        start_index (int): Starting index for pagination (1-based).
        sort (str): Field to sort results by.
        desc (str): If present, sort in descending order.
        collection (str): ID of a collection to filter by.
        filter (str): Additional filter criteria.
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    params = dict(request.query_params)

    # Log pagination parameters for debugging
    if 'start_index' in params:
        logger.debug("Pagination requested with start_index: %s", params['start_index'])

    return await library_feed.generate_library_items_feed(
        effective_username,
        library_id,
        params,
        token=token
    )


@app.get("/opds/{username}/libraries/{library_id}/series")
async def opds_series(
    username: str,
    request: Request,
    library_id: str,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get series from a specific library.

    Args:
        username (str): The username path parameter.
        library_id (str): ID of the library to get series from.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The series feed for the specified library.
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    return await series_feed.generate_series_feed(
        effective_username,
        library_id,
        token=token
    )


@app.get("/opds/{username}/libraries/{library_id}/series/{series_id}")
async def opds_series_items(
    username: str,
    request: Request,
    library_id: str,
    series_id: str,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get items from a specific series.

    This endpoint retrieves books belonging to a particular series within a library.
    It uses cached data when possible to optimize performance and applies proper
    sorting based on series sequence numbers.

    Args:
        username (str): The username path parameter identifying the user.
        library_id (str): ID of the library containing the series.
        series_id (str): ID of the specific series to get books from.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: An OPDS feed containing books from the specified series,
                 sorted by their sequence number within the series.

    Raises:
        ResourceNotFoundError: If the specified series doesn't exist.
        FeedGenerationError: If there's an error generating the feed.
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    return await series_feed.generate_series_items_feed(
        effective_username,
        library_id,
        series_id,
        token=token
    )


@app.get("/opds/{username}/libraries/{library_id}/collections")
async def opds_collections(
    username: str,
    request: Request,
    library_id: str,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get collections from a specific library.

    Args:
        username (str): The username path parameter.
        library_id (str): ID of the library to get collections from.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The collections feed for the specified library.
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    return await collection_feed.generate_collections_feed(
        effective_username,
        library_id,
        token=token
    )


@app.get("/opds/{username}/libraries/{library_id}/collections/{collection_id}")
async def opds_collection_items(
    username: str,
    library_id: str,
    collection_id: str,
    request: Request,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get items from a specific collection using cached data when possible.

    Args:
        username (str): The username path parameter.
        library_id (str): ID of the library to get items from.
        collection_id (str): ID of the collection to filter by.
        request (Request): The incoming request object containing pagination parameters.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The items feed for books in the specified collection.

    Query Parameters:
        start_index (int): Starting index for pagination (1-based).
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    return await collection_feed.generate_collection_items_feed(
        effective_username,
        library_id,
        collection_id,
        token=token,
        params=dict(request.query_params)
    )


@app.get("/opds/{username}/libraries/{library_id}/authors")
async def opds_authors(
    username: str,
    library_id: str,
    request: Request,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get authors from a specific library.

    Args:
        username (str): The username path parameter.
        library_id (str): ID of the library to get authors from.
        request (Request): The incoming request object containing pagination parameters.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The authors feed for the specified library.

    Query Parameters:
        start_index (int): Starting index for pagination (1-based).
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    return await author_feed.generate_authors_feed(
        effective_username,
        library_id,
        token=token,
        params=dict(request.query_params)
    )


@app.get("/opds/{username}/libraries/{library_id}/authors/{author_id}")
async def opds_author_items(
    username: str,
    library_id: str,
    author_id: str,
    request: Request,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Get items from a specific author using cached data when possible.

    Args:
        username (str): The username path parameter.
        library_id (str): ID of the library to get items from.
        author_id (str): ID of the author to filter by.
        request (Request): The incoming request object containing pagination parameters.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        Response: The items feed for books by the specified author.

    Query Parameters:
        start_index (int): Starting index for pagination (1-based).
    """
    auth_username, token, display_name = auth_info

    redirect = _canonical_user_redirect(request, username, auth_info)
    if redirect:
        return redirect

    # Use the display name from authentication if available
    effective_username = display_name if auth_username else username

    return await author_feed.generate_author_items_feed(
        effective_username,
        library_id,
        author_id,
        token=token,
        params=dict(request.query_params)
    )


@app.get("/admin/cache/stats")
async def get_cache_stats(auth_info: tuple = Depends(require_admin)):
    """Cache statistics (ABS admin/root only): entry count, ages, rough size."""
    now = time.time()
    entries = [
        {
            "key": key[:8] + "...",  # truncated: keys are hashes of user-specific requests
            "age_seconds": int(now - timestamp),
            "size_estimate": len(str(data)),
        }
        for key, (timestamp, data) in list(_cache.items())
    ]
    stats = {"total_entries": len(entries), "entries": entries}
    if entries:
        ages = [e["age_seconds"] for e in entries]
        stats.update(
            oldest_entry_age=max(ages),
            newest_entry_age=min(ages),
            average_entry_age=sum(ages) / len(ages),
            total_size_estimate=sum(e["size_estimate"] for e in entries),
        )
    return JSONResponse(content=stats)


@app.post("/admin/cache/clear")
async def clear_all_cache(auth_info: tuple = Depends(require_admin)):
    """Clear the data cache (ABS admin/root only)."""
    count = clear_cache()
    logger.info("Cache cleared by %s: %d entries", auth_info[0], count)
    return JSONResponse(content={"message": f"Cleared {count} items from cache"})


_ABS_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _recover_header_text(value: str) -> str:
    """Undo aiohttp's decoding of a raw header so non-ASCII text reads correctly.

    aiohttp decodes header bytes as UTF-8 with ``surrogateescape``. Older ABS
    (Node) wrote filenames as raw latin-1, so "Müller" arrived as "M\\udcfcller"
    and became "M\\ufffdller" (upstream issue #42). Recover the bytes, then
    decode as UTF-8 if valid, else latin-1.
    """
    raw = value.encode("utf-8", "surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _content_disposition(upstream_value: str, fallback: str) -> str:
    """Build a Content-Disposition that survives non-ASCII filenames.

    HTTP headers are latin-1 on the wire, so passing ABS's header through with a
    filename like "Märchen.epub" raised UnicodeEncodeError and the download
    500ed. Emit an ASCII ``filename`` fallback plus RFC 5987 ``filename*``.
    The upstream value is parsed with the stdlib MIME parser, which handles
    quoted names containing ';' and RFC 2231 ``filename*``; a regex cut
    "foo;bar.epub" down to "foo".
    """
    msg = Message()
    msg["content-disposition"] = _recover_header_text(upstream_value or "")
    # get_params decodes RFC 2231 `filename*` into a (charset, lang, value) tuple
    # listed under "filename" beside the plain one. Prefer the tuple. DO NOT use
    # msg.get_filename(): it returns the plain `filename`, which current ABS fills
    # with an ASCII fallback ("Ranger?s").
    candidates = [v for k, v in (msg.get_params(header="content-disposition") or [])
                  if k.lower() == "filename"]
    extended = [v for v in candidates if isinstance(v, tuple)]
    if extended:
        name = collapse_rfc2231_value(extended[0])
    else:
        name = collapse_rfc2231_value(candidates[0]) if candidates else ""
    name = name.replace("\r", "").replace("\n", "").strip() or fallback
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'["\\\r\n]', "", ascii_name).strip() or fallback
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


@app.get("/opds/proxy/download/{item_id}/file/{file_ino}")
async def proxy_download(
    item_id: str,
    file_ino: str,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Stream an ebook file from Audiobookshelf using the caller's ABS token.

    Acquisition links point here so clients only ever hold their OPDS (Basic)
    credentials, never an ABS token.

    One upstream GET whose status is checked BEFORE the response starts: errors
    raised from inside a streaming body arrive after a 200 has been sent and
    show up on the reader as a truncated or corrupt book.
    """
    if not (_ABS_ID_RE.match(item_id) and _ABS_ID_RE.match(file_ino)):
        raise HTTPException(status_code=404, detail="Not found")

    username, token, _ = auth_info
    url = f"{AUDIOBOOKSHELF_API}/items/{item_id}/file/{file_ino}/download"
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60))
    try:
        upstream = await session.get(url, headers=headers)
    except (aiohttp.ClientError, TimeoutError) as e:
        await session.close()
        logger.error("Download item=%s file=%s: cannot reach Audiobookshelf: %s", item_id, file_ino, e)
        raise HTTPException(status_code=503, detail="Audiobookshelf server is unavailable") from None
    except BaseException:
        # Client went away (cancellation) while waiting for ABS's headers.
        await session.close()
        raise

    if upstream.status != 200:
        upstream.release()
        await session.close()
        logger.warning("Download item=%s file=%s user=%s: Audiobookshelf returned HTTP %d",
                       item_id, file_ino, username, upstream.status)
        if upstream.status in (401, 403):
            if token:
                invalidate_token(token)
            raise HTTPException(status_code=401, detail="Authentication required",
                                headers=CHALLENGE_HEADERS)
        if upstream.status == 404:
            raise HTTPException(status_code=404, detail="File not found")
        raise HTTPException(status_code=502, detail="Audiobookshelf download failed")

    response_headers = {
        "Content-Disposition": _content_disposition(
            upstream.headers.get("Content-Disposition", ""), f"book-{item_id}.epub"),
    }
    if upstream.headers.get("Content-Length"):
        response_headers["Content-Length"] = upstream.headers["Content-Length"]
    media_type = upstream.headers.get("Content-Type", "application/octet-stream")
    logger.info("Download item=%s file=%s user=%s type=%s bytes=%s",
                item_id, file_ino, username, media_type,
                upstream.headers.get("Content-Length", "?"))

    async def body():
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(body(), media_type=media_type, headers=response_headers)
