"""Polite downloading from AEMO's NEMWEB, using only the standard library."""

import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def _user_agent():
    contact = os.environ.get("RR_CONTACT", "").strip()
    base = "RoofAndReserve-data/1.0 (public market data, educational use)"
    return f"{base}; contact: {contact}" if contact else base


class Fetcher:
    """Downloads files and directory listings. Tests replace this with a fake."""

    def __init__(self, timeout=45, retries=3, workers=4, user_agent=None):
        self.timeout = timeout
        self.retries = retries
        self.workers = workers
        self.user_agent = user_agent or _user_agent()

    def get(self, url):
        """Return the bytes at url. Raises on failure after retries."""
        last = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 404):
                    raise  # retrying will not help
                last = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(2 ** attempt)
        raise last

    def get_many(self, urls):
        """Download several urls in parallel. A failed download gives None."""

        def one(url):
            try:
                return self.get(url)
            except Exception as exc:  # noqa: BLE001 - report and carry on
                print(f"warning: could not download {url}: {exc}", file=sys.stderr)
                return None

        if not urls:
            return []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(one, urls))

    def list_files(self, dir_url, pattern):
        """List .zip files in a NEMWEB directory page whose name matches pattern.

        Returns a sorted list of (file_name, regex_groups).
        """
        html = self.get(dir_url).decode("utf-8", errors="replace")
        rx = re.compile(pattern)
        found = {}
        for href in re.findall(r'href="([^"]+?\.zip)"', html, flags=re.I):
            name = href.rsplit("/", 1)[-1]
            m = rx.match(name)
            if m:
                found[name] = m.groups()
        return sorted(found.items())
