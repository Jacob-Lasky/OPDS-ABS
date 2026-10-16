"""Series feed generator."""
# Standard library imports
import logging
import asyncio

# Local application imports
from opds_abs.core.feed_generator import BaseFeedGenerator
from opds_abs.api.client import fetch_from_api, get_download_urls_from_item
from opds_abs.config import AUDIOBOOKSHELF_EXTERNAL_API
from opds_abs.utils import dict_to_xml
from opds_abs.utils.cache_utils import get_cached_library_items, get_cached_series_details
from opds_abs.utils.error_utils import FeedGenerationError, OPDSBaseException

# Set up logging
logger = logging.getLogger(__name__)

def series_sequence_key(item):
    """Sort key for a book's position in its series.

    ABS sequences are free text: "2", "1.5", "", None, "Book 3". Numeric ones sort
    numerically, everything else after them in text order. DO NOT float() the raw
    value: one unnumbered book made the whole series feed a 500.
    """
    series = item.get('media', {}).get('metadata', {}).get('series') or {}
    if isinstance(series, list):
        series = series[0] if series else {}
    sequence = str(series.get('sequence') or '').strip()
    try:
        return (0, float(sequence), "")
    except ValueError:
        return (1, 0.0, sequence.lower())


class SeriesFeedGenerator(BaseFeedGenerator):
    """Generator for series feed.

    This class creates OPDS feeds that list series from an Audiobookshelf library and
    the books contained within specific series. It includes methods for fetching
    series details, filtering items by series, and generating series-based feeds.

    The class handles both listing all series in a library and displaying the books
    within a specific series, with proper sorting by sequence number when applicable.
    It also includes helper methods to determine the most common author for a series.

    Attributes:
        Inherits all attributes from BaseFeedGenerator.
    """

    def get_most_common_author(self, items):
        """Get the most common author from a list of items.

        Args:
            items (list): List of library items.

        Returns:
            str: Name of the most common author, or "Unknown Author" if not found.
        """
        # Collect all authors from all items
        all_authors = []

        for item in items:
            media = item.get("media", {})
            metadata = media.get("metadata", {})

            authors = metadata.get("authors", [])
            if authors:
                # Add each author to our list
                for author in authors:
                    author_name = author.get("name", "")
                    if author_name:
                        all_authors.append(author_name)

        if not all_authors:
            return "Unknown Author"

        # Count occurrences of each author
        author_counts = {}
        for author in all_authors:
            if author in author_counts:
                author_counts[author] += 1
            else:
                author_counts[author] = 1

        # Find the most common author
        most_common_author = max(
                author_counts.items(),
                key=lambda x: x[1],
                default=("Unknown Author", 0)
        )

        return most_common_author[0]

    async def generate_series_items_feed(self, username, library_id, series_id, token=None):
        """Generate a feed of items in a specific series.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library to generate the feed for.
            series_id (str): The ID of the series to filter by.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            Response: A FastAPI response object containing the XML feed.
        """
        from opds_abs.utils.cache_utils import get_cached_series_items

        try:
            # Get series details for name and author (still useful for feed metadata)
            series_details = await get_cached_series_details(
                fetch_from_api,
                username,
                library_id,
                series_id,
                token=token
            )

            # Get items for this series directly from items endpoint with series filter
            # This ensures we get the proper sequence information
            library_items = await get_cached_series_items(
                fetch_from_api,
                self.filter_items,
                username,
                library_id,
                series_id,
                token=token
            )

            # Get details for series and author
            series_name = "Unknown Series"
            author_name = "Unknown Author"
            if series_details:
                series_name = series_details.get("name", "Unknown Series")

            # Get the most common author if we have library items
            if library_items:
                author_name = self.get_most_common_author(library_items)
            elif series_details:
                # Fall back to series_details if we have it
                author_name = series_details.get("authorName", "Unknown Author")

            # Create the feed
            feed = self.create_base_feed(username, library_id, token=token)

            # Build the feed metadata
            feed_data = {
                "id": {"_text": f"{library_id}/series/{series_id}"},
                "author": {
                    "name": {"_text": "OPDS Audiobookshelf"}
                },
                "title": {"_text": f"{series_name} Series by {author_name}"}
            }

            # Convert feed metadata to XML
            dict_to_xml(feed, feed_data)

            if not library_items:
                error_data = {
                    "entry": {
                        "title": {"_text": "No books found"},
                        "content": {"_text": f"No ebooks were found in {series_name}."}
                    }
                }
                dict_to_xml(feed, error_data)
                return self.create_response(feed)

            # Items should already be sorted by sequence when fetched from the API
            # But let's ensure they are sorted correctly just to be safe
            sorted_library_items = sorted(library_items, key=series_sequence_key)

            logger.debug("Sorted %d items by sequence number for %s", len(sorted_library_items), series_name)

            # Get ebook files for each book
            tasks = []
            for book in sorted_library_items:
                book_id = book.get("id", "")
                tasks.append(get_download_urls_from_item(book_id, username=username, token=token))

            ebook_inos_list = await asyncio.gather(*tasks)
            for book, ebook_inos in zip(sorted_library_items, ebook_inos_list):
                self.add_book_to_feed(feed, book, ebook_inos, "", token)

            return self.create_response(feed)

        except OPDSBaseException:
            raise
        except Exception as e:
            raise FeedGenerationError(f"Generating series items feed for series {series_id}" + f": {e}") from e

    def filter_series(self, data):
        """Find items in a library series that have an ebook file, sorted by a field.

        Args:
            data (dict): Series data from the Audiobookshelf API containing results list.

        Returns:
            list: Filtered series results that contain at least one book with an ebook file.
        """
        n = 1
        filtered_results = []

        for result in data.get("results", []):
            filtered_books = [
                book for book in result.get("books", [])
                if book.get("media", {}).get("ebookFormat") is not None
            ]

            if filtered_books:
                result.update({"books": filtered_books, "opds_seq": n})
                filtered_results.append(result)

        return filtered_results

    async def add_series_to_feed(self, username, library_id, feed, series, token=None):
        """Add a series to the feed.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library that contains the series.
            feed (Element): The XML element to add the series to.
            series (dict): Series information to add to the feed.
            token (str, optional): Authentication token to include in links.
        """
        first_book = series.get('books', [])[0] if series.get('books') else {}
        first_book_id = first_book.get("id", None)
        first_book_metadata = first_book.get('media', {}).get('metadata', {})
        book_path = f"{AUDIOBOOKSHELF_EXTERNAL_API}/items/{first_book_id}" if first_book_id else ""
        cover_url = f"{book_path}/cover?format=jpeg" if book_path else ""

        # Determine if this was called from search feed by checking if authorName is already set
        # The search feed will directly set authorName, while series feed won't have this property
        from_search_feed = series.get("authorName", None)

        # Get author name with proper fallback chain
        raw_author_name = None

        # Check for the first book in library items if we have an ID
        if first_book_id:
            try:
                # Use cached library items if possible
                library_items = await get_cached_library_items(
                    fetch_from_api,
                    self.filter_items,
                    username,
                    library_id,
                    token=token
                )

                # Find the book in library items
                for item in library_items:
                    if item.get("id") == first_book_id:
                        # Use media.metadata.authorName from the library item
                        raw_author_name = item.get('media', {}).get('metadata', {}).get('authorName')
                        break
            except Exception as e:
                logger.error("Error checking library items for book ID %s: %s", first_book_id, e)

        # Fall back to the first_book_metadata if we couldn't find the book in library items
        if not raw_author_name and first_book_metadata.get("authorName"):
            raw_author_name = first_book_metadata.get("authorName")

        # Final fallback
        if not raw_author_name:
            raw_author_name = "Unknown Author"

        content_text = raw_author_name
        # Format the content based on the source
        if from_search_feed:
                content_text = f"Series by {raw_author_name}"
                logger.debug("Adding series to feed from search: %s", content_text)

        # Use the direct series route instead of query parameters
        series_id = series.get('id')

        series_link = f"/opds/{username}/libraries/{library_id}/series/{series_id}"

        # Create entry data structure
        entry_data = {
            "entry": {
                "title": {"_text": series.get("name", "Unknown series name")},
                "id": {"_text": series_id},
                "updated": {"_text": self.get_current_timestamp()},
                "author": {
                    "name": {"_text": raw_author_name}
                },
                "content": {"_text": content_text},
                "link": [
                    {
                        "_attrs": {
                            "href": series_link,
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

        # Convert the dictionary to XML elements
        dict_to_xml(feed, entry_data)

    async def generate_series_feed(self, username, library_id, token=None):
        """Display all series in the library.

        Args:
            username (str): The username requesting the feed.
            library_id (str): The ID of the library to generate the feed for.
            token (str, optional): Authentication token for Audiobookshelf.

        Returns:
            Response: A FastAPI response object containing the XML feed.
        """
        series_params = {"limit": 2000, "sort": "name"}
        data = await fetch_from_api(f"/libraries/{library_id}/series", series_params, username=username, token=token)

        feed = self.create_base_feed(username, library_id, token=token)

        # Create feed metadata using dictionary approach
        feed_data = {
            "id": {"_text": f"{library_id}/series"},
            "title": {"_text": f"{username}'s series"}
        }

        # Convert feed metadata to XML
        dict_to_xml(feed, feed_data)

        series_items = self.filter_series(data)

        for series in series_items:
            await self.add_series_to_feed(username, library_id, feed, series, token)

        return self.create_response(feed)
