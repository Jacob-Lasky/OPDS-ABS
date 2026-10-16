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
import re
import unicodedata
from urllib.parse import quote, unquote
from contextlib import asynccontextmanager


# Third-party imports
import aiohttp
from lxml import etree
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.exception_handlers import http_exception_handler

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
from opds_abs.api.client import invalidate_cache
from opds_abs.utils.auth_utils import CHALLENGE_HEADERS, get_authenticated_user, invalidate_token
from opds_abs.utils.logging_utils import AccessLogMiddleware, configure_logging
from opds_abs.utils.error_utils import (
    OPDSBaseException,
    ResourceNotFoundError,
    CacheError,
    handle_exception,
    convert_to_http_exception,
    log_error,
    APIClientError,
    AuthenticationError
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
    # Log configuration settings
    logger.info("Starting OPDS-ABS with configuration:")
    logger.info(f"Audiobookshelf URL: {AUDIOBOOKSHELF_INTERNAL_URL}")
    logger.info(f"Audiobookshelf Internal URL: {AUDIOBOOKSHELF_INTERNAL_URL}")
    logger.info(f"Audiobookshelf External URL: {AUDIOBOOKSHELF_EXTERNAL_URL}")
    logger.info(f"Authentication Enabled: {AUTH_ENABLED}")
    logger.info(f"API Key Authentication Enabled: {API_KEY_AUTH_ENABLED}")
    if not API_KEY_AUTH_ENABLED:
        logger.warning("API Key Authentication is DISABLED. Only username/password will work.")
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
    """Save the cache to disk on application shutdown."""
    if CACHE_PERSISTENCE_ENABLED:
        logger.info("Saving cache to disk...")
        save_cache_to_disk()

# Create FastAPI app
app = FastAPI(
    title="OPDS-ABS",
    description="OPDS server for Audiobookshelf",
    version="0.1.0",
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
    atexit.register(save_cache_to_disk)

# Custom exception handler for OPDS exceptions
@app.exception_handler(OPDSBaseException)
async def opds_exception_handler(request: Request, exc: OPDSBaseException):
    """Handle custom OPDS exceptions using our error handling utilities.

    Args:
        request (Request): The request that caused the exception
        exc (OPDSBaseException): The exception that was raised

    Returns:
        Response: A standardized error response
    """
    context = f"{request.method} {request.url.path}"
    return handle_exception(exc, context=context)

# Custom exception handler for API connectivity errors
@app.exception_handler(APIClientError)
async def api_client_error_handler(request: Request, exc: APIClientError):
    """Handle API connectivity errors gracefully with OPDS-compliant XML error format.

    This exception handler is specifically designed to handle connection errors
    to the Audiobookshelf API in a user-friendly way that's compatible with OPDS clients.

    Args:
        request (Request): The request that caused the exception
        exc (APIClientError): The exception that was raised

    Returns:
        Response: OPDS-compliant XML error response
    """
    # For connection errors, no need to log the full traceback again
    # since it was already logged when the exception was raised
    error_id = id(exc)
    logger.warning("API connection error [%s]: %s", error_id, str(exc))

    # Get the context from the request path
    context = f"{request.method} {request.url.path}"

    # Extract the username and library_id from the request path if possible
    path_parts = request.url.path.split('/')
    username = None
    library_id = None

    for i, part in enumerate(path_parts):
        if part == "opds" and i+1 < len(path_parts):
            username = path_parts[i+1]
        if part == "libraries" and i+1 < len(path_parts):
            library_id = path_parts[i+1]

    # Create a more specific context if we have the username and library_id
    if username and library_id:
        context = f"Generating items feed for user {username}, library {library_id}"

    # Create OPDS-compliant XML error response
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<error xmlns="http://opds-spec.org/2010/catalog">
  <id>{error_id}</id>
  <message>{str(exc)}</message>
  <context>{context}</context>
</error>"""

    # Return the XML response with proper content type
    return Response(
        content=xml_content,
        media_type="application/xml",
        status_code=503  # Service Unavailable
    )

# Custom exception handler for authentication errors
@app.exception_handler(AuthenticationError)
async def authentication_error_handler(request: Request, exc: AuthenticationError):
    """Handle authentication errors with OPDS-compliant XML error format.

    Args:
        request (Request): The request that caused the exception
        exc (AuthenticationError): The authentication error that was raised

    Returns:
        Response: OPDS-compliant XML error response
    """
    # Log authentication errors without traceback
    error_id = id(exc)
    logger.warning("Authentication error [%s]: %s", error_id, str(exc))

    # Get the context from the request path
    context = f"{request.method} {request.url.path}"

    # Extract the username and library_id from the request path if possible
    path_parts = request.url.path.split('/')
    username = None
    library_id = None

    for i, part in enumerate(path_parts):
        if part == "opds" and i+1 < len(path_parts):
            username = path_parts[i+1]
        if part == "libraries" and i+1 < len(path_parts):
            library_id = path_parts[i+1]

    # Create a more specific context if we have the username and library_id
    if username and library_id:
        context = f"Authentication for user {username}, library {library_id}"
    elif username:
        context = f"Authentication for user {username}"

    # Create OPDS-compliant XML error response
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<error xmlns="http://opds-spec.org/2010/catalog">
  <id>{error_id}</id>
  <message>{str(exc)}</message>
  <context>{context}</context>
</error>"""

    # Return the XML response with proper content type and WWW-Authenticate header
    return Response(
        content=xml_content,
        media_type="application/xml",
        status_code=401,
        headers={"WWW-Authenticate": "Basic realm=\"OPDS-ABS\""}
    )

# Custom exception handler for 503 Service Unavailable errors
@app.exception_handler(503)
async def service_unavailable_handler(request: Request, exc: HTTPException):
    """Handle Service Unavailable errors with OPDS-compliant XML format.

    This is specifically for when the Audiobookshelf server is unavailable.

    Args:
        request (Request): The request that caused the exception
        exc (HTTPException): The HTTP exception with status code 503

    Returns:
        Response: OPDS-compliant XML error response
    """
    error_id = id(exc)
    logger.warning("Service unavailable [%s]: %s", error_id, str(exc.detail))

    # Get the context from the request path
    context = f"{request.method} {request.url.path}"

    # Create OPDS-compliant XML error response
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<error xmlns="http://opds-spec.org/2010/catalog">
  <id>{error_id}</id>
  <message>Audiobookshelf server is unavailable: {exc.detail}</message>
  <context>{context}</context>
</error>"""

    # Return the XML response with proper content type
    return Response(
        content=xml_content,
        media_type="application/xml",
        status_code=503  # Service Unavailable
    )

# Fall back to standard HTTPException handling for other exceptions
@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    """Custom handler for HTTPExceptions that logs them before handling.

    Args:
        request (Request): The request that caused the exception
        exc (HTTPException): The exception that was raised

    Returns:
        Response: The standard FastAPI HTTP exception response
    """
    context = f"{request.method} {request.url.path}"
    log_error(exc, context=context, log_traceback=False)
    return await http_exception_handler(request, exc)

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
    segments = request.url.path.split("/")
    segments[2] = display_name  # ["", "opds", <username>, ...]
    url = "/".join(quote(seg, safe="") for seg in segments)
    if request.url.query:
        url += f"?{request.url.query}"
    return RedirectResponse(url=url)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Render the index page.

    Args:
        request (Request): The incoming request object.

    Returns:
        HTMLResponse: The rendered index.html template.
    """
    try:
        return templates.TemplateResponse("index.html", {"request": request})
    except Exception as e:
        log_error(e, context="Rendering index page")
        raise convert_to_http_exception(e, status_code=500,
            detail="Failed to render index page") from e


@app.get("/opds", response_class=RedirectResponse)
async def opds_root_redirect(
    request: Request,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Redirect to the authenticated user's OPDS root.

    Args:
        request (Request): The incoming request.
        auth_info (tuple): The authentication info (username, token, display_name).

    Returns:
        RedirectResponse: Redirect to the user's OPDS root.
    """
    username, token, display_name = auth_info

    if not username:
        # Check if authentication was disabled or failed because server is unavailable
        if not AUTH_ENABLED:
            # Authentication is disabled, so use a default username
            return RedirectResponse(url=f"/opds/anonymous")
        else:
            # If not authenticated, return a 401 with WWW-Authenticate header
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic realm=\"OPDS-ABS\""}
            )

    # Redirect to the user's OPDS root
    return RedirectResponse(url=f"/opds/{display_name}")


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
    search_url = request.url_for(
        "opds_search", username=username, library_id=library_id)
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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating root feed for user {username}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating navigation feed for user {username}, library {library_id}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Searching in library {library_id} for user {username}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating items feed for user {username}, library {library_id}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating series feed for user {username}, library {library_id}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating series items feed for user {username}, library {library_id}, series {series_id}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating collections feed for user {username}, library {library_id}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating collection items feed for user {username}, library {library_id}, collection {collection_id}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating authors feed for user {username}, library {library_id}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


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
    try:
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
    except ResourceNotFoundError as e:
        # ResourceNotFoundError is already properly handled in the feed generator
        raise
    except Exception as e:
        context = f"Generating author items feed for user {username}, library {library_id}, author {author_id}"
        log_error(e, context=context)
        return handle_exception(e, context=context)


@app.get("/admin/cache/stats")
async def get_cache_stats(auth_info: tuple = Depends(get_authenticated_user)):
    """Get statistics about the cache.

    Returns:
        JSONResponse: Statistics about the cache including entry count,
                      age information, and estimated size.
    """
    try:
        # Import here to ensure we're using the same _cache instance
        from opds_abs.utils.cache_utils import _cache as current_cache

        now = time.time()
        stats = {
            "total_entries": len(current_cache),
            "entries": []
        }

        # Calculate memory usage and age of each entry
        for key, (timestamp, data) in current_cache.items():
            age = int(now - timestamp)
            stats["entries"].append({
                "key": key[:8] + "...",  # Show truncated key for privacy
                "age_seconds": age,
                "age_minutes": round(age / 60, 1),
                "size_estimate": len(str(data))  # Rough size estimate
            })

        # Add summary stats
        if stats["entries"]:
            stats["oldest_entry_age"] = max(entry["age_seconds"] for entry in stats["entries"])
            stats["newest_entry_age"] = min(entry["age_seconds"] for entry in stats["entries"])
            stats["average_entry_age"] = sum(entry["age_seconds"] for entry in stats["entries"]) / len(stats["entries"])
            stats["total_size_estimate"] = sum(entry["size_estimate"] for entry in stats["entries"])

        return JSONResponse(content=stats)
    except Exception as e:
        log_error(e, context="Getting cache statistics")
        raise convert_to_http_exception(e, status_code=500,
            detail="Error retrieving cache statistics") from e


@app.post("/admin/cache/clear")
async def clear_all_cache(auth_info: tuple = Depends(get_authenticated_user)):
    """Clear all items in the cache.

    Returns:
        JSONResponse: A message indicating how many items were cleared from the cache.
    """
    try:
        # Use the count returned by clear_cache() instead of checking beforehand
        count = clear_cache()
        return JSONResponse(content={"message": f"Cleared {count} items from cache"})
    except Exception as e:
        log_error(e, context="Clearing cache")
        raise CacheError("Failed to clear cache") from e


@app.post("/admin/cache/invalidate")
async def invalidate_specific_cache(
    endpoint: str = None,
    username: str = None,
    auth_info: tuple = Depends(get_authenticated_user)
):
    """Invalidate cache for a specific endpoint.

    Args:
        endpoint (str, optional): The endpoint to invalidate cache for. Required.
        username (str, optional): The username to invalidate cache for. Defaults to None.

    Returns:
        JSONResponse: A message indicating the result of the cache invalidation.

    Raises:
        ResourceNotFoundError: If no matching cache entry was found.
    """
    if not endpoint:
        raise ResourceNotFoundError("Endpoint parameter is required")

    try:
        success = invalidate_cache(endpoint, username=username)

        if success:
            return JSONResponse(content={"message": f"Invalidated cache for {endpoint}"})
        raise ResourceNotFoundError(f"No cache found for {endpoint}")
    except ResourceNotFoundError:
        # Re-raise ResourceNotFoundError
        raise
    except Exception as e:
        log_error(e, context=f"Invalidating cache for {endpoint}")
        raise CacheError(f"Failed to invalidate cache for {endpoint}") from e


_ABS_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _content_disposition(upstream_value: str, fallback: str) -> str:
    """Build a Content-Disposition that survives non-ASCII filenames.

    HTTP headers are latin-1 on the wire, so passing ABS's header through with a
    filename like "Märchen.epub" raised UnicodeEncodeError and the download
    500ed. Emit an ASCII ``filename`` fallback plus RFC 5987 ``filename*``.
    """
    name = None
    star = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", upstream_value or "")
    if star:
        name = unquote(star.group(1).strip())
    else:
        plain = re.search(r'filename\s*=\s*"?([^";]+)"?', upstream_value or "")
        if plain:
            name = plain.group(1).strip()
    name = name or fallback
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
