import asyncio

import httpx
import pytest
import respx

from prodata.http_client import FetchBlocked, HttpClient, RateLimiter, SourceUnreachable

ROBOTS_ALLOW = "User-agent: *\nDisallow: /private/\n"


def run(coro):
    return asyncio.run(coro)


def test_user_agent_requires_contact_email(tmp_path):
    with pytest.raises(ValueError):
        HttpClient(user_agent="ProDataBot/0.1", cache_dir=tmp_path)


@respx.mock
def test_sends_user_agent_and_caches(make_client):
    respx.get("https://example.gov/robots.txt").respond(200, text=ROBOTS_ALLOW)
    route = respx.get("https://example.gov/data.csv").respond(200, text="a,b\n1,2\n")
    client = make_client()

    async def go():
        first = await client.get("https://example.gov/data.csv")
        second = await client.get("https://example.gov/data.csv")
        await client.aclose()
        return first, second

    first, second = run(go())
    assert first.text() == "a,b\n1,2\n" and not first.from_cache
    assert second.from_cache
    assert route.call_count == 1  # never refetched inside the TTL
    assert route.calls[0].request.headers["User-Agent"].startswith("ProDataBot/0.1")


@respx.mock
def test_robots_txt_disallow_is_honoured(make_client):
    respx.get("https://example.gov/robots.txt").respond(200, text=ROBOTS_ALLOW)
    client = make_client()

    async def go():
        try:
            await client.get("https://example.gov/private/list.csv")
        finally:
            await client.aclose()

    with pytest.raises(FetchBlocked):
        run(go())
    assert [str(c.request.url) for c in respx.calls] == ["https://example.gov/robots.txt"]


@respx.mock
def test_robots_403_means_disallow_all(make_client):
    respx.get("https://example.gov/robots.txt").respond(403)
    client = make_client()

    async def go():
        try:
            await client.get("https://example.gov/page")
        finally:
            await client.aclose()

    with pytest.raises(FetchBlocked):
        run(go())
    assert [str(c.request.url) for c in respx.calls] == ["https://example.gov/robots.txt"]


@respx.mock
def test_unreachable_site_reports_network_error_not_robots(make_client):
    respx.get("https://example.gov/robots.txt").mock(side_effect=httpx.ConnectError("getaddrinfo failed"))
    client = make_client()

    async def go():
        try:
            await client.get("https://example.gov/page")
        finally:
            await client.aclose()

    with pytest.raises(SourceUnreachable, match="Could not reach example.gov"):
        run(go())
    assert [str(c.request.url) for c in respx.calls] == ["https://example.gov/robots.txt"]


@respx.mock
def test_retries_5xx_then_succeeds(make_client):
    respx.get("https://example.gov/robots.txt").respond(404)
    route = respx.get("https://example.gov/api").mock(
        side_effect=[httpx.Response(503), httpx.Response(429), httpx.Response(200, json={"ok": True})]
    )
    client = make_client()

    async def go():
        resp = await client.get("https://example.gov/api")
        await client.aclose()
        return resp

    assert run(go()).json() == {"ok": True}
    assert route.call_count == 3


@respx.mock
def test_403_stops_without_retry(make_client):
    respx.get("https://example.gov/robots.txt").respond(404)
    route = respx.get("https://example.gov/api").respond(403)
    client = make_client()

    async def go():
        try:
            await client.get("https://example.gov/api")
        finally:
            await client.aclose()

    with pytest.raises(FetchBlocked):
        run(go())
    assert route.call_count == 1


def test_rate_limiter_spaces_requests_per_domain():
    clock = {"t": 0.0}
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(round(seconds, 3))
        clock["t"] += seconds

    limiter = RateLimiter(1.0, clock=lambda: clock["t"], sleep=fake_sleep)

    async def go():
        for _ in range(3):
            await limiter.wait("a.gov")
        await limiter.wait("b.gov")  # other domain: no wait

    run(go())
    assert sleeps == [1.0, 1.0]


@respx.mock
def test_client_applies_rate_limit(make_client):
    respx.get("https://example.gov/robots.txt").respond(404)
    respx.get(url__regex=r"https://example\.gov/p\d").respond(200, text="x")
    clock = {"t": 0.0}
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    client = make_client(rate_limiter=RateLimiter(1.0, clock=lambda: clock["t"], sleep=fake_sleep))

    async def go():
        for i in range(3):
            await client.get(f"https://example.gov/p{i}")
        await client.aclose()

    run(go())
    # robots.txt + 3 pages = 4 requests to one host → 3 one-second waits
    assert sleeps == [1.0, 1.0, 1.0]
