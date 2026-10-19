"""Polite downloading from AEMO's NEMWEB, using only the standard library."""

import os
import re
import sys
import time
import urllib.error
import urllib.request


def _user_agent():
    contact = os.environ.get("RR_CONTACT", "").strip()
    base = "RoofAndReserve-data/1.0 (public market data, educational use)"
    return f"{base}; contact: {contact}" if contact else base


class Fetcher:
    """Downloads files and directory listings. Tests replace this with a fake."""

    def __init__(self, timeout=45, retries=3, pause=0.4, max_refused=3, user_agent=None):
        self.timeout = timeout
        self.retries = retries
        self.pause = pause              # seconds to wait between downloads (be gentle with AEMO)
        self.max_refused = max_refused  # stop a batch after this many refusals in a row
        self.user_agent = user_agent or _user_agent()
        self._last_request = 0.0

    def polite(self):
        """Wait so that downloads are at least `pause` seconds apart."""
        wait = self.pause - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, url):
        """Return the bytes at url. Raises on failure after retries."""
        last = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise  # retrying will not help
                if exc.code in (403, 429):
                    # AEMO answers 403 when it is rate limiting. Wait longer, try once more.
                    last = exc
                    if attempt >= 1:
                        raise
                    time.sleep(8)
                    continue
                last = exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last = exc
            time.sleep(2 ** attempt)
        raise last

    def get_many(self, urls):
        """Download several urls one at a time, politely. A failed download gives None.

        If AEMO refuses several downloads in a row we stop the batch and leave the rest
        for the next run, rather than hammering a server that has said no.
        """
        results = []
        refused = 0
        for i, url in enumerate(urls):
            if refused >= self.max_refused:
                print(f"warning: stopping after {refused} refused downloads in a row; "
                      f"{len(urls) - i} file(s) left for the next run", file=sys.stderr)
                results.extend([None] * (len(urls) - i))
                break
            self.polite()
            try:
                results.append(self.get(url))
                refused = 0
            except Exception as exc:  # noqa: BLE001 - report and carry on
                print(f"warning: could not download {url}: {exc}", file=sys.stderr)
                results.append(None)
                refused += 1
        return results

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
