import unittest

import main


class SpotifyCliTests(unittest.TestCase):
    def test_build_code_challenge_is_url_safe(self) -> None:
        verifier = "example-verifier"
        challenge = main.build_code_challenge(verifier)
        self.assertEqual(challenge, "YZHC5CELD1x821VESsimH0XRmYO5iqMyp3tMc2BrB7I")

    def test_normalize_spotify_uri_from_url(self) -> None:
        uri = main.normalize_spotify_uri("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")
        self.assertEqual(uri, "spotify:track:4uLU6hMCjMI75M1A2tKUQC")

    def test_build_play_payload_for_context_uri(self) -> None:
        payload = main.build_play_payload("spotify:album:1ATL5GLyefJaxhQzSPVrLX")
        self.assertEqual(payload, {"context_uri": "spotify:album:1ATL5GLyefJaxhQzSPVrLX"})

    def test_format_ms_handles_hours(self) -> None:
        self.assertEqual(main.format_ms(3_726_000), "1:02:06")

    def test_positive_limit_rejects_out_of_range(self) -> None:
        with self.assertRaises(Exception):
            main.positive_limit("0")

    def test_format_search_result_contains_uri_and_id(self) -> None:
        track = {
            "name": "Example Song",
            "uri": "spotify:track:abc123",
            "id": "abc123",
            "artists": [{"name": "Example Artist"}],
            "album": {"name": "Example Album"},
        }
        output = main.format_search_result(1, track)
        self.assertIn("spotify:track:abc123", output)
        self.assertIn("id:    abc123", output)

    def test_choose_track_from_results_by_index(self) -> None:
        tracks = [{"uri": "spotify:track:first"}, {"uri": "spotify:track:second"}]
        chosen = main.choose_track_from_results(tracks, selection=2)
        self.assertEqual(chosen["uri"], "spotify:track:second")

    def test_choose_track_from_results_rejects_out_of_range(self) -> None:
        with self.assertRaises(main.SpotifyCliError):
            main.choose_track_from_results([{"uri": "spotify:track:first"}], selection=2)

    def test_default_playlist_name_uses_query(self) -> None:
        self.assertEqual(
            main.default_playlist_name("James Brown Funky"),
            "spotify-cli search: James Brown Funky",
        )


if __name__ == "__main__":
    unittest.main()
