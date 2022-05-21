from os import access
import click
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx
import itertools
import json
import pathlib
import sqlite_utils
import sys
import textwrap
import urllib.parse
from .utils import (
    APIClient,
    paginate_all,
    flatten_keys,
)

GOOGLE_CLIENT_ID = (
    "184325416553-nu5ci563v36rmj9opdl7mah786anbkrq.apps.googleusercontent.com"
)
# It's OK to publish this secret in application source code
GOOGLE_CLIENT_SECRET = "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"
DEFAULT_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


def start_auth_url(google_client_id, scope):
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {
            "access_type": "offline",
            "client_id": google_client_id,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "response_type": "code",
            "scope": scope,
        }
    )


@click.group()
@click.version_option()
def cli():
    "Create a SQLite database containing your data from Google Calendar"


@cli.command()
@click.argument(
    "database",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=False),
    required=False,
)
@click.option(
    "-a",
    "--auth",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=False),
    default="auth.json",
    help="Path to load token, defaults to auth.json",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Send verbose output to stderr",
)
def calendars(database, auth, verbose):
    db = sqlite_utils.Database(database or ":memory:")
    kwargs = load_tokens(auth)
    if verbose:
        kwargs["logger"] = lambda s: click.echo(s, err=True)
    client = APIClient(**kwargs)
    url = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
    calendars = paginate_all(client, url, "items")
    db["calendars"].upsert_all(
        (dict(calendar, name=calendar["summary"]) for calendar in calendars),
        pk="id",
        column_order=("id", "name", "description"),
    )
    click.echo(
        "\n".join(
            "{name}: {id}".format(**calendar) for calendar in db["calendars"].rows
        )
    )


@cli.command()
@click.argument(
    "database",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=False),
)
@click.argument("calendars", nargs=-1)
@click.option(
    "-a",
    "--auth",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=False),
    default="auth.json",
    help="Path to load token, defaults to auth.json",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Send verbose output to stderr",
)
def events(database, calendars, auth, verbose):
    db = sqlite_utils.Database(database)
    kwargs = load_tokens(auth)
    if verbose:
        kwargs["logger"] = lambda s: click.echo(s, err=True)
    client = APIClient(**kwargs)

    if not calendars:
        # Do all of them
        calendars = [
            c["id"]
            for c in client.get(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList"
            ).json()["items"]
        ]

    for calendar_id in calendars:
        url = "https://www.googleapis.com/calendar/v3/calendars/{}/events".format(
            calendar_id
        )
        events = paginate_all(client, url, "items")
        db["calendars"].upsert(
            {
                "id": calendar_id,
            },
            pk="id",
        )
        # Flatten specific keys
        events = (
            dict(flatten_keys(dict(event, calendar_id=calendar_id), ("start", "end")))
            for event in events
        )
        db["events"].insert_all(
            events,
            pk="id",
            column_order=(
                "id",
                "summary",
                "location",
                "start_dateTime",
                "end_dateTime",
                "description",
                "calendar_id",
            ),
            alter=True,
            foreign_keys=(("calendar_id", "calendars", "id"),),
        )


@cli.command()
@click.option(
    "-a",
    "--auth",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=False),
    default="auth.json",
    help="Path to save token, defaults to auth.json",
)
@click.option("--google-client-id", help="Custom Google client ID")
@click.option("--google-client-secret", help="Custom Google client secret")
@click.option("--scope", help="Custom token scope")
def auth(auth, google_client_id, google_client_secret, scope):
    "Authenticate user and save credentials"
    if google_client_id is None:
        google_client_id = GOOGLE_CLIENT_ID
    if google_client_secret is None:
        google_client_secret = GOOGLE_CLIENT_SECRET
    if scope is None:
        scope = DEFAULT_SCOPE

    click.echo("Visit the following URL to authenticate with Google Calendar")
    click.echo("")
    click.echo(start_auth_url(google_client_id, scope))
    click.echo("")
    click.echo("Then return here and paste in the resulting code:")
    copied_code = click.prompt("Paste code here", hide_input=True)
    response = httpx.post(
        "https://www.googleapis.com/oauth2/v4/token",
        data={
            "code": copied_code,
            "client_id": google_client_id,
            "client_secret": google_client_secret,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
            "grant_type": "authorization_code",
        },
    )
    tokens = response.json()
    if "error" in tokens:
        message = "{error}: {error_description}".format(**tokens)
        raise click.ClickException(message)
    if "refresh_token" not in tokens:
        raise click.ClickException("No refresh_token in response")
    # Read existing file and add refresh_token to it
    try:
        auth_data = json.load(open(auth))
    except (ValueError, FileNotFoundError):
        auth_data = {}
    info = {"refresh_token": tokens["refresh_token"]}
    if google_client_id != GOOGLE_CLIENT_ID:
        info["google_client_id"] = google_client_id
    if google_client_secret != GOOGLE_CLIENT_SECRET:
        info["google_client_secret"] = google_client_secret
    if scope != DEFAULT_SCOPE:
        info["scope"] = scope
    auth_data["google-calendar-to-sqlite"] = info
    with open(auth, "w") as fp:
        fp.write(json.dumps(auth_data, indent=4))
    # chmod 600 to avoid other users on the shared machine reading it
    pathlib.Path(auth).chmod(0o600)


@cli.command()
@click.option(
    "-a",
    "--auth",
    type=click.Path(file_okay=True, dir_okay=False, allow_dash=False),
    default="auth.json",
    help="Path to load token, defaults to auth.json",
)
def revoke(auth):
    "Revoke the token stored in auth.json"
    tokens = load_tokens(auth)
    response = httpx.get(
        "https://accounts.google.com/o/oauth2/revoke",
        params={
            "token": tokens["refresh_token"],
        },
    )
    if "error" in response.json():
        raise click.ClickException(response.json()["error"])


def load_tokens(auth):
    try:
        token_info = json.load(open(auth))["google-calendar-to-sqlite"]
    except (KeyError, FileNotFoundError):
        raise click.ClickException(
            "Could not find google-calendar-to-sqlite in auth.json"
        )
    return {
        "refresh_token": token_info["refresh_token"],
        "client_id": token_info.get("google_client_id", GOOGLE_CLIENT_ID),
        "client_secret": token_info.get("google_client_secret", GOOGLE_CLIENT_SECRET),
    }
