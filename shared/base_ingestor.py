import httpx
import logging
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)

class BaseIngestor:
    """
    Base class for all data ingestion services.
    Implements retry logic and standardized HTTP handling.
    """
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.client = httpx.AsyncClient(timeout=30.0)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException))
    )
    async def fetch_data(self, endpoint: str, params: dict = None):
        """
        Fetches data from Energinet/ENTSO-E with exponential backoff.
        """
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error occurred: {e.response.status_code}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise

    async def close(self):
        await self.client.aclose()