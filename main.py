import csv
import io
import json
import logging
import pickle
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Any, Literal, NewType, TypedDict, cast

import click
import requests
from parsel import Selector

_EPOCH: datetime = datetime(1970, 1, 1)

_IMDB_DEFAULT_HEADERS = {
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
}

_IMDB_GRAPHQL_URL = "https://api.graphql.imdb.com/"

_IMDB_GRAPHQL_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Accept": "application/graphql+json, application/json",
    "Accept-Language": "en-US,en;q=0.5",
    "content-type": "application/json",
    "x-amzn-sessionid": "",
    "x-imdb-client-name": "imdb-web-next-localized",
    "x-imdb-user-country": "US",
    "x-imdb-user-language": "en-US",
}

_WATCHLIST_URL = "https://www.imdb.com/list/watchlist"
_WATCHLIST_TEMPLATE_URL = "https://www.imdb.com/user/{user_id}/watchlist/"
_RATINGS_URL = "https://www.imdb.com/list/ratings"
_RATINGS_TEMPLATE_URL = "https://www.imdb.com/user/{user_id}/ratings/"
_EXPORTS_URL = "https://www.imdb.com/exports/"

UserID = NewType("UserID", str)
ListID = NewType("ListID", str)
ExportID = Literal["watchlist", "ratings"] | ListID
Status = Literal["NOT_FOUND", "READY", "PROCESSING"]


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


class ListIDParam(click.ParamType):
    name = "list_id"

    def convert(
        self,
        value: str,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> ListID:
        if value.startswith("ls"):
            return ListID(value)
        else:
            return self.fail(f"Invalid list ID: {value}", param, ctx)


class UserIDParam(click.ParamType):
    name = "user_id"

    def convert(
        self,
        value: str,
        param: click.Parameter | None,
        ctx: click.Context | None,
    ) -> UserID:
        if value.startswith("ur"):
            return UserID(value)
        else:
            return self.fail(f"Invalid user ID: {value}", param, ctx)


logger = logging.getLogger("imdb-data")


@contextmanager
def _open_cookie_jar(
    cookie_file: Path,
) -> Generator[requests.cookies.RequestsCookieJar, None, None]:
    did_change = False

    if cookie_file.exists():
        cookies = pickle.load(cookie_file.open("rb"))
        assert isinstance(cookies, requests.cookies.RequestsCookieJar)
    else:
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        cookies = requests.cookies.RequestsCookieJar()
        did_change = True

    old_cookies = cookies.copy()
    new_cookies = cookies.copy()

    try:
        yield new_cookies
    finally:
        for new_cookie in new_cookies:
            old_cookie_value = old_cookies.get(
                name=new_cookie.name,
                domain=new_cookie.domain,
            )
            if old_cookie_value != new_cookie.value:
                logger.debug("Cookie %s changed", new_cookie.name)
                did_change = True

        if did_change:
            logger.info("Saving cookies")
            pickle.dump(new_cookies, cookie_file.open("wb"))
        else:
            logger.debug("No changes to cookies")


def watchlist_url(user_id: UserID | None = None) -> str:
    if user_id:
        return _WATCHLIST_TEMPLATE_URL.format(user_id=user_id)
    else:
        return _WATCHLIST_URL


def ratings_url(user_id: UserID | None = None) -> str:
    if user_id:
        return _RATINGS_TEMPLATE_URL.format(user_id=user_id)
    else:
        return _RATINGS_URL


@click.group()
@click.option(
    "-c",
    "--cookie-file",
    type=click.Path(file_okay=True, dir_okay=False, writable=True, path_type=Path),
    required=True,
    help="imdb.com Cookie Jar file",
    envvar="IMDB_COOKIE_FILE",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Enable verbose logging",
    envvar="ACTIONS_RUNNER_DEBUG",
)
@click.pass_context
def main(
    ctx: click.Context,
    cookie_file: Path,
    verbose: bool,
) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)
    ctx.obj = ctx.with_resource(_open_cookie_jar(cookie_file))


def _get_nextjs_data(response: requests.Response) -> dict[str, Any]:
    selector = Selector(response.text)
    for script_el in selector.css('script[id="__NEXT_DATA__"]::text'):
        json_text = script_el.get()
        data = json.loads(json_text)
        return cast(dict[str, Any], data)
    raise ValueError("Could not find __NEXT_DATA__")


@main.command()
@click.option(
    "--cookie",
    prompt=True,
    required=True,
    help="imdb.com Cookie header",
    envvar="IMDB_COOKIE",
)
@click.pass_obj
def import_cookies(jar: requests.cookies.RequestsCookieJar, cookie: str) -> None:
    for c in cookie.strip().split("; "):
        key, value = c.strip().split("=", 1)
        jar.set(key, value)


@main.command()
@click.pass_obj
def dump_cookies(jar: requests.cookies.RequestsCookieJar) -> None:
    print("; ".join(f"{cookie.name}={cookie.value}" for cookie in jar))


def get_user_and_watchlist_id(
    jar: requests.cookies.RequestsCookieJar,
    user_id: UserID | None = None,
) -> tuple[str, str]:
    url = watchlist_url(user_id=user_id)
    response = requests.get(url, headers=_IMDB_DEFAULT_HEADERS, cookies=jar)
    response.raise_for_status()
    next_data = _get_nextjs_data(response)
    data = next_data["props"]["pageProps"]["aboveTheFoldData"]
    return (data["authorId"], data["listId"])


@main.command()
@click.pass_obj
def user_id(jar: requests.cookies.RequestsCookieJar) -> None:
    user_id, _ = get_user_and_watchlist_id(jar)
    click.echo(user_id)


@main.command()
@click.pass_obj
def watchlist_id(jar: requests.cookies.RequestsCookieJar) -> None:
    _, watchlist_id = get_user_and_watchlist_id(jar)
    click.echo(watchlist_id)


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
    export_id: ExportID,
    started_after: datetime = _EPOCH,
    max_time: timedelta = timedelta(minutes=5),
) -> str | None:
    url = get_export_url(
        jar=jar,
        export_id=export_id,
        started_after=started_after,
        max_time=max_time,
    )
    assert url, "Failed to get export URL"
    r = requests.get(url, headers=_IMDB_DEFAULT_HEADERS, allow_redirects=True)
    r.raise_for_status()
    return r.content.decode("utf-8")


def get_export_url(
    jar: requests.cookies.RequestsCookieJar,
    export_id: ExportID,
    started_after: datetime = _EPOCH,
    max_time: timedelta = timedelta(minutes=5),
) -> str:
    started_at = datetime.now()
    status, url = get_export_status(
        jar=jar,
        export_id=export_id,
        started_after=started_after,
    )
    if status == "READY":
        return url
    elif status == "NOT_FOUND":
        logger.warning("Export not found, enqueuing...")
        queue_export(jar=jar, export_id=export_id)
        sleep(1)
        return get_export_url(
            jar=jar,
            export_id=export_id,
            started_after=started_after,
            max_time=max_time,
        )
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
        raise TimeoutError("Export timed out")


class _ExportNodeStatus(TypedDict):
    id: Literal["READY", "PROCESSING"]


class _ListExportMetadata(TypedDict):
    id: str
    listClassId: Literal["LIST", "WATCH_LIST"]
    listType: Literal["TITLES"]
    name: str


class _ExportDetail(TypedDict):
    startedOn: str
    totalExportedObjects: int
    status: _ExportNodeStatus
    resultUrl: str
    expiresOn: str
    exportType: Literal["LIST", "RATINGS"]
    listExportMetadata: _ListExportMetadata


_YOUR_EXPORTS_VARIABLES = {
    "first": 2,
    "locale": "en-US",
}
_YOUR_EXPORTS_EXTENSIONS = {
    "persistedQuery": {
        "sha256Hash": "5470e249d72b3078b1ec2c2adc0a4a74ecd822e3333d22182fc71fb78588dcb6",
        "version": 1,
    }
}
_YOUR_EXPORTS_PARAMS = {
    "operationName": "YourExports",
    "variables": json.dumps(_YOUR_EXPORTS_VARIABLES, separators=(",", ":")),
    "extensions": json.dumps(_YOUR_EXPORTS_EXTENSIONS, separators=(",", ":")),
}


def _get_export_nodes_graphql(
    jar: requests.cookies.RequestsCookieJar,
) -> list[_ExportDetail]:
    headers = _IMDB_GRAPHQL_DEFAULT_HEADERS.copy()
    if session_id := jar.get("session-id"):
        headers["x-amzn-sessionid"] = session_id
    response = requests.get(
        _IMDB_GRAPHQL_URL,
        headers=headers,
        cookies=jar,
        params=_YOUR_EXPORTS_PARAMS,
        allow_redirects=False,
    )
    response.raise_for_status()
    data = response.json()["data"]
    return [edge["node"] for edge in data["getExports"]["edges"]]


def _get_export_nodes_html(
    jar: requests.cookies.RequestsCookieJar,
) -> list[_ExportDetail]:
    response = requests.get(_EXPORTS_URL, headers=_IMDB_DEFAULT_HEADERS, cookies=jar)
    response.raise_for_status()
    next_data = _get_nextjs_data(response)
    data = next_data["props"]["pageProps"]["mainColumnData"]
    return [edge["node"] for edge in data["getExports"]["edges"]]


def get_export_status(
    jar: requests.cookies.RequestsCookieJar,
    export_id: ExportID | None = None,
    started_after: datetime = _EPOCH,
) -> tuple[Status, str]:
    nodes = _get_export_nodes_html(jar=jar)
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


_START_LIST_EXPORT_QUERY = """
mutation StartListExport($listId: ID!) {
  createListExport(input: {listId: $listId}) {
    status {
      id
    }
  }
}
"""

_START_RATINGS_EXPORT_QUERY = """
mutation StartRatingsExport {
  createRatingsExport {
    status {
      id
    }
  }
}
"""


def queue_export(
    jar: requests.cookies.RequestsCookieJar,
    export_id: ExportID,
) -> None:
    post_data: dict[str, Any] = {}

    if export_id == "ratings":
        post_data = {
            "query": _START_RATINGS_EXPORT_QUERY,
            "operationName": "StartRatingsExport",
            "variables": {"listId": "RATINGS"},
        }
    elif export_id == "watchlist":
        _, watchlist_id = get_user_and_watchlist_id(jar)
        post_data = {
            "query": _START_LIST_EXPORT_QUERY,
            "operationName": "StartListExport",
            "variables": {"listId": watchlist_id},
        }
    elif export_id.startswith("ls"):
        post_data = {
            "query": _START_LIST_EXPORT_QUERY,
            "operationName": "StartListExport",
            "variables": {"listId": export_id},
        }
    else:
        raise ValueError(f"Unknown export ID: {export_id}")

    headers = _IMDB_GRAPHQL_DEFAULT_HEADERS.copy()
    if session_id := jar.get("session-id"):
        headers["x-amzn-sessionid"] = session_id

    r = requests.post(
        _IMDB_GRAPHQL_URL,
        headers=_IMDB_GRAPHQL_DEFAULT_HEADERS,
        cookies=jar,
        json=post_data,
    )
    r.raise_for_status()
    data = r.json()

    operation_name = post_data["operationName"]
    assert data["data"][operation_name]["status"]["id"] == "PROCESSING"
    return None


@main.command()
@click.argument(
    "export_id",
    type=ExportIDParam(),
    required=True,
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
@click.pass_obj
def download_export(
    jar: requests.cookies.RequestsCookieJar,
    export_id: ExportID,
    output: io.TextIOWrapper,
    since: int,
) -> int:
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


def get_watchlist_last_modified(
    jar: requests.cookies.RequestsCookieJar,
    user_id: UserID | None = None,
) -> datetime:
    url = watchlist_url(user_id=user_id)
    response = requests.get(url, headers=_IMDB_DEFAULT_HEADERS, cookies=jar)
    response.raise_for_status()
    next_data = _get_nextjs_data(response)
    data = next_data["props"]["pageProps"]["mainColumnData"]
    watchlist = data["predefinedList"]
    last_modified = datetime.strptime(
        watchlist["lastModifiedDate"], "%Y-%m-%dT%H:%M:%SZ"
    )
    return last_modified


def get_recently_rated_ids(
    jar: requests.cookies.RequestsCookieJar,
    user_id: UserID | None = None,
) -> list[str]:
    url = ratings_url(user_id=user_id)
    response = requests.get(url, headers=_IMDB_DEFAULT_HEADERS, cookies=jar)
    response.raise_for_status()
    next_data = _get_nextjs_data(response)
    data = next_data["props"]["pageProps"]

    recently_rated_title_ids: list[str] = [
        edge["node"]["title"]["id"]
        for edge in data["mainColumnData"]["advancedTitleSearch"]["edges"]
    ]
    return recently_rated_title_ids


@main.command()
@click.argument(
    "csv_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "-u",
    "--user-id",
    type=UserIDParam(),
    help="IMDB User ID",
    envvar="IMDB_USER_ID",
)
@click.pass_obj
def check_watchlist(
    jar: requests.cookies.RequestsCookieJar,
    csv_path: Path,
    user_id: UserID | None,
) -> None:
    csv_mtime: datetime = datetime.fromtimestamp(csv_path.stat().st_mtime)
    imdb_last_modified = get_watchlist_last_modified(jar=jar, user_id=user_id)
    if csv_mtime <= imdb_last_modified:
        click.echo("outdated=true")
    else:
        click.echo("outdated=false")


@main.command()
@click.argument(
    "csv_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "-u",
    "--user-id",
    type=UserIDParam(),
    help="IMDB User ID",
    envvar="IMDB_USER_ID",
)
@click.pass_obj
def check_ratings(
    jar: requests.cookies.RequestsCookieJar,
    csv_path: Path,
    user_id: UserID | None,
) -> None:
    csv_title_ids: set[str] = set(
        row["Const"] for row in csv.DictReader(csv_path.open("r"))
    )

    recently_rated_ids = get_recently_rated_ids(jar=jar, user_id=user_id)
    for title_id in recently_rated_ids:
        if title_id not in csv_title_ids:
            click.echo("outdated=true")
            return

    click.echo("outdated=false")
    return


if __name__ == "__main__":
    main()
