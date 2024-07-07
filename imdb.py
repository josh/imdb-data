import io
import json
import logging
import pickle
from datetime import datetime, timedelta
from time import sleep
from typing import Any, Literal, NewType

import click
import requests
from parsel import Selector

_EPOCH: datetime = datetime(1970, 1, 1)

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


ListID = NewType("ListID", str)
ExportID = Literal["watchlist", "ratings"] | ListID
Status = Literal["NOT_FOUND", "READY", "PROCESSING"]

_EXPORTS_URL = "https://www.imdb.com/exports/"


def parse_export_id(value: str) -> ExportID | None:
    if value.startswith("ls"):
        return ListID(value)
    elif value == "watchlist":
        return "watchlist"
    elif value == "ratings":
        return "ratings"
    else:
        return None


def get_export_text(
    jar: requests.cookies.RequestsCookieJar,
    export_id: ExportID | None = None,
    started_after: datetime = _EPOCH,
    max_time: timedelta = timedelta(minutes=5),
) -> str | None:
    if url := get_export_url(
        jar=jar,
        export_id=export_id,
        started_after=started_after,
        max_time=max_time,
    ):
        r = requests.get(url, headers=_IMDB_DEFAULT_HEADERS, allow_redirects=True)
        r.raise_for_status()
        return r.content.decode("utf-8")
    else:
        return None


def get_export_url(
    jar: requests.cookies.RequestsCookieJar,
    export_id: ExportID | None = None,
    started_after: datetime = _EPOCH,
    max_time: timedelta = timedelta(minutes=5),
) -> str | None:
    started_at = datetime.now()
    status, url = get_export_status(
        jar=jar,
        export_id=export_id,
        started_after=started_after,
    )
    if status == "READY":
        return url
    elif status == "NOT_FOUND":
        return None
    elif status == "PROCESSING":
        wait = 1
        while datetime.now() - started_at < max_time:
            logger.warning("Export is in progress, waiting %d seconds...", wait)
            sleep(wait)
            status, url = get_export_status(
                jar=jar,
                export_id=export_id,
                started_after=started_after,
            )
            if status == "READY":
                return url
            wait *= 2

        logger.error("Export is still processing, but timed out")
        return None


def get_export_status(
    jar: requests.cookies.RequestsCookieJar,
    export_id: ExportID | None = None,
    started_after: datetime = _EPOCH,
) -> tuple[Status, str]:
    response = requests.get(_EXPORTS_URL, headers=_IMDB_DEFAULT_HEADERS, cookies=jar)
    response.raise_for_status()

    selector = Selector(response.text)

    next_data: dict[str, Any] = {}
    for script_el in selector.css('script[id="__NEXT_DATA__"]::text'):
        next_data = json.loads(script_el.get())
    assert next_data, "Could not find __NEXT_DATA__"

    data = next_data["props"]["pageProps"]["mainColumnData"]

    nodes = [edge["node"] for edge in data["getExports"]["edges"]]
    logger.debug("Found %d exports", len(nodes))

    if export_id is None:
        pass
    elif export_id == "watchlist":
        nodes = [
            node
            for node in nodes
            if node["exportType"] == "LIST"
            and node["listExportMetadata"]["name"] == "WATCHLIST"
        ]
    elif export_id == "ratings":
        nodes = [node for node in nodes if node["exportType"] == "RATINGS"]
    elif export_id.startswith("ls"):
        nodes = [
            node
            for node in nodes
            if node["exportType"] == "LIST"
            and node["listExportMetadata"]["id"] == export_id
        ]
    else:
        raise ValueError(f"Unknown export ID: {export_id}")

    nodes = [
        node
        for node in nodes
        if datetime.strptime(node["startedOn"], "%Y-%m-%dT%H:%M:%S.%fZ") > started_after
    ]
    logger.debug("Found %d matching exports", len(nodes))

    node = nodes[0] if nodes else None
    if not node:
        logger.debug("No matching exports found")
        return ("NOT_FOUND", "")

    if node["status"]["id"] == "PROCESSING":
        return ("PROCESSING", "")

    assert node["status"]["id"] == "READY"

    url = node["resultUrl"]
    assert isinstance(url, str), "Expected resultUrl to be a string"
    assert url.startswith(
        "https://userdataexport-dataexportsbucket-prod.s3.amazonaws.com"
    )
    return ("READY", url)


class ExportIDParam(click.ParamType):
    name = "export_id"

    def convert(
        self,
        value: str,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> ExportID:
        return parse_export_id(value) or self.fail(
            f"Invalid export ID: {value}", param, ctx
        )


@main.command()
@click.argument(
    "export_id",
    type=ExportIDParam(),
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
    export_id: ExportID,
    cookie_file: io.BufferedReader,
    output: io.TextIOWrapper,
    since: int,
) -> int:
    jar = _load_cookies(cookie_file)
    started_after = datetime.now() - timedelta(seconds=since)

    if export_text := get_export_text(
        export_id=export_id,
        started_after=started_after,
        jar=jar,
    ):
        output.write(export_text)
        return 0
    else:
        click.echo("No export found", err=True)
        return 1


if __name__ == "__main__":
    main()
