import pytest

from leadsmith.scraper import ScrapeError, fetch_site, normalize_url, parse_page


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.org", "https://example.org"),
        ("HTTP://Example.org/", "http://example.org"),
        ("  https://shop.example.org/about/ ", "https://shop.example.org/about"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_parse_page_extracts_signal_and_drops_noise():
    html = """
    <html><head><title> Acme Plumbing </title>
    <meta name="description" content="24/7 emergency plumbing."></head>
    <body>
      <nav><a href="/about-us">About</a><a href="https://other.org/about">Elsewhere</a></nav>
      <p>Call or email jobs@acmeplumbing.org</p>
      <img src="logo@2x.png">
      <script>var tracker = "noise@sentry.io"; alert("hi")</script>
    </body></html>
    """
    page = parse_page("https://acmeplumbing.org", html)

    assert page.title == "Acme Plumbing"
    assert page.description == "24/7 emergency plumbing."
    assert page.emails == {"jobs@acmeplumbing.org"}
    assert page.subpage_links == ["https://acmeplumbing.org/about-us"]
    assert "alert" not in page.text
    assert "Call or email" in page.text


async def test_fetch_site_follows_relevant_subpages(http):
    site = await fetch_site(http, "brightsmiledental.test")

    assert site.url == "https://brightsmiledental.test"
    assert "https://brightsmiledental.test/about" in site.pages
    assert "hundreds of calls" in site.text  # only on the About page
    assert site.emails == ["frontdesk@brightsmiledental.test"]


async def test_fetch_site_skips_broken_subpages(http):
    # summitroofing links to /contact, which doesn't exist; the lead should still succeed.
    site = await fetch_site(http, "summitroofing.test")
    assert site.pages == ["https://summitroofing.test", "https://summitroofing.test/services"]


async def test_fetch_site_raises_for_missing_site(http):
    with pytest.raises(ScrapeError, match="404"):
        await fetch_site(http, "nowhere.test")
