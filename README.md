# spotify-cli

This project controls Spotify through the Spotify Web API instead of Linux `dbus`.
That means it can run inside WSL while targeting the Spotify desktop app running on
Windows, as long as the Windows app appears as a Spotify Connect device.

## Requirements

- Spotify Premium for playback control endpoints
- A Spotify app client ID from the Spotify developer dashboard
- The Windows Spotify desktop app open and signed in

## Setup

1. Create an app in the Spotify developer dashboard.
2. In the app settings, add this redirect URI:

   `http://127.0.0.1:4380/callback`

3. Put your client ID in a local `.env` file or export it in WSL:

   ```bash
   echo 'SPOTIFY_CLIENT_ID=your_client_id_here' >> .env
   ```

   or:

   ```bash
   export SPOTIFY_CLIENT_ID=your_client_id_here
   ```

4. Authenticate:

   ```bash
   uv run spotifycli auth login
   ```

5. With Spotify open on Windows, list devices:

   ```bash
   uv run spotifycli devices
   ```

6. Transfer playback to the Windows client if needed:

   ```bash
   uv run spotifycli transfer "Your Windows Device Name" --play
   ```

## Commands

```bash
uv run spotifycli status
uv run spotifycli devices
uv run spotifycli search "The Less I Know The Better"
uv run spotifycli search "The Less I Know The Better" --play 1
uv run spotifycli search "The Less I Know The Better" --interactive
uv run spotifycli search "James Brown Funky" --playlist
uv run spotifycli search "James Brown Funky" --playlist "James Brown Funky Picks"
uv run spotifycli play
uv run spotifycli play spotify:track:4uLU6hMCjMI75M1A2tKUQC
uv run spotifycli play https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC
uv run spotifycli pause
uv run spotifycli toggle
uv run spotifycli next
uv run spotifycli prev
uv run spotifycli transfer "DESKTOP-ABC123" --play
uv run spotifycli auth logout
```

## Notes For WSL

- This works because playback control goes through Spotify Connect, not Linux desktop
  integration.
- The Windows Spotify app must be open and available as a device.
- If the browser callback does not reach WSL automatically, the CLI falls back to
  asking for the redirected URL so the login can still complete.
- Some Spotify devices are restricted and cannot accept transfer or playback control.

## Search Example

```bash
uv run spotifycli search "The Less I Know The Better"
uv run spotifycli search "The Less I Know The Better" --play 1
uv run spotifycli search "The Less I Know The Better" --interactive
uv run spotifycli search "James Brown Funky" --playlist
uv run spotifycli search "James Brown Funky" --playlist "James Brown Funky Picks"
```

The output includes both the full `spotify:track:...` URI and the raw track ID, so
you can use either:

```bash
uv run spotifycli play spotify:track:2Foc5Q5nqNiosCNqttzHof
uv run spotifycli --songuri 2Foc5Q5nqNiosCNqttzHof
```

`--play N` starts one of the returned results directly. `--interactive` lets you
pick from the numbered list. `--playlist` creates a private Spotify playlist from
the returned search results and starts that playlist.
