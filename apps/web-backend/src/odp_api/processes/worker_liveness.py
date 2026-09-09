"""Expiring, model-scoped worker presence used only for admission preflight."""

HEARTBEAT_TTL_SECONDS = 15


def liveness_key(model_sha256: str) -> str:
    digest = model_sha256.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("worker liveness requires a valid model SHA-256")
    return f"odp:inference:worker-presence:{digest}"


class RedisWorkerHeartbeat:
    """Any matching worker may renew presence; exits never delete a peer's key."""

    def __init__(self, client, model_sha256: str) -> None:
        self._client = client
        self._key = liveness_key(model_sha256)

    async def __call__(self) -> None:
        result = await self._client.set(self._key, "alive", ex=HEARTBEAT_TTL_SECONDS)
        if not result:
            raise ConnectionError("worker presence update was not acknowledged")
