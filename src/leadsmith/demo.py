"""Serves the bundled example websites through a fake HTTP transport, so demos and tests
run without touching the real internet."""

from pathlib import Path

import httpx

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


def make_demo_transport(sites_dir: Path = EXAMPLES_DIR / "sites") -> httpx.MockTransport:
    """Map https://<host>/<page> to examples/sites/<host>/<page>.html ('/' is index.html)."""

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.path.strip("/") or "index"
        file = sites_dir / request.url.host / f"{page}.html"
        if not file.resolve().is_relative_to(sites_dir.resolve()) or not file.is_file():
            return httpx.Response(404, text="Not found", headers={"content-type": "text/html"})
        return httpx.Response(200, text=file.read_text(), headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)
