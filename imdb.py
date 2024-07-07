import io
import json
import logging
import pickle
from datetime import datetime, timedelta
from time import sleep
from typing import Any, Literal

import click
import requests
from parsel import Selector

_IMDB_DEFAULT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
}

logger = logging.getLogger("imdb-data")


@click.group()
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose logging",
    envvar="ACTIONS_RUNNER_DEBUG",
)
def main(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)


def _load_cookies(cookie_file: io.BufferedReader) -> requests.cookies.RequestsCookieJar:
    if _is_pickle(cookie_file):
        logger.debug("Loading cookie jar from pickle")
        jar = pickle.load(cookie_file)
        assert isinstance(
            jar, requests.cookies.RequestsCookieJar
        ), "Expected cookie jar"
        return jar
    else:
        logger.debug("Loading cookie jar from text")
        return _parse_cookie_header(cookie_file.read().decode("utf-8"))


def _parse_cookie_header(cookie: str) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    for c in cookie.strip().split("; "):
        key, value = c.strip().split("=", 1)
        jar.set(key, value)
    return jar


def _is_pickle(file: io.BufferedReader) -> bool:
    return file.peek().startswith(
        (b"\x80", b"\x00", b"\x01", b"\x02", b"\x03", b"\x04")
    )


@main.command()
@click.option(
    "--cookie",
    prompt=True,
    required=True,
    help="imdb.com Cookie header",
)
@click.option(
    "-o",
    "--output",
    type=click.File("wb"),
    default="cookies.pickle",
    help="imdb.com Cookie Jar file",
    envvar="IMDB_COOKIE_FILE",
)
def save_cookies(cookie: str, output: io.BufferedWriter) -> None:
    jar = _parse_cookie_header(cookie)
    pickle.dump(jar, output)


@main.command()
@click.option(
    "-t",
    "--type",
    type=click.Choice(["watchlist", "ratings"]),
    required=True,
)
@click.option(
    "-c",
    "--cookie-file",
    type=click.File("rb"),
    required=True,
    help="imdb.com Cookie Jar file",
    envvar="IMDB_COOKIE_FILE",
)
@click.option(
    "-o",
    "--output",
    type=click.File("w"),
    default="-",
    help="CSV output file",
)
@click.option(
    "-s",
    "--since",
    type=int,
    default=3600,
    help="Seconds since last export",
)
def download_export(
    type: Literal["watchlist", "ratings"],
    cookie_file: io.BufferedReader,
    output: io.TextIOWrapper,
    since: int,
) -> int:
    jar = _load_cookies(cookie_file)
    started_after = datetime.now() - timedelta(seconds=since)
    if export_url := _get_export_url(type=type, started_after=started_after, jar=jar):
        r = requests.get(export_url, headers=_IMDB_DEFAULT_HEADERS)
        r.raise_for_status()
        output.write(r.text)
        return 0
    else:
        logger.error("No export found")
        return 1


def _get_export_url(
    type: Literal["watchlist", "ratings"],
    started_after: datetime,
    jar: requests.cookies.RequestsCookieJar,
) -> str | None:
    url = "https://www.imdb.com/exports/"
    response = requests.get(url, headers=_IMDB_DEFAULT_HEADERS, cookies=jar)
    response.raise_for_status()

    selector = Selector(response.text)

    next_data: dict[str, Any] = {}
    for script_el in selector.css('script[id="__NEXT_DATA__"]::text'):
        next_data = json.loads(script_el.get())
    assert next_data, "Could not find __NEXT_DATA__"

    data = next_data["props"]["pageProps"]["mainColumnData"]

    nodes = [edge["node"] for edge in data["getExports"]["edges"]]

    if type == "watchlist":
        nodes = [
            node
            for node in nodes
            if node["exportType"] == "LIST"
            and node["listExportMetadata"]["name"] == "WATCHLIST"
        ]
    elif type == "ratings":
        nodes = [node for node in nodes if node["exportType"] == "RATINGS"]
    else:
        raise ValueError(f"Unknown export type: {type}")

    nodes = [
        node
        for node in nodes
        if datetime.strptime(node["startedOn"], "%Y-%m-%dT%H:%M:%S.%fZ") > started_after
    ]

    node = nodes[0] if nodes else None
    if not node:
        return None

    if node["status"]["id"] == "PROCESSING":
        sleep(30)
        logger.warning("Export is in progress...")
        return _get_export_url(type=type, started_after=started_after, jar=jar)

    assert node["status"]["id"] == "READY"

    url = node["resultUrl"]
    assert isinstance(url, str), "Expected resultUrl to be a string"
    assert url.startswith(
        "https://userdataexport-dataexportsbucket-prod.s3.amazonaws.com"
    )
    return url


if __name__ == "__main__":
    main()
