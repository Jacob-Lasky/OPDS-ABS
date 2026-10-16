"""OPDS-ABS entry point: run the ASGI app under uvicorn on 0.0.0.0:8000."""
import uvicorn

from opds_abs.config import DEV_RELOAD
from opds_abs.utils.logging_utils import configure_logging

if __name__ == "__main__":
    configure_logging()
    uvicorn.run(
        "opds_abs.main:app",
        host="0.0.0.0",
        port=8000,
        reload=DEV_RELOAD,
        # The app installs ProxyHeadersMiddleware itself (FORWARDED_ALLOW_IPS).
        proxy_headers=False,
        # Our AccessLogMiddleware replaces this; uvicorn's prints raw ?token= values.
        access_log=False,
        # Keep our handlers: uvicorn's default dictConfig would replace them.
        log_config=None,
    )
