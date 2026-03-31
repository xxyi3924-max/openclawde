import http.server
import socketserver
import threading

import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

import sandbox

_servers: dict[int, socketserver.TCPServer] = {}


def web_search(query: str, max_results: int = 5) -> str:
    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append(f"Title: {r['title']}\nURL: {r['href']}\nSnippet: {r['body']}")
        return "\n\n---\n\n".join(results) if results else "No results."
    except Exception as e:
        return f"Search error: {e}"


def fetch_url(url: str) -> str:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            return text[:4000] + ("…" if len(text) > 4000 else "")
        return r.text[:4000]
    except Exception as e:
        return f"Fetch error: {e}"


def start_web_server(directory: str = "", port: int = 8080) -> str:
    if port in _servers:
        return f"Server already running on port {port}"
    target = sandbox.workspace_path(directory)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(target), **kwargs)

        def log_message(self, *args):
            pass

    try:
        httpd = socketserver.TCPServer(("", port), Handler)
        httpd.allow_reuse_address = True
        _servers[port] = httpd
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return f"Server running at http://localhost:{port} (serving {target.name}/)"
    except OSError as e:
        return f"Could not start server: {e}"


def stop_web_server(port: int = 8080) -> str:
    if port not in _servers:
        return f"No server on port {port}"
    _servers[port].shutdown()
    del _servers[port]
    return f"Server on port {port} stopped"
