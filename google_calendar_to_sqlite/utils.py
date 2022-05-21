from contextlib import contextmanager
import click
import httpx
import itertools
from time import sleep


class APIClient:
    class Error(click.ClickException):
        pass

    timeout = 30.0

    def __init__(self, refresh_token, client_id, client_secret, logger=None):
        self.refresh_token = refresh_token
        self.access_token = None
        self.client_id = client_id
        self.client_secret = client_secret
        self.log = logger or (lambda s: None)

    def get_access_token(self, force_refresh=False):
        if self.access_token and not force_refresh:
            return self.access_token
        url = "https://www.googleapis.com/oauth2/v4/token"
        self.log("POST {}".format(url))
        data = httpx.post(
            url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=self.timeout,
        ).json()
        if "error" in data:
            raise self.Error(str(data))
        self.access_token = data["access_token"]
        return self.access_token

    def get(
        self,
        url,
        params=None,
        headers=None,
        allow_token_refresh=True,
        transport_retries=2,
    ):
        headers = headers or {}
        headers["Authorization"] = "Bearer {}".format(self.get_access_token())
        self.log("GET: {} {}".format(url, params or "").strip())
        try:
            response = httpx.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
        except httpx.TransportError as ex:
            if transport_retries:
                sleep(2)
                self.log("  Got {}, retrying".format(ex.__class__.__name__))
                return self.get(
                    url,
                    params,
                    headers,
                    allow_token_refresh=allow_token_refresh,
                    transport_retries=transport_retries - 1,
                )
            else:
                raise

        if response.status_code == 401 and allow_token_refresh:
            # Try again after refreshing the token
            self.get_access_token(force_refresh=True)
            return self.get(url, params, headers, allow_token_refresh=False)
        return response

    def post(self, url, data=None, headers=None, allow_token_refresh=True):
        headers = headers or {}
        headers["Authorization"] = "Bearer {}".format(self.get_access_token())
        self.log("POST: {}".format(url))
        response = httpx.post(url, data=data, headers=headers, timeout=self.timeout)
        if response.status_code == 403 and allow_token_refresh:
            self.get_access_token(force_refresh=True)
            return self.post(url, data, headers, allow_token_refresh=False)
        return response

    @contextmanager
    def stream(self, method, url, params=None):
        with httpx.stream(
            method,
            url,
            params=params,
            headers={"Authorization": "Bearer {}".format(self.get_access_token())},
        ) as stream:
            yield stream


def paginate_all(client, url, pagination_key):
    next_page_token = None
    while True:
        params = {}
        if next_page_token is not None:
            params["pageToken"] = next_page_token
        response = client.get(
            url,
            params=params,
        )
        data = response.json()
        if response.status_code != 200:
            raise click.ClickException(json.dumps(data, indent=4))
        # Paginate using the specified key and nextPageToken
        if pagination_key not in data:
            raise click.ClickException(
                "paginate key {} not found in {}".format(
                    repr(pagination_key), repr(list(data.keys()))
                )
            )
        yield from data[pagination_key]

        next_page_token = data.get("nextPageToken")
        if not next_page_token:
            break


def flatten_keys(d, keys=None):
    for key, value in d.items():
        if isinstance(value, dict) and keys is not None and key in keys:
            for key2, value2 in flatten_keys(value):
                yield key + "_" + key2, value2
        else:
            yield key, value
