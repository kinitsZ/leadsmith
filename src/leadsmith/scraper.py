"""Fetches a company's website and boils it down to the text an analyst would actually read."""

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = "LeadSmithBot/0.1 (+https://github.com/your-username/leadsmith)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Internal pages that usually explain what a business does and how it operates.
SUBPAGE_HINTS = ("about", "services", "what-we-do", "solutions", "team", "contact")
NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "nav", "footer")
# Ignore image filenames and tracking addresses that look like emails.
IGNORED_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
IGNORED_EMAIL_DOMAINS = ("sentry.io", "example.com", "wixpress.com")


class ScrapeError(Exception):
    pass


@dataclass
class SiteSnapshot:
    url: str
    title: str = ""
    description: str = ""
    text: str = ""
    emails: list[str] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)


@dataclass
class ParsedPage:
    title: str
    description: str
    text: str
    emails: set[str]
    subpage_links: list[str]


def normalize_url(url: str) -> str:
    """Turn 'Example.com/' or 'http://example.com' into 'https://example.com'."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}"


def parse_page(base_url: str, html: str) -> ParsedPage:
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    description = meta.get("content", "").strip() if meta else ""

    emails = {
        m.lower()
        for m in EMAIL_RE.findall(html)
        if not m.lower().endswith(IGNORED_EMAIL_SUFFIXES)
        and not m.lower().endswith(IGNORED_EMAIL_DOMAINS)
    }

    # Collect links before removing <nav>, since navigation is where "About" usually lives.
    base_host = urlparse(base_url).netloc
    subpage_links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url + "/", a["href"])
        parsed = urlparse(href)
        if parsed.netloc != base_host or parsed.scheme not in ("http", "https"):
            continue
        clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if clean == base_url or clean in subpage_links:
            continue
        if any(hint in parsed.path.lower() for hint in SUBPAGE_HINTS):
            subpage_links.append(clean)

    for tag in soup(NOISE_TAGS):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

    return ParsedPage(title, description, text, emails, subpage_links)


async def fetch_site(
    client: httpx.AsyncClient, url: str, *, max_subpages: int = 2, max_chars: int = 12_000
) -> SiteSnapshot:
    """Fetch the homepage plus a couple of relevant subpages.

    Text is capped at `max_chars` on purpose: the homepage and About page carry nearly all
    the signal, and a cap keeps per-lead model cost predictable.
    """
    url = normalize_url(url)
    home = await _get_html(client, url)
    page = parse_page(url, home)

    snapshot = SiteSnapshot(url=url, title=page.title, description=page.description)
    texts = [page.text]
    emails = set(page.emails)
    snapshot.pages.append(url)

    for link in page.subpage_links[:max_subpages]:
        try:
            sub = parse_page(url, await _get_html(client, link))
        except ScrapeError:
            continue  # A broken About page shouldn't sink the whole lead.
        texts.append(sub.text)
        emails |= sub.emails
        snapshot.pages.append(link)

    snapshot.text = "\n\n".join(texts)[:max_chars]
    snapshot.emails = sorted(emails)
    return snapshot


async def _get_html(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise ScrapeError(f"Could not reach {url}: {exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        raise ScrapeError(f"{url} returned HTTP {response.status_code}")
    if "html" not in response.headers.get("content-type", "text/html"):
        raise ScrapeError(f"{url} is not an HTML page")
    return response.text


def make_http_client(timeout: float = 15.0, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        limits=httpx.Limits(max_connections=20),
        **kwargs,
    )
