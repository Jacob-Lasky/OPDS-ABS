"""Exception types and the XML error response for OPDS-ABS.

Feed code raises ``OPDSBaseException`` subclasses (each carries its HTTP
status); the exception handlers in ``main.py`` log each error once and turn it
into ``error_response``. Routes do not catch exceptions themselves.
"""
import logging
from typing import Optional
from fastapi.responses import Response
from lxml import etree

# Create a logger for this module
logger = logging.getLogger(__name__)

# Custom exception classes
class OPDSBaseException(Exception):
    """Base exception for all OPDS-ABS specific exceptions.

    This class serves as the foundation for the application's exception hierarchy,
    providing consistent behavior and properties that all derived exceptions inherit.

    Each derived exception should:
    1. Define an appropriate status_code (HTTP status code)
    2. Provide a meaningful default_message
    3. Optionally override __init__ if additional context is needed

    The exception hierarchy allows for specific error types to be caught and handled
    individually while also enabling generic error handling through the base type.

    Attributes:
        status_code (int): HTTP status code to return (defaults to 500)
        default_message (str): Message to use if no specific message is provided
    """
    status_code = 500
    default_message = "An internal server error occurred"

class ResourceNotFoundError(OPDSBaseException):
    """Raised when a requested resource is not found."""
    status_code = 404
    default_message = "Resource not found"

class AuthenticationError(OPDSBaseException):
    """Raised when authentication fails."""
    status_code = 401
    default_message = "Authentication failed"

class APIClientError(OPDSBaseException):
    """Raised when there's an error communicating with Audiobookshelf."""
    status_code = 502
    default_message = "Error communicating with Audiobookshelf"

class FeedGenerationError(OPDSBaseException):
    """Raised when there's an error generating a feed."""
    status_code = 500
    default_message = "Error generating feed"

class CacheError(OPDSBaseException):
    """Raised when there's an error with the cache."""
    status_code = 500
    default_message = "Cache operation failed"

# Error handling functions
def error_response(status_code: int, message: str, request_id: str = "-",
                   headers: Optional[dict] = None) -> Response:
    """Build the XML error body every handler returns.

    Built with lxml so the message is escaped. DO NOT go back to f-string XML:
    request paths were interpolated raw, so a crafted username produced a
    malformed document (and markup reflected on this origin).
    """
    root = etree.Element("error", nsmap={None: "http://opds-spec.org/2010/catalog"})
    etree.SubElement(root, "id").text = request_id
    etree.SubElement(root, "message").text = str(message)
    body = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    return Response(content=body, media_type="application/xml",
                    status_code=status_code, headers=headers)


def log_error(
    exc: Exception,
    context: str = "",
    log_traceback: bool = True
) -> None:
    """Log an error once, with optional traceback.

    Args:
        exc: The exception to log
        context: Additional context about where the error occurred
        log_traceback: Whether to log the full traceback
    """
    prefix = f"Error in {context}" if context else "Error"
    if log_traceback:
        logger.error("%s: %s", prefix, exc, exc_info=exc)
    else:
        logger.error("%s: %s", prefix, exc)
