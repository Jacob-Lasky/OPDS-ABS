"""Collections feed generator."""
# Standard library imports
import logging
import asyncio

# Local application imports
from opds_abs.core.feed_generator import BaseFeedGenerator
from opds_abs.api.client import fetch_from_api, get_download_urls_from_item
from opds_abs.config import AUDIOBOOKSHELF_EXTERNAL_API, ITEMS_PER_PAGE, PAGINATION_ENABLED
from opds_abs.utils import dict_to_xml
from opds_abs.utils.cache_utils import get_cached_library_items
from opds_abs.utils.error_utils import (
    AuthenticationError,
    FeedGenerationError,
    ResourceNotFoundError,
    OPDSBaseException,
    log_error,
)

# Set up logging
logger = logging.getLogger(__name__)

class CollectionFeedGenerator(BaseFeedGenerator):
    """Generator for collections feed.

    This class creates OPDS feeds that list collections from an Audiobookshelf library and
    the books contained within specific collections. It includes methods for fetching
    collection details, filtering items by collection, and generating collection-based feeds.

    Attributes:
        Inherits all attributes from BaseFeedGenerator.
    """

    async def get_collection_details(self, username, collection_id, token=None):
        """Fetch a collection from Audiobookshelf.

        Raises:
            ResourceNotFoundError: The collection does not exist.
        """
        return await fetch_from_api(f"/collections/{collection_id}", username=username, token=token)

    async def filter_items_by_collection_id(self, username, library_id, collection_id, token=None):
        """Return the library's ebook items that belong to a collection.

        Uses the cached all-items list, so collection pages need one collection
        fetch rather than one item fetch per book.

        DO NOT add a fallback to ``/libraries/{id}/items?collection=``: ABS has no
        such filter and ignores it, so the "fallback" listed the whole library
        under the collection's name. Errors propagate and become an error
        response instead.
        """
        collection_details = await self.get_collection_details(username, collection_id, token=token)
        collection_book_ids = {
            book.get("id") for book in collection_details.get("books", []) if book.get("id")
        }
        if not collection_book_ids:
            return []

        library_items = await get_cached_library_items(
            fetch_from_api, self.filter_items, username, library_id, token=token)
        filtered_items = [item for item in library_items if item.get("id") in collection_book_ids]
        logger.debug("Collection %s: %d of %d books have ebooks",
                     collection_details.get("name"), len(filtered_items), len(collection_book_ids))
        return filtered_items

    async def generate_collection_items_feed(self, username, library_id, collection_id, token=None,
                                             params=None):
        """Generate a feed of items in a specific collection.

        Paginates with the same 1-based ``start_index`` scheme as the library items
        feed. DO NOT reintroduce a ``page=`` query parameter here: the OPDS routes
        forward raw query params and the shared ``add_pagination_links`` helper emits
        ``start_index``, so a ``page=`` parameter is never read back and the feed
        silently pins to the first page forever.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library to generate the feed for.
            collection_id (str): The ID of the collection to filter by.
            token (str, optional): Authentication token for Audiobookshelf.
            params (dict, optional): Raw query parameters; ``start_index`` is honored.

        Returns:
            Response: A FastAPI response object containing the XML feed.
        """
        try:
            params = params if params else {}

            # Extract pagination parameters (1-based, OpenSearch style)
            try:
                start_index = int(params.get('start_index', 1))
                if start_index < 1:
                    start_index = 1
            except (ValueError, TypeError):
                start_index = 1

            logger.debug("Generating collection items feed for collection %s (start_index %d)",
                        collection_id, start_index)

            # Get collection details first to get the name
            collection_info = await self.get_collection_details(
                    username,
                    collection_id,
                    token=token
            )
            collection_name = collection_info.get("name", "Unknown Collection")

            # Get items in the collection
            collection_items = await self.get_items_in_collection(
                    username,
                    library_id,
                    collection_id,
                    token=token
            )

            # Honor the global pagination config, exactly like the library items feed
            if not PAGINATION_ENABLED:
                items_per_page = 0
            else:
                items_per_page = ITEMS_PER_PAGE
            no_pagination = (items_per_page <= 0)

            # Current page calculation (1-based)
            page = 1 if no_pagination else ((start_index - 1) // items_per_page) + 1

            # Path used for the self/alternate links and the pagination links
            current_path = f"{username}/libraries/{library_id}/collections/{collection_id}"
            current_path_with_page = (current_path if no_pagination
                                      else f"{current_path}?start_index={start_index}")

            # Create the feed
            feed = self.create_base_feed(username, library_id, current_path_with_page, token)

            # Build the feed metadata
            feed_data = {
                "id": {"_text": f"{library_id}/collections/{collection_id}"},
                "title": {"_text": collection_name}
            }

            # Convert feed metadata to XML
            dict_to_xml(feed, feed_data)

            if not collection_items:
                error_data = {
                    "entry": {
                        "title": {"_text": "No books found"},
                        "content": {"_text": f"No books in the {collection_name} collection with ebooks were found in this library."}
                    }
                }
                dict_to_xml(feed, error_data)
                return self.create_response(feed)

            # Get total count before pagination
            total_books = len(collection_items)

            # Apply pagination
            if no_pagination:
                paged_items = collection_items
            else:
                paged_items = self.paginate_results(collection_items, start_index, items_per_page)

            # Add pagination metadata and links
            if not no_pagination:
                self.add_pagination_metadata(feed, page, items_per_page, total_books)
                self.add_pagination_links(feed, current_path.rstrip('&?'),
                                          page, items_per_page, total_books, token=token)

            # Get ebook files in optimal batch sizes to avoid overwhelming the server
            BATCH_SIZE = 5  # Adjust based on server capacity
            tasks = []

            for book in paged_items:
                book_id = book.get("id", "")
                if book_id:
                    tasks.append(get_download_urls_from_item(book_id, username=username, token=token))

            # Process in batches if we have a lot of books
            for i in range(0, len(tasks), BATCH_SIZE):
                batch_tasks = tasks[i:i+BATCH_SIZE]
                batch_results = await asyncio.gather(*batch_tasks)

                # Add each book from this batch to the feed
                for j, ebook_info in enumerate(batch_results):
                    book_index = i + j
                    if book_index < len(paged_items):
                        self.add_book_to_feed(feed, paged_items[book_index], ebook_info, "", token)

            return self.create_response(feed)

        except OPDSBaseException:
            raise
        except Exception as e:
            raise FeedGenerationError(f"Generating collection items feed for collection {collection_id}" + f": {e}") from e

    def add_collection_to_feed(self, username, library_id, feed, collection, token=None):
        """Add a collection to the feed.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library.
            feed (Element): The XML feed element to add the collection to.
            collection (dict): The collection data.
            token (str, optional): Authentication token to include in links.

        Raises:
            FeedGenerationError: If there's an error adding the collection to the feed.
        """
        try:
            # Default cover and collection details
            cover_url = "/static/images/collections.png"
            collection_id = collection.get("id", "")
            collection_name = collection.get("name", "Unknown collection name")

            # Use list comprehension for more efficient filtering of books with ebooks
            books_with_ebooks = [
                book for book in collection.get("books", [])
                if (book.get("media", {}).get("ebookFile") is not None or
                   (book.get("media", {}).get("ebookFormat") is not None and book.get("media", {}).get("ebookFormat")))
            ]

            # Get the book count for the entry content
            book_count = len(books_with_ebooks)

            # Find a cover image from the first book with an ID
            if books_with_ebooks:
                for book in books_with_ebooks:
                    book_id = book.get("id")
                    if book_id:
                        book_path = f"{AUDIOBOOKSHELF_EXTERNAL_API}/items/{book_id}"
                        cover_url = f"{book_path}/cover?format=jpeg"
                        break

            collection_link = f"/opds/{username}/libraries/{library_id}/collections/{collection_id}"

            # Create entry data structure
            entry_data = {
                "entry": {
                    "title": {"_text": collection_name},
                    "id": {"_text": collection_id},
                    "updated": {"_text": self.get_current_timestamp()},
                    "content": {"_text": f"Collection with {book_count} ebook{'s' if book_count != 1 else ''}"},
                    "link": [
                        {
                            "_attrs": {
                                "href": collection_link,
                                "rel": "subsection",
                                "type": "application/atom+xml;profile=opds-catalog"
                            }
                        },
                        {
                            "_attrs": {
                                "href": cover_url,
                                "rel": "http://opds-spec.org/image",
                                "type": self.image_type(cover_url)
                            }
                        }
                    ]
                }
            }

            # Convert dictionary to XML elements
            dict_to_xml(feed, entry_data)

        except (ValueError, KeyError) as e:
            context = f"Adding collection {collection.get('name', 'unknown')} to feed"
            log_error(e, context=context)
            raise FeedGenerationError(f"Failed to add collection to feed: {str(e)}") from e
        except Exception as e:
            context = f"Adding collection {collection.get('name', 'unknown')} to feed"
            log_error(e, context=context)
            raise FeedGenerationError(f"Unexpected error adding collection to feed: {str(e)}") from e

    async def generate_collections_feed(self, username, library_id, token=None):
        """Generate an OPDS feed listing all collections in a library.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library to generate the feed for.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            Response: A FastAPI response object containing the XML feed.
        """
        try:
            logger.debug("Generating collections feed for user %s, library %s",
                        username,
                        library_id
            )

            # Get all collections from the API
            collections = await self.get_collections(username, library_id, token=token)

            # Create the feed
            feed = self.create_base_feed(username, library_id, token=token)

            # Build the feed metadata
            feed_data = {
                "id": {"_text": f"{library_id}/collections"},
                "title": {"_text": f"Collections in {username}'s library"}
            }
            dict_to_xml(feed, feed_data)

            # Filter collections to only include those with books that have ebook files
            filtered_collections = []
            collection_errors = []

            # Fetch every collection's books concurrently (was one at a time).
            ids = [c.get("id") for c in collections if c.get("id")]
            results = await asyncio.gather(
                *(fetch_from_api(f"/collections/{cid}", username=username, token=token) for cid in ids),
                return_exceptions=True)
            for collection_id, collection_data in zip(ids, results):
                if isinstance(collection_data, AuthenticationError):
                    raise collection_data
                if isinstance(collection_data, Exception):
                    logger.warning("Skipping collection %s: %s", collection_id, collection_data)
                    collection_errors.append(f"Collection {collection_id}: {collection_data}")
                    continue
                ebook_count = sum(1 for book in collection_data.get("books", [])
                                  if self._book_has_ebook(book))
                if ebook_count:
                    filtered_collections.append(collection_data)

            # Sort collections by name
            filtered_collections = sorted(filtered_collections, key=lambda x: x.get("name", "").lower())

            # Add each collection to the feed
            for collection in filtered_collections:
                self.add_collection_to_feed(username, library_id, feed, collection, token)

            # If we have no collections but had errors, add an entry about the errors
            if not filtered_collections and collection_errors:
                error_message = "Some collections could not be retrieved:\n" + "\n".join(collection_errors)
                error_data = {
                    "entry": {
                        "title": {"_text": "Error retrieving some collections"},
                        "content": {"_text": error_message}
                    }
                }
                dict_to_xml(feed, error_data)

            return self.create_response(feed)

        except ResourceNotFoundError as e:
            context = f"Generating collections feed for library {library_id}"
            log_error(e, context=context, log_traceback=False)

            # Return a feed with the specific error
            feed = self.create_base_feed(username, library_id, token=token)
            error_data = {
                "title": {"_text": "Collections not found"},
                "entry": {
                    "content": {"_text": str(e)}
                }
            }
            dict_to_xml(feed, error_data)
            return self.create_response(feed)
        except OPDSBaseException:
            raise
        except Exception as e:
            raise FeedGenerationError(f"Generating collections feed for user {username}, library {library_id}" + f": {e}") from e

    @staticmethod
    def _book_has_ebook(book):
        media = book.get("media", {})
        return media.get("ebookFile") is not None or bool(media.get("ebookFormat"))

    async def get_collections(self, username, library_id, token=None):
        """Fetch all collections available in a library.

        Args:
            username (str): The username of the authenticated user.
            library_id (str): ID of the library to get collections from.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            list: All collections in the library.

        Raises:
            ResourceNotFoundError: If the library or collections cannot be found.
        """
        try:
            # Make an API call to get all collections
            collections_data = await fetch_from_api(
                f"/libraries/{library_id}/collections",
                {"limit": 1000, "sort": "name"},
                username=username,
                token=token
            )

            # Check if we got any collections
            collections = collections_data.get("results", [])

            if not collections:
                logger.info("No collections in library %s", library_id)
            else:
                logger.debug("Found %d collections in library %s", len(collections), library_id)

            return collections

        except Exception as e:
            logger.error("Error fetching collections: %s", e)
            if "not found" in str(e).lower():
                raise ResourceNotFoundError(f"Collections not found for library {library_id}")
            raise

    async def get_items_in_collection(self, username, library_id, collection_id, token=None):
        """Get items in a specific collection with ebooks.

        Args:
            username (str): The username of the authenticated user.
            library_id (str): ID of the library containing the collection.
            collection_id (str): ID of the collection to get items from.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            list: Items in the collection that have ebooks.
        """
        # Use the existing filter_items_by_collection_id method to get the items
        return await self.filter_items_by_collection_id(
            username,
            library_id,
            collection_id,
            token=token
        )
