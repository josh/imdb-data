# imdb-data

IMDB scraper to fetch **your** watchlist, lists and ratings.

## Setup

Design to run via GitHub Actions. First, Fork this repository but only the `main` branch. Data is stored on `gh-pages` and probably don't want my personal data.

Then set up GitHub Action Repository secrets for the following:

- `IMDB_COOKIE`: The `Cookie` header value after you've logged in

Click "Watchlist" and see the URL bar for `https://www.imdb.com/user/ur***/watchlist`, then Edit for `https://www.imdb.com/list/ls**/edit`

- `IMDB_USER_ID`: The ID that starts with `ur`
- `IMDB_WATCHLIST_ID`: The ID that starts with `ls`

## CLI Usage

Not officially published on Python Package Index (PyPI), but you can install it directly from GitHub:

```
$ pip install git+https://github.com/josh/imdb-data.git
```

```
$ imdb-data
Usage: imdb-data [OPTIONS] COMMAND [ARGS]...

Options:
  -c, --cookie-file FILE  imdb.com Cookie Jar file  [required]
  -v, --verbose           Enable verbose logging
  --help                  Show this message and exit.

Commands:
  check-ratings
  check-watchlist
  download-export
  dump-cookies
  import-cookies
  watchlist-quicksync
```

## Lib Usage

**requirements.txt**

```
imdb-data @ git+https://github.com/josh/imdb-data@main
```

```python
import imdb_data
import requests

jar: requests.cookies.RequestsCookieJar = pickle.load(open("cookies.pickle", "rb"))
csvtext = get_export_text(jar, "watchlist")
print(csvtext)
```
