from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import lyriq


AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE_URL = "https://api.spotify.com/v1"
DEFAULT_PORT = 4380
VERSION = "0.1.0"
DEFAULT_SCOPES = (
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
    "playlist-modify-private",
)
CONFIG_PATH = Path.home() / ".config" / "spotify-cli" / "config.json"
TOKEN_PATH = Path.home() / ".cache" / "spotify-cli" / "token.json"
DEFAULT_WEB_PORT = 4381
DEFAULT_WEB_TIMEOUT = 60
DEFAULT_WEB_PLAYER_NAME = "spotify-cli Web Player"
DEFAULT_WEB_PROFILE_PATH = Path.home() / ".cache" / "spotify-cli" / "web-player"


class SpotifyCliError(RuntimeError):
    pass


@dataclass
class AuthCallbackResult:
    code: str | None = None
    error: str | None = None
    state: str | None = None


@dataclass
class BrowserWindowHandle:
    label: str
    wait_for_exit: Any
    close_window: Any

    def wait(self) -> None:
        self.wait_for_exit()

    def close(self) -> None:
        self.close_window()


@dataclass
class WebPlayerState:
    device_id: str | None = None
    error: str | None = None
    ready_event: threading.Event = field(default_factory=threading.Event)
    state_lock: threading.Lock = field(default_factory=threading.Lock)

    def set_ready(self, device_id: str) -> None:
        with self.state_lock:
            self.device_id = device_id
            self.error = None
            self.ready_event.set()

    def set_error(self, message: str) -> None:
        with self.state_lock:
            self.error = message
            self.ready_event.set()

    def snapshot(self) -> tuple[str | None, str | None]:
        with self.state_lock:
            return self.device_id, self.error


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_code_verifier() -> str:
    return secrets.token_urlsafe(72)


def build_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def epoch_seconds() -> int:
    return int(time.time())


def format_ms(milliseconds: int | None) -> str:
    if milliseconds is None:
        return "--:--"
    seconds = max(milliseconds // 1000, 0)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def truncate_text(value: str, limit: int) -> str:
    return value[:limit] + ("..." if len(value) > limit else "")


def clear_screen() -> None:
    print("\033c", end="")


def normalize_spotify_uri(value: str) -> str:
    if value.startswith("spotify:"):
        return value

    parsed = urlparse(value)
    if parsed.netloc not in {"open.spotify.com", "play.spotify.com"}:
        raise SpotifyCliError("Expected a Spotify URI or open.spotify.com URL.")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise SpotifyCliError("Could not determine the Spotify resource from the URL.")

    resource_type, resource_id = parts[0], parts[1]
    if "?" in resource_id:
        resource_id = resource_id.split("?", 1)[0]
    return f"spotify:{resource_type}:{resource_id}"


def build_play_payload(uri: str) -> dict[str, Any]:
    normalized = normalize_spotify_uri(uri)
    if normalized.startswith(("spotify:track:", "spotify:episode:")):
        return {"uris": [normalized]}
    return {"context_uri": normalized}


def positive_limit(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 10:
        raise argparse.ArgumentTypeError("--limit must be between 1 and 10.")
    return parsed


def positive_index(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("Selection index must be 1 or greater.")
    return parsed


def format_search_result(index: int, track: dict[str, Any]) -> str:
    artists = ", ".join(artist["name"] for artist in track.get("artists", [])) or "Unknown artist"
    album = track.get("album", {}).get("name", "Unknown album")
    uri = track.get("uri", "")
    track_id = track.get("id", "")
    return (
        f"{index}. {artists} - {track.get('name', 'Unknown track')}\n"
        f"   album: {album}\n"
        f"   uri:   {uri}\n"
        f"   id:    {track_id}"
    )


def choose_track_from_results(
    tracks: list[dict[str, Any]],
    *,
    selection: int | None = None,
    interactive: bool = False,
) -> dict[str, Any] | None:
    if selection is None and not interactive:
        return None

    if interactive:
        if not sys.stdin.isatty():
            raise SpotifyCliError("Interactive selection requires a TTY. Use `search --play N` instead.")
        prompt = f"Choose a result to play [1-{len(tracks)}], or press Enter to cancel: "
        raw = input(prompt).strip()
        if not raw:
            print("Selection cancelled.")
            return None
        try:
            selection = positive_index(raw)
        except (ValueError, argparse.ArgumentTypeError) as exc:
            raise SpotifyCliError(str(exc)) from exc

    assert selection is not None
    if selection > len(tracks):
        raise SpotifyCliError(f"Selection {selection} is out of range for {len(tracks)} result(s).")
    return tracks[selection - 1]


def default_playlist_name(query: str) -> str:
    return f"spotify-cli search: {query}"


def resolve_client_id(args: argparse.Namespace) -> str:
    if getattr(args, "client_id", None):
        return args.client_id

    env_client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if env_client_id:
        return env_client_id

    config = load_json(Path(args.config_path))
    client_id = config.get("client_id")
    if client_id:
        return str(client_id)

    raise SpotifyCliError(
        "Spotify client ID is required. Pass --client-id, set SPOTIFY_CLIENT_ID, "
        "or store it with `spotifycli --client-id ... auth login --save-client-id`."
    )


def maybe_store_client_id(args: argparse.Namespace, client_id: str) -> None:
    if not getattr(args, "save_client_id", False):
        return

    config_path = Path(args.config_path)
    config = load_json(config_path)
    config["client_id"] = client_id
    save_json(config_path, config)


def build_authorize_url(client_id: str, redirect_uri: str, code_challenge: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(DEFAULT_SCOPES),
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


class CallbackHandler(BaseHTTPRequestHandler):
    result: AuthCallbackResult
    event: threading.Event

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        self.result.code = query.get("code", [None])[0]
        self.result.error = query.get("error", [None])[0]
        self.result.state = query.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            (
                "<html><body><h1>Spotify CLI authorization received.</h1>"
                "<p>You can close this tab and return to the terminal.</p></body></html>"
            ).encode("utf-8")
        )
        self.event.set()

    def log_message(self, format: str, *args: Any) -> None:
        return


def wait_for_callback(port: int, expected_state: str, timeout_seconds: int = 180) -> str:
    result = AuthCallbackResult()
    event = threading.Event()

    class ServerHandler(CallbackHandler):
        pass

    ServerHandler.result = result
    ServerHandler.event = event

    try:
        httpd = HTTPServer(("127.0.0.1", port), ServerHandler)
    except OSError as exc:
        raise SpotifyCliError(
            f"Could not bind the callback server to 127.0.0.1:{port}. "
            "Pick a different --port and update the Spotify redirect URI to match."
        ) from exc
    server_thread = threading.Thread(target=httpd.handle_request, daemon=True)
    server_thread.start()

    if not event.wait(timeout_seconds):
        httpd.server_close()
        return prompt_for_callback_url(expected_state)

    httpd.server_close()
    if result.error:
        raise SpotifyCliError(f"Spotify authorization failed: {result.error}")
    if result.state != expected_state:
        raise SpotifyCliError("Spotify authorization failed: state mismatch.")
    if not result.code:
        raise SpotifyCliError("Spotify authorization failed: missing authorization code.")
    return result.code


def prompt_for_callback_url(expected_state: str) -> str:
    print("Timed out waiting for the browser callback.")
    print("Paste the full redirected URL from the browser to finish login:")
    redirected_url = input("> ").strip()
    parsed = urlparse(redirected_url)
    query = parse_qs(parsed.query)

    error = query.get("error", [None])[0]
    code = query.get("code", [None])[0]
    state = query.get("state", [None])[0]
    if error:
        raise SpotifyCliError(f"Spotify authorization failed: {error}")
    if state != expected_state:
        raise SpotifyCliError("Spotify authorization failed: state mismatch.")
    if not code:
        raise SpotifyCliError("Spotify authorization failed: missing authorization code.")
    return code


def http_json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    expected_statuses: set[int] | None = None,
) -> tuple[int, Any]:
    payload = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = Request(url, data=payload, headers=request_headers, method=method)
    expected = expected_statuses or {200}

    try:
        with urlopen(request) as response:
            status = response.getcode()
            raw = response.read()
    except HTTPError as exc:
        status = exc.code
        raw = exc.read()
        if status not in expected:
            detail = raw.decode("utf-8", errors="replace")
            if status == 403 and "Insufficient client scope" in detail:
                detail += (
                    "\nRe-run `spotifycli auth login` to grant any newly required Spotify scopes."
                )
            raise SpotifyCliError(f"Spotify API request failed ({status}): {detail}") from exc
        if not raw:
            return status, None
        return status, json.loads(raw.decode("utf-8"))

    if status not in expected:
        detail = raw.decode("utf-8", errors="replace")
        raise SpotifyCliError(f"Spotify API request failed ({status}): {detail}")
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return status, None


def exchange_code_for_token(client_id: str, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    form_body = urlencode(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    request = Request(
        TOKEN_URL,
        data=form_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SpotifyCliError(f"Could not exchange Spotify authorization code: {detail}") from exc

    payload["expires_at"] = epoch_seconds() + int(payload["expires_in"]) - 30
    return payload


def refresh_token(client_id: str, refresh_token_value: str) -> dict[str, Any]:
    form_body = urlencode(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
        }
    ).encode("utf-8")
    request = Request(
        TOKEN_URL,
        data=form_body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SpotifyCliError(f"Could not refresh Spotify token: {detail}") from exc

    payload["refresh_token"] = payload.get("refresh_token", refresh_token_value)
    payload["expires_at"] = epoch_seconds() + int(payload["expires_in"]) - 30
    return payload


def save_token(token_path: Path, token: dict[str, Any]) -> None:
    save_json(token_path, token)


def load_token(token_path: Path) -> dict[str, Any]:
    token = load_json(token_path)
    if not token:
        raise SpotifyCliError(
            "No Spotify token found. Run `spotifycli auth login` before using playback commands."
        )
    return token


def open_auth_url(url: str) -> bool:
    commands = [
        ["wslview", url],
        ["cmd.exe", "/C", "start", "", url],
        ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{url}'"],
        ["xdg-open", url],
    ]
    for command in commands:
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue

    try:
        return webbrowser.open(url)
    except webbrowser.Error:
        return False


class SpotifyClient:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client_id = resolve_client_id(args)
        self.token_path = Path(args.token_path)
        self.token = load_token(self.token_path)

    def ensure_access_token(self) -> str:
        if epoch_seconds() >= int(self.token.get("expires_at", 0)):
            self.token = refresh_token(self.client_id, self.token["refresh_token"])
            save_token(self.token_path, self.token)
        return str(self.token["access_token"])

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> tuple[int, Any]:
        query_string = f"?{urlencode(query)}" if query else ""
        url = f"{API_BASE_URL}{path}{query_string}"
        expected = set(expected_statuses or {200})
        expected_with_retry = set(expected)
        expected_with_retry.add(401)
        token = self.ensure_access_token()
        status, payload = http_json_request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            body=body,
            expected_statuses=expected_with_retry,
        )
        if status == 401 and self.token.get("refresh_token"):
            self.token = refresh_token(self.client_id, self.token["refresh_token"])
            save_token(self.token_path, self.token)
            token = self.ensure_access_token()
            return http_json_request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                body=body,
                expected_statuses=expected,
            )
        if status == 401:
            raise SpotifyCliError("Spotify access token is invalid. Run `spotifycli auth login` again.")
        return status, payload

    def player(self) -> dict[str, Any] | None:
        status, payload = self.request("GET", "/me/player", expected_statuses={200, 204})
        if status == 204:
            return None
        return payload

    def devices(self) -> list[dict[str, Any]]:
        _, payload = self.request("GET", "/me/player/devices")
        return payload.get("devices", [])

    def search_tracks(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        _, payload = self.request(
            "GET",
            "/search",
            query={"q": query, "type": "track", "limit": str(limit)},
        )
        return payload.get("tracks", {}).get("items", [])

    def create_playlist(self, name: str, *, description: str = "", public: bool = False) -> dict[str, Any]:
        _, payload = self.request(
            "POST",
            "/me/playlists",
            body={"name": name, "description": description, "public": public},
            expected_statuses={201},
        )
        return payload

    def add_items_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        self.request(
            "POST",
            f"/playlists/{playlist_id}/items",
            body={"uris": uris},
            expected_statuses={201},
        )

    def transfer(self, device_id: str, play: bool = False) -> None:
        self.request(
            "PUT",
            "/me/player",
            body={"device_ids": [device_id], "play": play},
            expected_statuses={200, 202, 204},
        )

    def play(self, *, device_id: str | None = None, body: dict[str, Any] | None = None) -> None:
        self.request(
            "PUT",
            "/me/player/play",
            query={"device_id": device_id} if device_id else None,
            body=body,
            expected_statuses={200, 202, 204},
        )

    def pause(self, *, device_id: str | None = None) -> None:
        self.request(
            "PUT",
            "/me/player/pause",
            query={"device_id": device_id} if device_id else None,
            expected_statuses={200, 202, 204},
        )

    def next_track(self, *, device_id: str | None = None) -> None:
        self.request(
            "POST",
            "/me/player/next",
            query={"device_id": device_id} if device_id else None,
            expected_statuses={200, 202, 204},
        )

    def previous_track(self, *, device_id: str | None = None) -> None:
        self.request(
            "POST",
            "/me/player/previous",
            query={"device_id": device_id} if device_id else None,
            expected_statuses={200, 202, 204},
        )


def prompt_device_selection(devices: list[dict[str, Any]]) -> str:
    if not sys.stdin.isatty():
        raise SpotifyCliError(
            "No active device found and cannot prompt (not a TTY). "
            "Use --device to specify a target device."
        )
    print("Available Spotify devices:")
    for i, device in enumerate(devices, 1):
        active = " (active)" if device.get("is_active") else ""
        print(f"  {i}. {device['name']} | type={device.get('type', '?')}{active}")
    raw = input(f"Choose a device [1-{len(devices)}]: ").strip()
    if not raw:
        raise SpotifyCliError("Device selection cancelled.")
    try:
        index = int(raw)
    except ValueError:
        raise SpotifyCliError(f"Invalid selection: {raw!r}")
    if index < 1 or index > len(devices):
        raise SpotifyCliError(f"Selection {index} is out of range.")
    return str(devices[index - 1]["id"])


def resolve_device_id(client: SpotifyClient, target: str | None) -> str | None:
    if target is None:
        return None

    devices = client.devices()
    exact_id = next((device for device in devices if device["id"] == target), None)
    if exact_id:
        return str(exact_id["id"])

    name_matches = [device for device in devices if device["name"].lower() == target.lower()]
    if len(name_matches) == 1:
        return str(name_matches[0]["id"])
    if len(name_matches) > 1:
        raise SpotifyCliError(f"Multiple devices are named {target!r}; use the device ID instead.")

    raise SpotifyCliError(f"Could not find a Spotify device matching {target!r}.")


def print_status(payload: dict[str, Any] | None) -> None:
    if not payload or not payload.get("item"):
        print("No active Spotify playback context.")
        return

    item = payload["item"]
    artists = ", ".join(artist["name"] for artist in item.get("artists", [])) or "Unknown artist"
    title = item.get("name", "Unknown track")
    progress = format_ms(payload.get("progress_ms"))
    duration = format_ms(item.get("duration_ms"))
    playback = "playing" if payload.get("is_playing") else "paused"
    device = payload.get("device", {}).get("name", "unknown device")
    print(f"{artists} - {title}")
    print(f"{progress} / {duration} | {playback} | {device}")


def get_current_item(payload: dict[str, Any] | None) -> tuple[dict[str, Any], str, str]:
    if not payload or not payload.get("item"):
        raise SpotifyCliError("No active Spotify playback context.")
    item = payload["item"]
    artists = ", ".join(artist["name"] for artist in item.get("artists", [])) or "Unknown artist"
    title = item.get("name", "Unknown track")
    return item, artists, title


def print_legacy_status(payload: dict[str, Any]) -> None:
    _, artists, title = get_current_item(payload)
    print(f"{artists} - {title}")


def print_legacy_status_short(payload: dict[str, Any]) -> None:
    _, artists, title = get_current_item(payload)
    print(f"{truncate_text(artists, 15)} - {truncate_text(title, 10)}")


def print_legacy_status_position(payload: dict[str, Any]) -> None:
    item, artists, title = get_current_item(payload)
    progress = format_ms(payload.get("progress_ms"))
    duration = format_ms(item.get("duration_ms"))
    print(f"{artists} - {title} ({progress}/{duration})")


def print_legacy_song(payload: dict[str, Any], short: bool = False) -> None:
    _, _, title = get_current_item(payload)
    print(truncate_text(title, 10) if short else title)


def print_legacy_artist(payload: dict[str, Any], short: bool = False) -> None:
    _, artists, _ = get_current_item(payload)
    print(truncate_text(artists, 15) if short else artists)


def print_legacy_album(payload: dict[str, Any]) -> None:
    item, _, _ = get_current_item(payload)
    print(item.get("album", {}).get("name", "Unknown album"))


def print_legacy_position(payload: dict[str, Any]) -> None:
    item, _, _ = get_current_item(payload)
    print(f"({format_ms(payload.get('progress_ms'))}/{format_ms(item.get('duration_ms'))})")


def print_legacy_playback_status(payload: dict[str, Any]) -> None:
    symbol = "▶" if payload.get("is_playing") else "▮▮"
    print(symbol)


def print_legacy_art_url(payload: dict[str, Any]) -> None:
    item, _, _ = get_current_item(payload)
    images = item.get("album", {}).get("images", [])
    if not images:
        raise SpotifyCliError("No album artwork is available for the current track.")
    print(images[0]["url"])


def print_legacy_lyrics(payload: dict[str, Any]) -> None:
    _, artists, title = get_current_item(payload)
    lyrics = lyriq.get_lyrics(title, artists)
    if lyrics is None:
        print(f"Lyrics for '{title}' by {artists} were not found.")
        return
    print(lyrics.plain_lyrics)


def command_auth_login(args: argparse.Namespace) -> int:
    client_id = resolve_client_id(args)
    redirect_uri = f"http://127.0.0.1:{args.port}/callback"
    state = secrets.token_urlsafe(24)
    verifier = build_code_verifier()
    challenge = build_code_challenge(verifier)
    url = build_authorize_url(client_id, redirect_uri, challenge, state)

    print("Opening Spotify authorization in your browser...")
    print(f"If it does not open automatically, open this URL manually:\n{url}")
    if not open_auth_url(url):
        print("Could not open a browser automatically.")

    code = wait_for_callback(args.port, state)
    token = exchange_code_for_token(client_id, code, verifier, redirect_uri)
    save_token(Path(args.token_path), token)
    maybe_store_client_id(args, client_id)
    print(f"Saved Spotify token to {args.token_path}.")
    return 0


def command_auth_logout(args: argparse.Namespace) -> int:
    token_path = Path(args.token_path)
    if token_path.exists():
        token_path.unlink()
        print(f"Removed {token_path}.")
    else:
        print("No stored token found.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    print_status(client.player())
    return 0


def command_devices(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    devices = client.devices()
    if not devices:
        print("No Spotify Connect devices are currently available.")
        return 0

    for device in devices:
        marker = "*" if device.get("is_active") else " "
        print(
            f"{marker} {device['name']} | id={device['id']} | type={device['type']} | "
            f"volume={device.get('volume_percent')} | restricted={device.get('is_restricted')}"
        )
    return 0


def command_transfer(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    device_id = resolve_device_id(client, args.target)
    if device_id is None:
        raise SpotifyCliError("Transfer requires a device name or device ID.")
    client.transfer(device_id, play=args.play)
    print(f"Transferred playback to {args.target}.")
    return 0


def command_play(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    device_id = resolve_device_id(client, args.device)
    body = build_play_payload(args.uri) if args.uri else None
    try:
        client.play(device_id=device_id, body=body)
    except SpotifyCliError as exc:
        if "NO_ACTIVE_DEVICE" not in str(exc) and "No active device" not in str(exc):
            raise
        devices = client.devices()
        if not devices:
            raise SpotifyCliError("No Spotify devices available. Open Spotify on a device first.")
        device_id = prompt_device_selection(devices)
        client.play(device_id=device_id, body=body)
    print("Playback started.")
    return 0


def command_pause(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    device_id = resolve_device_id(client, args.device)
    try:
        client.pause(device_id=device_id)
    except SpotifyCliError as exc:
        if "NO_ACTIVE_DEVICE" not in str(exc) and "No active device" not in str(exc):
            raise
        devices = client.devices()
        if not devices:
            raise SpotifyCliError("No Spotify devices available. Open Spotify on a device first.")
        device_id = prompt_device_selection(devices)
        client.pause(device_id=device_id)
    print("Playback paused.")
    return 0


def command_toggle(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    player = client.player()
    if not player:
        raise SpotifyCliError("No active playback context found for toggle.")
    if player.get("is_playing"):
        client.pause()
        print("Playback paused.")
    else:
        client.play()
        print("Playback started.")
    return 0


def command_next(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    device_id = resolve_device_id(client, args.device)
    try:
        client.next_track(device_id=device_id)
    except SpotifyCliError as exc:
        if "NO_ACTIVE_DEVICE" not in str(exc) and "No active device" not in str(exc):
            raise
        devices = client.devices()
        if not devices:
            raise SpotifyCliError("No Spotify devices available. Open Spotify on a device first.")
        device_id = prompt_device_selection(devices)
        client.next_track(device_id=device_id)
    print("Skipped to next track.")
    return 0


def command_prev(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    device_id = resolve_device_id(client, args.device)
    try:
        client.previous_track(device_id=device_id)
    except SpotifyCliError as exc:
        if "NO_ACTIVE_DEVICE" not in str(exc) and "No active device" not in str(exc):
            raise
        devices = client.devices()
        if not devices:
            raise SpotifyCliError("No Spotify devices available. Open Spotify on a device first.")
        device_id = prompt_device_selection(devices)
        client.previous_track(device_id=device_id)
    print("Returned to previous track.")
    return 0


def command_search(args: argparse.Namespace) -> int:
    client = SpotifyClient(args)
    tracks = client.search_tracks(args.query, limit=args.limit)
    if not tracks:
        print("No matching tracks found.")
        return 0

    for index, track in enumerate(tracks, start=1):
        if index > 1:
            print()
        print(format_search_result(index, track))

    device_id = resolve_device_id(client, args.device)
    if args.playlist is not None:
        playlist_name = args.playlist or default_playlist_name(args.query)
        playlist = client.create_playlist(
            playlist_name,
            description=f"Created by spotify-cli from search results for: {args.query}",
            public=False,
        )
        client.add_items_to_playlist(playlist["id"], [track["uri"] for track in tracks])
        client.play(device_id=device_id, body={"context_uri": playlist["uri"]})
        print()
        print(f"Playlist created and started: {playlist['name']}")
        print(f"Playlist URI: {playlist['uri']}")
        return 0

    chosen = choose_track_from_results(tracks, selection=args.play_index, interactive=args.interactive)
    if chosen is None:
        return 0

    client.play(device_id=device_id, body={"uris": [chosen["uri"]]})
    print()
    print(f"Playback started: {chosen['uri']}")
    return 0


def legacy_device_arg(args: argparse.Namespace) -> str | None:
    return args.legacy_client


def dispatch_legacy_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    legacy_flags = [
        args.legacy_version,
        args.legacy_status,
        args.legacy_statusshort,
        args.legacy_statusposition,
        args.legacy_song,
        args.legacy_songshort,
        args.legacy_artist,
        args.legacy_artistshort,
        args.legacy_album,
        args.legacy_position,
        args.legacy_playbackstatus,
        args.legacy_lyrics,
        args.legacy_arturl,
        args.legacy_clear,
        args.legacy_play,
        args.legacy_pause,
        args.legacy_playpause,
        args.legacy_next,
        args.legacy_prev,
        args.legacy_songuri is not None,
        args.legacy_listuri is not None,
    ]
    if not any(legacy_flags):
        parser.print_help()
        return 0

    if args.legacy_version:
        print(VERSION)
        return 0

    if args.legacy_clear:
        clear_screen()
        return 0

    client = SpotifyClient(args)
    payload = client.player()

    if args.legacy_status:
        print_legacy_status(payload)
        return 0
    if args.legacy_statusshort:
        print_legacy_status_short(payload)
        return 0
    if args.legacy_statusposition:
        print_legacy_status_position(payload)
        return 0
    if args.legacy_song:
        print_legacy_song(payload)
        return 0
    if args.legacy_songshort:
        print_legacy_song(payload, short=True)
        return 0
    if args.legacy_artist:
        print_legacy_artist(payload)
        return 0
    if args.legacy_artistshort:
        print_legacy_artist(payload, short=True)
        return 0
    if args.legacy_album:
        print_legacy_album(payload)
        return 0
    if args.legacy_position:
        print_legacy_position(payload)
        return 0
    if args.legacy_playbackstatus:
        print_legacy_playback_status(payload)
        return 0
    if args.legacy_lyrics:
        print_legacy_lyrics(payload)
        return 0
    if args.legacy_arturl:
        print_legacy_art_url(payload)
        return 0

    device_id = resolve_device_id(client, legacy_device_arg(args))
    if args.legacy_play:
        client.play(device_id=device_id)
        print("Playback started.")
        return 0
    if args.legacy_pause:
        client.pause(device_id=device_id)
        print("Playback paused.")
        return 0
    if args.legacy_playpause:
        if not payload:
            raise SpotifyCliError("No active playback context found for toggle.")
        if payload.get("is_playing"):
            client.pause(device_id=device_id)
            print("Playback paused.")
        else:
            client.play(device_id=device_id)
            print("Playback started.")
        return 0
    if args.legacy_next:
        client.next_track(device_id=device_id)
        print("Skipped to next track.")
        return 0
    if args.legacy_prev:
        client.previous_track(device_id=device_id)
        print("Returned to previous track.")
        return 0
    if args.legacy_songuri:
        client.play(device_id=device_id, body=build_play_payload(f"spotify:track:{args.legacy_songuri}"))
        print("Playback started.")
        return 0
    if args.legacy_listuri:
        client.play(device_id=device_id, body=build_play_payload(f"spotify:playlist:{args.legacy_listuri}"))
        print("Playback started.")
        return 0

    raise SpotifyCliError("No legacy command was selected.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Control Spotify playback from WSL or any other environment via the Spotify Web API."
    )
    parser.add_argument("--client-id", help="Spotify application client ID.")
    parser.add_argument("--config-path", default=str(CONFIG_PATH), help="Path to the CLI config file.")
    parser.add_argument("--token-path", default=str(TOKEN_PATH), help="Path to the stored OAuth token.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Loopback callback port for OAuth.")
    parser.add_argument("--version", dest="legacy_version", action="store_true", help="Legacy: show version.")
    parser.add_argument("--status", dest="legacy_status", action="store_true", help="Legacy: show song and artist.")
    parser.add_argument("--statusshort", dest="legacy_statusshort", action="store_true", help="Legacy: show a shortened status.")
    parser.add_argument("--statusposition", dest="legacy_statusposition", action="store_true", help="Legacy: show song, artist, and position.")
    parser.add_argument("--song", dest="legacy_song", action="store_true", help="Legacy: show the song name.")
    parser.add_argument("--songshort", dest="legacy_songshort", action="store_true", help="Legacy: show a shortened song name.")
    parser.add_argument("--artist", dest="legacy_artist", action="store_true", help="Legacy: show the artist name.")
    parser.add_argument("--artistshort", dest="legacy_artistshort", action="store_true", help="Legacy: show a shortened artist name.")
    parser.add_argument("--album", dest="legacy_album", action="store_true", help="Legacy: show the album name.")
    parser.add_argument("--position", dest="legacy_position", action="store_true", help="Legacy: show playback position.")
    parser.add_argument("--arturl", dest="legacy_arturl", action="store_true", help="Legacy: show album art URL.")
    parser.add_argument("--playbackstatus", dest="legacy_playbackstatus", action="store_true", help="Legacy: show playback status.")
    parser.add_argument("--play", dest="legacy_play", action="store_true", help="Legacy: start playback.")
    parser.add_argument("--pause", dest="legacy_pause", action="store_true", help="Legacy: pause playback.")
    parser.add_argument("--playpause", dest="legacy_playpause", action="store_true", help="Legacy: toggle playback.")
    parser.add_argument("--lyrics", dest="legacy_lyrics", action="store_true", help="Legacy: show lyrics for the current track.")
    parser.add_argument("--next", dest="legacy_next", action="store_true", help="Legacy: skip to the next track.")
    parser.add_argument("--prev", dest="legacy_prev", action="store_true", help="Legacy: return to the previous track.")
    parser.add_argument("--clear", dest="legacy_clear", action="store_true", help="Legacy: clear the terminal.")
    parser.add_argument("--songuri", dest="legacy_songuri", help="Legacy: play the track with the given Spotify track ID.")
    parser.add_argument("--listuri", dest="legacy_listuri", help="Legacy: play the playlist with the given Spotify playlist ID.")
    parser.add_argument(
        "--client",
        dest="legacy_client",
        help="Legacy: optional Spotify device name or ID to target for playback actions.",
    )

    subparsers = parser.add_subparsers(dest="command")

    auth_parser = subparsers.add_parser("auth", help="Authenticate with Spotify.")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_command", required=True)

    login_parser = auth_subparsers.add_parser("login", help="Run the OAuth PKCE login flow.")
    login_parser.add_argument(
        "--save-client-id",
        action="store_true",
        help="Store the supplied client ID in the config file for future runs.",
    )
    login_parser.set_defaults(func=command_auth_login)

    logout_parser = auth_subparsers.add_parser("logout", help="Delete the stored token.")
    logout_parser.set_defaults(func=command_auth_logout)

    status_parser = subparsers.add_parser("status", help="Show the current playback status.")
    status_parser.set_defaults(func=command_status)

    devices_parser = subparsers.add_parser("devices", help="List available Spotify Connect devices.")
    devices_parser.set_defaults(func=command_devices)

    transfer_parser = subparsers.add_parser("transfer", help="Transfer playback to a specific device.")
    transfer_parser.add_argument("target", help="Device name or device ID.")
    transfer_parser.add_argument("--play", action="store_true", help="Start playback after transfer.")
    transfer_parser.set_defaults(func=command_transfer)

    play_parser = subparsers.add_parser("play", help="Resume playback or start a specific URI.")
    play_parser.add_argument("uri", nargs="?", help="Optional Spotify URI or open.spotify.com URL.")
    play_parser.add_argument("--device", help="Target device name or device ID.")
    play_parser.set_defaults(func=command_play)

    pause_parser = subparsers.add_parser("pause", help="Pause playback.")
    pause_parser.add_argument("--device", help="Target device name or device ID.")
    pause_parser.set_defaults(func=command_pause)

    toggle_parser = subparsers.add_parser("toggle", help="Toggle the active playback state.")
    toggle_parser.set_defaults(func=command_toggle)

    next_parser = subparsers.add_parser("next", help="Skip to the next track.")
    next_parser.add_argument("--device", help="Target device name or device ID.")
    next_parser.set_defaults(func=command_next)

    prev_parser = subparsers.add_parser("prev", help="Return to the previous track.")
    prev_parser.add_argument("--device", help="Target device name or device ID.")
    prev_parser.set_defaults(func=command_prev)

    search_parser = subparsers.add_parser("search", help="Search Spotify tracks and print playable URIs.")
    search_parser.add_argument("query", help="Track search query.")
    search_parser.add_argument("--limit", type=positive_limit, default=5, help="Number of results to return (1-10).")
    search_mode = search_parser.add_mutually_exclusive_group()
    search_mode.add_argument("--play", dest="play_index", type=positive_index, help="Immediately play result N from the returned list.")
    search_mode.add_argument("--interactive", action="store_true", help="Prompt to choose one of the returned results to play.")
    search_mode.add_argument(
        "--playlist",
        nargs="?",
        const="",
        help="Create a private playlist from the returned results and start playing it. Optionally supply a playlist name.",
    )
    search_parser.add_argument("--device", help="Target device name or device ID for playback.")
    search_parser.set_defaults(func=command_search)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, "command", None):
            return args.func(args)
        return dispatch_legacy_command(args, parser)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except SpotifyCliError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
