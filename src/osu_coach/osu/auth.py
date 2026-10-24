import httpx
from pydantic import BaseModel, SecretStr, ValidationError
from redis.asyncio import Redis

TOKEN_URL = "https://osu.ppy.sh/oauth/token"
CACHE_KEY = "osu_api_token"
EXPIRY_MARGIN = 60  # seconds


class OsuAuthError(Exception):
    """osu! API authentication error."""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int


class TokenProvider:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        redis_client: Redis,
        client_id: str,
        client_secret: SecretStr,
    ) -> None:
        self.http_client = http_client
        self.redis_client = redis_client
        self.client_id = client_id
        self.client_secret = client_secret

    async def get_token(self) -> str:
        # Check if token is cached in Redis
        cached_token: bytes | None = await self.redis_client.get(CACHE_KEY)
        if cached_token:
            return cached_token.decode("utf-8")

        # If not cached, request a new token from osu! API.
        # Several processes may fetch at once when the token expires; that's fine
        # for now (about once a day, and extra tokens are harmless).
        token_data = await self._fetch_token()

        # Cache the token in Redis with an expiry time.
        # TODO: if expires_in <= EXPIRY_MARGIN, ex is 0 or negative and Redis rejects the
        # set. osu! always sends 86400 today; skip caching in that case when we fix it.
        await self.redis_client.set(
            CACHE_KEY,
            token_data.access_token,
            ex=token_data.expires_in - EXPIRY_MARGIN,
        )

        return token_data.access_token

    async def _fetch_token(self) -> TokenResponse:
        """Fetch a new token from osu! API."""
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret.get_secret_value(),
            "grant_type": "client_credentials",
            "scope": "public",
        }

        response = await self.http_client.post(TOKEN_URL, data=data)

        # Bad credentials. Other errors (5xx, network) pass through as httpx errors.
        if response.status_code in (400, 401):
            raise OsuAuthError("osu! rejected the client credentials")
        response.raise_for_status()

        try:
            return TokenResponse.model_validate_json(response.content)
        except ValidationError:
            # Fixed message: the validation error would include parts of the token.
            raise OsuAuthError("osu! sent an unexpected token reply") from None
