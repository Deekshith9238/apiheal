"""Bounded HTTP reads against app-controlled upstream hosts."""

import json

import httpx

MAX_RESPONSE = 4_000_000


class UpstreamError(Exception):
    """The upstream response could not be used."""


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    missing_ok: bool = False,
    **kwargs,
):
    try:
        with client.stream(method, url, **kwargs) as response:
            if missing_ok and response.status_code == 404:
                return None
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    raise UpstreamError("Upstream response exceeded the size limit.")
        return json.loads(data)
    except (httpx.HTTPError, ValueError, RecursionError) as exc:
        raise UpstreamError("Upstream request failed or returned invalid JSON.") from exc
