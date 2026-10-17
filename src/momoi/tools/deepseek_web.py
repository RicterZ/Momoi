"""DeepSeek's standalone search and page extraction APIs."""
from urllib.parse import urlsplit

import httpx


async def deepseek_web(name, arguments, api_key):
    if not api_key:
        return {"ok": False, "error": "deepseek_api_key_not_configured"}
    if name == "search_web":
        if set(arguments) - {"query", "count"}:
            raise ValueError("unsupported search arguments")
        query, count = arguments.get("query"), arguments.get("count", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            raise ValueError("query must contain 1 to 128 characters")
        if type(count) is not int or not 1 <= count <= 50:
            raise ValueError("count must be between 1 and 50")
        payload, endpoint = {"query": query, "count": count}, "search"
    else:
        if set(arguments) != {"url"}:
            raise ValueError("fetch requires only url")
        url = arguments["url"]
        if not isinstance(url, str) or len(url) > 8192:
            raise ValueError("invalid URL")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("invalid URL")
        payload, endpoint = {"url": url}, "fetch"
    # Only send credentials to the official API, never to the fetched URL.
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post("https://api.deepseek.com/v1/tools/" + endpoint,
            headers={"Authorization": "Bearer " + api_key}, json=payload)
        if response.status_code != 200:
            return {"ok": False, "error": "deepseek_web_http_error", "status": response.status_code}
        return {"ok": True, **response.json()}


def deepseek_web_key(catalog):
    for capability in ("balance", "llm"):
        if not catalog.enabled(capability):
            continue
        options = catalog.options_for(capability)
        host = urlsplit(options.get("base_url", "")).hostname
        if host == "api.deepseek.com" or (capability == "balance" and catalog.adapter_for(capability) == "deepseek"):
            if options.get("api_key"):
                return options["api_key"]
    return ""
