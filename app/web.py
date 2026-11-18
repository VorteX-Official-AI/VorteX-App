"""Live web access: search engines, Wikipedia, GitHub, Hugging Face and page reading."""

import asyncio
import base64
import html as html_lib
import ipaddress
import json
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from .database import get_setting

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SEARCH_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
PAGE_TIMEOUT = httpx.Timeout(14.0, connect=6.0)
MAX_PAGE_CHARS = 2500
MAX_CONTEXT_CHARS = 12000
MAX_SOURCES = 8

SEARCH_PROVIDERS = ("google", "brave", "serper", "tavily", "exa", "bing", "duckduckgo")
PROVIDER_LABELS = {
    "auto": "Automatic",
    "google": "Google Programmable Search",
    "brave": "Brave Search",
    "tavily": "Tavily",
    "serper": "Serper (Google)",
    "exa": "Exa",
    "bing": "Bing (no key)",
    "duckduckgo": "DuckDuckGo (no key)",
}

TAG_BLOCK = re.compile(r"<(script|style|noscript|svg|head|nav|footer)[^>]*>.*?</\1>", re.S | re.I)
TAG_ANY = re.compile(r"<[^>]+>")


def _clean(text: str, limit: int | None = None) -> str:
    text = html_lib.unescape(TAG_ANY.sub(" ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def _html_to_text(markup: str) -> str:
    return _clean(TAG_BLOCK.sub(" ", markup or ""))


def _public_url(url: str) -> bool:
    """Refuse localhost/private targets so the fetcher cannot reach the host network."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


GITHUB_HINTS = {
    "api", "cli", "compiler", "database", "docker", "framework", "github", "go", "golang",
    "java", "javascript", "kotlin", "kubernetes", "library", "npm", "package", "pip",
    "python", "repo", "repository", "runtime", "rust", "sdk", "swift", "typescript",
}
HF_HINTS = {
    "asr", "dataset", "datasets", "diffusion", "embedding", "embeddings", "face", "finetune",
    "fine-tune", "hugging", "huggingface", "inference", "llm", "llms", "model", "models",
    "pytorch", "speech", "stable", "tensorflow", "transformer", "transformers", "tts", "vision",
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "did", "do",
    "does", "for", "from", "give", "how", "i", "in", "is", "it", "me", "my", "of",
    "on", "or", "please", "tell", "than", "that", "the", "their", "them", "there", "these",
    "this", "to", "was", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "would", "you", "your",
}


def search_query(text: str) -> str:
    """Turn a chat message into a compact keyword query for search engines."""
    first = re.split(r"[?\n]", text or "")[0].strip() or (text or "").strip()
    first = re.sub(r"^(hi|hey|hello|yo)[,!\s]+", "", first, flags=re.I)
    words = re.findall(r"[\w.+#@-]+", first)
    kept = [word for word in words if word.lower() not in STOPWORDS]
    query = " ".join(kept or words)[:120].strip(" .,")
    return query or first[:120]


def _search_key(name: str) -> str:
    return (get_setting(f"web_{name}_key", "") or "").strip()


# ------------------------------------------------------------------ search engines


async def _google(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    key = _search_key("google")
    cx = (get_setting("web_google_cx", "") or "").strip()
    if not key or not cx:
        return []
    response = await client.get(
        "https://www.googleapis.com/customsearch/v1",
        params={"key": key, "cx": cx, "q": query, "num": min(limit, 10)},
    )
    if response.status_code >= 400:
        return []
    return [
        {
            "title": _clean(item.get("title", ""), 160),
            "url": item.get("link", ""),
            "snippet": _clean(item.get("snippet", ""), 400),
            "engine": "Google",
        }
        for item in (response.json().get("items") or [])
        if item.get("link")
    ][:limit]


async def _brave(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    key = _search_key("brave")
    if not key:
        return []
    response = await client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": min(limit, 20)},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    if response.status_code >= 400:
        return []
    results = (response.json().get("web") or {}).get("results") or []
    return [
        {
            "title": _clean(item.get("title", ""), 160),
            "url": item.get("url", ""),
            "snippet": _clean(item.get("description", ""), 400),
            "engine": "Brave",
        }
        for item in results
        if item.get("url")
    ][:limit]


async def _tavily(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    key = _search_key("tavily")
    if not key:
        return []
    response = await client.post(
        "https://api.tavily.com/search",
        json={
            "api_key": key,
            "query": query,
            "max_results": min(limit, 10),
            "search_depth": "basic",
            "include_answer": False,
        },
    )
    if response.status_code >= 400:
        return []
    payload = response.json()
    return [
        {
            "title": _clean(item.get("title", ""), 160),
            "url": item.get("url", ""),
            "snippet": _clean(item.get("content", ""), 400),
            "engine": "Tavily",
        }
        for item in payload.get("results") or []
        if item.get("url")
    ][:limit]


async def _serper(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    key = _search_key("serper")
    if not key:
        return []
    response = await client.post(
        "https://google.serper.dev/search",
        json={"q": query, "num": min(limit, 10)},
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        return []
    payload = response.json()
    return [
        {
            "title": _clean(item.get("title", ""), 160),
            "url": item.get("link", ""),
            "snippet": _clean(item.get("snippet", ""), 400),
            "engine": "Google",
        }
        for item in payload.get("organic") or []
        if item.get("link")
    ][:limit]


async def _exa(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    key = _search_key("exa")
    if not key:
        return []
    response = await client.post(
        "https://api.exa.ai/search",
        json={
            "query": query,
            "numResults": min(limit, 10),
            "type": "auto",
            "contents": {"text": {"maxCharacters": 700}},
        },
        headers={"x-api-key": key, "Content-Type": "application/json"},
    )
    if response.status_code >= 400:
        return []
    payload = response.json()
    return [
        {
            "title": _clean(item.get("title", "") or item.get("url", ""), 160),
            "url": item.get("url", ""),
            "snippet": _clean(item.get("text", ""), 400),
            "engine": "Exa",
        }
        for item in payload.get("results") or []
        if item.get("url")
    ][:limit]


def _bing_target(url: str) -> str:
    """Bing wraps results in bing.com/ck/a?u=a1<base64> redirect links."""
    url = html_lib.unescape(url or "")
    if "bing.com/ck/a" not in url:
        return url
    try:
        param = parse_qs(urlparse(url).query).get("u", [""])[0]
        if param.startswith("a1"):
            param = param[2:]
        return base64.urlsafe_b64decode(param + "=" * (-len(param) % 4)).decode("utf-8", "replace")
    except (ValueError, KeyError, IndexError):
        return ""


async def _bing(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    response = await client.get(
        "https://www.bing.com/search",
        params={"q": query, "setlang": "en", "cc": "us"},
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    if response.status_code >= 400:
        return []
    blocks = re.findall(r'<li class="b_algo".*?</li>', response.text, re.S)
    results: list[dict] = []
    for block in blocks:
        heading = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not heading:
            continue
        url = _bing_target(heading.group(1))
        if not url.startswith(("http://", "https://")):
            continue
        snippet = re.search(r'class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', block, re.S)
        text = _clean(snippet.group(1), 400) if snippet else ""
        if not text:
            body = TAG_ANY.sub(" ", re.sub(r'<h2[^>]*>.*?</h2>', " ", block, flags=re.S))
            text = _clean(body, 400)
        results.append(
            {
                "title": _clean(heading.group(2), 160),
                "url": url,
                "snippet": text,
                "engine": "Bing",
            }
        )
        if len(results) >= limit:
            break
    return results


async def _duckduckgo(client: httpx.AsyncClient, query: str, limit: int) -> list[dict]:
    response = await client.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query, "kl": "wt-wt"},
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    if response.status_code >= 400:
        return []
    markup = response.text
    links = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', markup, re.S
    )
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', markup, re.S
    )
    results: list[dict] = []
    for index, (href, title) in enumerate(links[:limit]):
        url = href
        if "uddg=" in url:
            try:
                url = parse_qs(urlparse(url).query)["uddg"][0]
            except (KeyError, IndexError):
                url = unquote(url)
        if not url.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "title": _clean(title, 160),
                "url": url,
                "snippet": _clean(snippets[index], 400) if index < len(snippets) else "",
                "engine": "DuckDuckGo",
            }
        )
    return results


_ENGINES = {
    "google": _google,
    "brave": _brave,
    "tavily": _tavily,
    "serper": _serper,
    "exa": _exa,
    "bing": _bing,
    "duckduckgo": _duckduckgo,
}


async def _wikipedia_results(query: str, limit: int) -> list[dict]:
    """Last-resort result set so a search never comes back empty."""
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            response = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": min(limit, 8),
                },
            )
            if response.status_code >= 400:
                return []
            hits = (response.json().get("query") or {}).get("search") or []
    except (httpx.HTTPError, ValueError):
        return []
    return [
        {
            "title": hit.get("title", ""),
            "url": "https://en.wikipedia.org/wiki/" + quote(str(hit.get("title", "")).replace(" ", "_")),
            "snippet": _clean(hit.get("snippet", ""), 300),
            "engine": "Wikipedia",
        }
        for hit in hits
        if hit.get("title")
    ]


async def search(query: str, limit: int = 5) -> tuple[list[dict], str]:
    """Search the web, using the provider chosen in admin settings."""
    chosen = (get_setting("web_provider", "auto") or "auto").strip().lower()
    if chosen not in _ENGINES:
        chosen = "auto"
    order = list(SEARCH_PROVIDERS) if chosen == "auto" else [chosen]
    for fallback in ("bing", "duckduckgo"):
        if fallback not in order:
            order.append(fallback)

    async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as client:
        for name in order:
            try:
                results = await _ENGINES[name](client, query, limit)
            except (httpx.HTTPError, ValueError, KeyError):
                results = []
            if results:
                return results, name
    return await _wikipedia_results(query, limit), "wikipedia"


# ------------------------------------------------------------------ sources


async def wikipedia(query: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as client:
            found = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 1,
                },
                headers={"User-Agent": USER_AGENT},
            )
            if found.status_code >= 400:
                return None
            hits = (found.json().get("query") or {}).get("search") or []
            if not hits:
                return None
            title = hits[0]["title"]
            summary = await client.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace(" ", "_")),
                headers={"User-Agent": USER_AGENT},
            )
            if summary.status_code >= 400:
                return None
            data = summary.json()
            extract = _clean(data.get("extract", ""), 1400)
            if not extract:
                return None
            return {
                "title": data.get("title") or title,
                "url": ((data.get("content_urls") or {}).get("desktop") or {}).get("page")
                or f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                "extract": extract,
                "kind": "wikipedia",
            }
    except (httpx.HTTPError, ValueError):
        return None


async def github(query: str, limit: int = 3) -> list[dict]:
    token = (get_setting("web_github_token", "") or "").strip()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        explicit = "github" in query.lower() or "repo" in query.lower()
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "per_page": limit * 3},
                headers=headers,
            )
            if response.status_code >= 400:
                return []
            items = response.json().get("items") or []
            repos = [
                {
                    "name": item.get("full_name", ""),
                    "url": item.get("html_url", ""),
                    "description": _clean(item.get("description", "") or "", 220),
                    "stars": item.get("stargazers_count", 0),
                    "language": item.get("language") or "",
                }
                for item in items
                if item.get("html_url") and (explicit or (item.get("stargazers_count") or 0) >= 10)
            ]
            return repos[:limit]
    except (httpx.HTTPError, ValueError):
        return []


async def huggingface(query: str, limit: int = 3) -> list[dict]:
    token = (get_setting("web_hf_token", "") or "").strip()
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(
                "https://huggingface.co/api/models",
                params={
                    "search": query,
                    "limit": limit,
                    "sort": "downloads",
                    "direction": -1,
                },
                headers=headers,
            )
            if response.status_code >= 400:
                return []
            items = response.json() or []
            return [
                {
                    "name": item.get("id", ""),
                    "url": "https://huggingface.co/" + str(item.get("id", "")),
                    "downloads": item.get("downloads", 0),
                    "likes": item.get("likes", 0),
                    "task": item.get("pipeline_tag") or "",
                }
                for item in items
                if item.get("id")
            ][:limit]
    except (httpx.HTTPError, ValueError):
        return []


async def fetch_page(url: str) -> str:
    """Download a page and return readable text, or an empty string."""
    if not await asyncio.to_thread(_public_url, url):
        return ""
    try:
        async with httpx.AsyncClient(
            timeout=PAGE_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return ""
                content_type = response.headers.get("content-type", "")
                if content_type and "html" not in content_type and "text" not in content_type:
                    return ""
                if not await asyncio.to_thread(_public_url, str(response.url)):
                    return ""
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 600_000:
                        break
    except (httpx.HTTPError, ValueError):
        return ""
    markup = b"".join(chunks).decode("utf-8", "replace")
    text = _html_to_text(markup)
    return text[:MAX_PAGE_CHARS]


# ------------------------------------------------------------------ assembly


def _source(title: str, url: str, kind: str) -> dict:
    return {"title": _clean(title or url, 110), "url": url, "kind": kind}


async def _empty_list() -> list:
    return []


def _topics(query: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.-]+", (query or "").lower()))


async def gather_context(query: str) -> tuple[str, list[dict]]:
    """Collect web, Wikipedia, GitHub, Hugging Face and page content for a question."""
    words = _topics(query)
    want_github = bool(words & GITHUB_HINTS)
    want_hf = bool(words & HF_HINTS)
    search_task = search(query, 5)
    outcomes = await asyncio.gather(
        search_task,
        wikipedia(query),
        github(query, 3) if want_github else _empty_list(),
        huggingface(query, 3) if want_hf else _empty_list(),
        return_exceptions=True,
    )
    results, engine = outcomes[0] if isinstance(outcomes[0], tuple) else ([], "")
    wiki = outcomes[1] if isinstance(outcomes[1], dict) else None
    repos = outcomes[2] if isinstance(outcomes[2], list) else []
    models = outcomes[3] if isinstance(outcomes[3], list) else []

    try:
        pages_wanted = int(get_setting("web_pages", "2") or "2")
    except ValueError:
        pages_wanted = 2
    pages_wanted = max(0, min(pages_wanted, 3))

    pages: list[tuple[str, str]] = []
    if pages_wanted and results:
        targets = [item["url"] for item in results[:pages_wanted]]
        fetched = await asyncio.gather(*(fetch_page(url) for url in targets), return_exceptions=True)
        for url, text in zip(targets, fetched):
            if isinstance(text, str) and len(text) > 200:
                pages.append((url, text))

    sections: list[str] = []
    sources: list[dict] = []

    if results:
        if engine == "wikipedia":
            results = results[:3]
        lines = [f"Web search results ({engine or 'search'}):"]
        for index, item in enumerate(results, start=1):
            lines.append(f"[{index}] {item['title']} — {item['url']}")
            if item.get("snippet"):
                lines.append(f"    {item['snippet']}")
            sources.append(_source(item["title"], item["url"], "web"))
        sections.append("\n".join(lines))

    if wiki:
        sections.append(f"Wikipedia — {wiki['title']} ({wiki['url']}):\n{wiki['extract']}")
        sources.append(_source("Wikipedia: " + wiki["title"], wiki["url"], "wikipedia"))

    if repos:
        lines = ["GitHub repositories:"]
        for repo in repos:
            lines.append(
                f"- {repo['name']} ({repo['stars']} stars"
                + (f", {repo['language']}" if repo.get("language") else "")
                + f") — {repo['url']}"
                + (f"\n    {repo['description']}" if repo.get("description") else "")
            )
            sources.append(_source(repo["name"], repo["url"], "github"))
        sections.append("\n".join(lines))

    if models:
        lines = ["Hugging Face models:"]
        for model in models:
            lines.append(
                f"- {model['name']} ({model['downloads']} downloads"
                + (f", task {model['task']}" if model.get("task") else "")
                + f") — {model['url']}"
            )
            sources.append(_source(model["name"], model["url"], "huggingface"))
        sections.append("\n".join(lines))

    for url, text in pages:
        sections.append(f"Page content — {url}:\n{text}")
        if not any(source["url"] == url for source in sources):
            sources.append(_source(url, url, "page"))

    if not sections:
        return "", []

    def canonical(url: str) -> str:
        return html_lib.unescape(unquote(url)).rstrip("/").lower()

    seen: set[str] = set()
    unique: list[dict] = []
    for source in sources:
        key = canonical(source["url"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(source)
    sources = unique

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = "\n\n".join(sections)[:MAX_CONTEXT_CHARS]
    prompt = (
        f"Live web context retrieved {stamp}. Use it for facts, versions, prices and anything "
        "recent; prefer it over your own memory when they disagree. Cite the sources you rely on "
        "as markdown links using their URLs, and say so plainly if the context does not cover "
        "the question.\n\n" + body
    )
    return prompt, sources[:MAX_SOURCES]


async def test_search(query: str) -> dict:
    started = time.perf_counter()
    results, engine = await search(query, 5)
    elapsed = int((time.perf_counter() - started) * 1000)
    return {
        "ok": bool(results),
        "engine": engine,
        "elapsed_ms": elapsed,
        "results": [
            {"title": item["title"], "url": item["url"]} for item in results
        ],
    }
