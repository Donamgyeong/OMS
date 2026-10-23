"""Fetch pinned frontend assets and their licenses from upstream repositories."""
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent / "static" / "vendor"
ASSETS = {
    "bootstrap/bootstrap.min.css": "https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/dist/css/bootstrap.min.css",
    "bootstrap/bootstrap.min.css.map": "https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/dist/css/bootstrap.min.css.map",
    "bootstrap/LICENSE": "https://raw.githubusercontent.com/twbs/bootstrap/v5.3.8/LICENSE",
    "htmx/htmx.min.js": "https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.8/dist/htmx.min.js",
    "htmx/LICENSE": "https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.8/LICENSE",
}

if __name__ == "__main__":
    for relative, url in ASSETS.items():
        destination = ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(url, timeout=30) as response:
            content = response.read()
        destination.write_bytes(content)
        print(relative)
