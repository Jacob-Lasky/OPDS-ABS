"""Caching utilities for API responses.

This module provides functions for caching API responses and other data
to reduce API calls and improve application performance. It implements
a simple in-memory cache with time-based expiration and optional
persistence using pickle.
"""
# Standard library imports
import time
import logging
import os
import pickle
import threading
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import hashlib
import json
import base64

# Local application imports
from opds_abs.config import (
    DEFAULT_CACHE_EXPIRY,
    LIBRARY_ITEMS_CACHE_EXPIRY,
    SEARCH_RESULTS_CACHE_EXPIRY,
    SERIES_DETAILS_CACHE_EXPIRY,
    SERIES_ITEMS_CACHE_EXPIRY,  # Add this new constant
    CACHE_PERSISTENCE_ENABLED,
    CACHE_FILE_PATH,
    CACHE_SAVE_INTERVAL,
    AUTHORS_CACHE_EXPIRY
)

logger = logging.getLogger(__name__)

# Cache dictionary: key -> (timestamp, data)
_cache: Dict[str, Tuple[float, Any]] = {}
_last_save_time = 0
_cache_lock = threading.RLock()
# Serialises whole saves (snapshot + write + rename) so an older snapshot can never
# be renamed over a newer one, e.g. a background save landing after a clear.
_save_lock = threading.Lock()
# Entries older than every per-endpoint expiry are dropped when saving.
MAX_CACHE_AGE = max(DEFAULT_CACHE_EXPIRY, LIBRARY_ITEMS_CACHE_EXPIRY, SEARCH_RESULTS_CACHE_EXPIRY,
                    SERIES_DETAILS_CACHE_EXPIRY, SERIES_ITEMS_CACHE_EXPIRY, AUTHORS_CACHE_EXPIRY)


def _create_cache_key(endpoint: str, params: Optional[Dict] = None, username: Optional[str] = None,
                      token: Optional[str] = None) -> str:
    """Hash endpoint, params, username and a token fingerprint into a cache key.

    The token is part of the key, not just the username: keyed on the name alone,
    a re-created account with the same username, or a user whose library access
    was revoked, kept being served the old account's cached catalog. Only a hash
    of the token reaches the key, and keys are hashed again, so no token is ever
    stored in the (persisted) cache.
    """
    token_fp = hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""
    params_str = json.dumps(params, sort_keys=True) if params else "{}"
    key_str = "\0".join([endpoint, params_str, username or "", token_fp])
    return hashlib.sha256(key_str.encode()).hexdigest()


# Bump when the cache layout changes, or when old files must not be trusted.
# v3: keys include a token fingerprint. v2 keys did not; v1 files (a bare dict)
# contained ABS tokens. Older files are discarded on load.
CACHE_FORMAT_VERSION = 3


def load_cache_from_disk() -> None:
    """Load the persisted data cache into memory.

    Mutates ``_cache`` in place. DO NOT rebind it: other modules import the dict
    object, and rebinding left them holding the old empty one.
    """
    if not CACHE_PERSISTENCE_ENABLED:
        return
    cache_path = Path(CACHE_FILE_PATH)
    if not cache_path.exists():
        logger.info("No cache file at %s, starting empty", CACHE_FILE_PATH)
        return
    try:
        with cache_path.open("rb") as f:
            stored = pickle.load(f)
    except (pickle.PickleError, OSError, EOFError, AttributeError) as e:
        logger.warning("Ignoring unreadable cache file %s: %s", CACHE_FILE_PATH, e)
        return
    if not isinstance(stored, dict) or stored.get("version") != CACHE_FORMAT_VERSION:
        logger.warning("Discarding cache file %s written by an older version", CACHE_FILE_PATH)
        cache_path.unlink(missing_ok=True)
        return
    with _cache_lock:
        _cache.clear()
        _cache.update(stored["entries"])
    logger.info("Loaded %d cached items from disk", len(stored["entries"]))


def save_cache_to_disk(force: bool = False) -> None:
    """Persist the data cache, at most every CACHE_SAVE_INTERVAL unless ``force``.

    Copies the dict under the cache lock and pickles the copy outside it, so
    requests are not stalled for the length of the dump, then writes a temp file
    and renames it so a crash mid-write cannot leave a truncated cache. The whole
    sequence holds ``_save_lock``, so concurrent saves (background thread, admin
    clear, shutdown) finish in order and share the temp file safely.
    """
    global _last_save_time

    if not CACHE_PERSISTENCE_ENABLED:
        return
    with _save_lock:
        now = time.time()
        with _cache_lock:
            if not force and now - _last_save_time < CACHE_SAVE_INTERVAL:
                return
            _last_save_time = now
            for key in [k for k, (ts, _) in _cache.items() if now - ts > MAX_CACHE_AGE]:
                del _cache[key]
            snapshot = dict(_cache)

        cache_path = Path(CACHE_FILE_PATH)
        tmp_path = cache_path.with_suffix(".tmp")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp_path.open("wb") as f:
                pickle.dump({"version": CACHE_FORMAT_VERSION, "entries": snapshot}, f,
                            protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_path, cache_path)
            logger.debug("Saved %d cache items to %s", len(snapshot), CACHE_FILE_PATH)
        except (pickle.PickleError, OSError) as e:
            logger.error("Failed to save cache to %s: %s", CACHE_FILE_PATH, e)


def cache_get(key: str, max_age: int = DEFAULT_CACHE_EXPIRY, ignore_expiry: bool = False) -> Optional[Any]:
    """Get an item from the cache if it exists and isn't expired.

    Args:
        key: Cache key
        max_age: Maximum age in seconds for cached item
        ignore_expiry: If True, return the data even if expired (used for fallbacks when server is down)

    Returns:
        The cached data or None if not found or expired
    """
    with _cache_lock:
        if key not in _cache:
            return None

        timestamp, data = _cache[key]
        if not ignore_expiry and time.time() - timestamp > max_age:
            # Cache expired, remove it unless ignore_expiry is True
            del _cache[key]
            return None

        return data


def cache_set(key: str, data: Any) -> None:
    """Store an item in the cache.

    Args:
        key: Cache key
        data: Data to cache
    """
    with _cache_lock:
        _cache[key] = (time.time(), data)

    # Schedule background save if enough time has passed
    if CACHE_PERSISTENCE_ENABLED and time.time() - _last_save_time >= CACHE_SAVE_INTERVAL:
        # Use a thread to save the cache without blocking
        threading.Thread(target=save_cache_to_disk, daemon=True).start()


def clear_cache() -> int:
    """Clear all cached items from memory and disk.

    This function removes all entries from the in-memory cache dictionary,
    effectively invalidating all cached data. If cache persistence is enabled
    in the configuration, it also triggers a save of the empty cache to disk,
    overwriting the previous cache file.

    The function uses a thread lock to ensure thread safety during the operation.

    Returns:
        int: The number of items that were cleared from the cache
    """
    with _cache_lock:
        count = len(_cache)
        _cache.clear()
        logger.info("Cleared %d items from cache", count)

    if CACHE_PERSISTENCE_ENABLED:
        save_cache_to_disk(force=True)

    return count


async def get_cached_library_items(fetch_from_api_func, filter_items_func, username, library_id, token=None, bypass_cache=False):
    """Fetch and cache all library items that can be reused for filtering.

    This method fetches all library items and caches them so they can be
    reused when processing search results instead of making additional API calls.

    Args:
        fetch_from_api_func (callable): The function to fetch data from the API.
        filter_items_func (callable): The function to filter items.
        username (str): The username of the authenticated user.
        library_id (str): ID of the library to fetch items from.
        token (str, optional): Authentication token for Audiobookshelf.
        bypass_cache (bool): Whether to bypass the cache and force a fresh fetch.

    Returns:
        list: All filtered library items containing ebooks.
    """
    cache_key = _create_cache_key(f"/library-items-all/{library_id}", None, username, token)

    # Try to get from cache if not bypassing
    if not bypass_cache:
        cached_data = cache_get(cache_key, LIBRARY_ITEMS_CACHE_EXPIRY)
        if cached_data is not None:
            logger.debug("✓ Cache hit for all library items %s", library_id)
            return cached_data  # Return cached data

    # Not in cache or bypassing cache, fetch the data
    logger.debug("Fetching all library items for library %s", library_id)
    items_params = {"limit": 10000, "expand": "media"}
    data = await fetch_from_api_func(f"/libraries/{library_id}/items", items_params, username=username, token=token)
    library_items = filter_items_func(data)

    # Store in cache for future use
    cache_set(cache_key, library_items)

    return library_items


async def get_cached_search_results(fetch_from_api_func, username, library_id, query, token=None, bypass_cache=False):
    """Fetch and cache search results to avoid repeated API calls.

    Args:
        fetch_from_api_func (callable): The function to fetch data from the API.
        username (str): The username of the authenticated user.
        library_id (str): ID of the library to search in.
        query (str): Search query string.
        token (str, optional): Authentication token for Audiobookshelf.
        bypass_cache (bool): Whether to bypass the cache and force a fresh search.

    Returns:
        dict: Search results from API or cache.
    """
    cache_key = _create_cache_key(f"/libraries/{library_id}/search", {"q": query}, username, token)

    # Try to get from cache if not bypassing
    if not bypass_cache:
        cached_data = cache_get(cache_key, SEARCH_RESULTS_CACHE_EXPIRY)
        if cached_data is not None:
            logger.debug("✓ Cache hit for search query: %s", query)
            return cached_data

    # Not in cache or bypassing cache, perform the search
    logger.debug("Performing search for query: %s", query)
    search_params = {"limit": 2000, "q": query}
    search_data = await fetch_from_api_func(f"/libraries/{library_id}/search", search_params, username=username, token=token)

    # Store in cache for future use
    cache_set(cache_key, search_data)

    return search_data


async def get_cached_series_details(fetch_from_api_func, username, library_id, series_id, token=None):
    """Fetch and cache detailed information about a specific series.

    This method caches series details to avoid redundant API calls when
    the same series information is requested multiple times.

    Args:
        fetch_from_api_func (callable): The function to fetch data from the API.
        username (str): The username of the authenticated user.
        library_id (str): ID of the library containing the series.
        series_id (str): ID of the series to get details for.
        token (str, optional): Authentication token for Audiobookshelf.

    Returns:
        dict: Series details or None if not found.
    """
    # Create a cache key for this specific series
    cache_key = _create_cache_key(f"/series-details/{library_id}/{series_id}", None, username, token)

    # Try to get from cache first
    cached_data = cache_get(cache_key, SERIES_DETAILS_CACHE_EXPIRY)
    if cached_data is not None:
        logger.debug("✓ Cache hit for series details %s", series_id)
        return cached_data

    # Not in cache, fetch the series data
    logger.debug("Fetching series details for series %s", series_id)

    # Get all series to find the one with the matching ID
    try:
        series_params = {"limit": 2000, "sort": "name"}
        data = await fetch_from_api_func(f"/libraries/{library_id}/series", series_params, username=username, token=token)

        # Find the series with the matching ID
        series_details = None
        for series in data.get("results", []):
            if series.get("id") == series_id:
                series_details = series
                break

        # Store in cache for future use if we found details
        if series_details:
            cache_set(cache_key, series_details)

        return series_details
    except Exception as e:
        logger.error("Error fetching series details: %s", e)
        return None


def book_authors(author_name, known_authors):
    """Return the entries of ``known_authors`` that wrote a book with this authorName.

    ``author_name`` is ABS's ", "-joined list. A name that itself contains ", "
    is matched as a delimited run rather than a single segment.
    """
    if not author_name:
        return []
    segments = set(author_name.split(", "))
    padded = f", {author_name}, "
    return [
        a for a in known_authors
        if a.get("id") and a.get("name") and (
            a["name"] in segments or (", " in a["name"] and f", {a['name']}, " in padded))
    ]


async def get_cached_author_details(fetch_func, filter_func, username, library_id, token=None, bypass_cache=False):
    """Fetch and cache author information, focusing on authors who have books with ebook files.

    Args:
        fetch_func (callable): The function to fetch data from the API.
        filter_func (callable): Function to filter items.
        username (str): The username of the authenticated user.
        library_id (str): ID of the library to search in.
        token (str, optional): Authentication token for Audiobookshelf.
        bypass_cache (bool): Whether to bypass cache and force fresh data lookup.

    Returns:
        list: A list of dictionaries containing author information with ebook counts.
    """
    # Create a cache key specifically for authors with ebooks
    cache_key = _create_cache_key(f"/authors-with-ebooks/{library_id}", None, username, token)

    # Try to get from cache first if not bypassing
    if not bypass_cache:
        cached_data = cache_get(cache_key, AUTHORS_CACHE_EXPIRY)
        if cached_data is not None:
            logger.debug("✓ Cache hit for authors with ebooks in library %s", library_id)
            return cached_data

    # Use cached library items instead of fetching directly
    library_items = await get_cached_library_items(
        fetch_func,
        filter_func,
        username,
        library_id,
        token=token
    )

    if not library_items:
        logger.debug("No library items found for library %s", library_id)
        return []

    authors_params = {"limit": 2000, "sort": "name"}
    author_data = await fetch_func(f"/libraries/{library_id}/authors", authors_params, username=username, token=token)
    known_authors = author_data.get("authors", []) if author_data else []

    # ABS list payloads only carry authorName, the book's authors joined with ", ".
    # DO NOT group on that string: a co-written book became one fake author
    # "Alice Smith, Bob Jones" with no id, and appeared under neither real author.
    # Match each of ABS's real authors against the joined string instead.
    authors_with_ebooks = {}
    for item in library_items:
        media = item.get("media", {})
        if media.get("ebookFile") is None and not media.get("ebookFormat"):
            continue
        for author in book_authors(media.get("metadata", {}).get("authorName"), known_authors):
            entry = authors_with_ebooks.setdefault(author["id"], {
                "name": author.get("name"),
                "id": author["id"],
                "imagePath": author.get("imagePath"),
                "ebook_count": 0,
            })
            entry["ebook_count"] += 1

    # Convert to list for the caller
    authors_list = list(authors_with_ebooks.values())

    # Cache the result for future use
    cache_set(cache_key, authors_list)

    logger.debug("Found %d authors with ebooks in library %s", len(authors_list), library_id)
    return authors_list


async def get_cached_series_items(fetch_from_api_func, filter_items_func, username, library_id, series_id, token=None, bypass_cache=False):
    """Fetch and cache items for a specific series by directly querying the items endpoint with series filter.

    This ensures we get the proper sequence information for items in a series.

    Args:
        fetch_from_api_func (callable): The function to fetch data from the API.
        filter_items_func (callable): The function to filter items.
        username (str): The username of the authenticated user.
        library_id (str): ID of the library containing the items.
        series_id (str): ID of the series to filter by.
        token (str, optional): Authentication token for Audiobookshelf.
        bypass_cache (bool): Whether to bypass the cache and force a fresh fetch.

    Returns:
        list: Library items filtered by the specified series ID with sequence information.
    """
    # Create a cache key for this specific series items
    cache_key = _create_cache_key(f"/series-items/{library_id}/{series_id}", None, username, token)

    # Try to get from cache if not bypassing
    if not bypass_cache:
        cached_data = cache_get(cache_key, SERIES_ITEMS_CACHE_EXPIRY)
        if cached_data is not None:
            logger.debug("✓ Cache hit for series items %s", series_id)
            return cached_data

    # Not in cache or bypassing cache, query the items endpoint with series filter
    logger.debug("Fetching items for series %s using direct items query", series_id)

    # Create base64 encoded filter for the series ID
    base64_series_id = base64.b64encode(series_id.encode()).decode()

    # Query items endpoint with the series filter to get proper sequence information
    params = {"filter": f"series.{base64_series_id}", "sort": "media.metadata.series.sequence"}
    data = await fetch_from_api_func(
        f"/libraries/{library_id}/items",
        params,
        username=username,
        token=token
    )

    # Filter items that contain ebooks
    filtered_items = filter_items_func(data)

    # Store in cache for future use
    cache_set(cache_key, filtered_items)

    logger.debug("Found %d items for series %s", len(filtered_items), series_id)
    return filtered_items
