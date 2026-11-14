"""HTTP GET that never leaks keys: errors carry the host and status only."""

import time
from urllib.parse import urlsplit

import requests

USER_AGENT = "smoke-outlook/0.1 (research; unofficial)"


class FetchError(RuntimeError):
    pass


def get(url: str, *, params=None, headers=None, timeout=60, retries=3) -> requests.Response:
    host = urlsplit(url).netloc
    last = "no attempt"
    for attempt in range(retries):
        try:
            r = requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, **(headers or {})},
                timeout=timeout,
            )
        except requests.RequestException as e:
            last = type(e).__name__
        else:
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if r.status_code == 429:
                time.sleep(min(int(r.headers.get("X-Ratelimit-Reset", 20)), 60))
                continue
            if r.status_code < 500:
                break
        time.sleep(2 * (attempt + 1))
    raise FetchError(f"{host}: {last}")
