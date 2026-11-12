import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from filmfactory.core import FilmError, StateStore, budget, parse_prices, validate_story
from filmfactory.media import stamp, subtitles, synthesize, verify_native
from filmfactory.providers import Runway, new_story, produce_assets

ROOT = Path(__file__).resolve().parents[1]
STORY = json.loads((ROOT / "examples/sample-story.json").read_text(encoding="utf-8"))
RATES = """Credits can be purchased for $0.01 per credit.
| `seedance2_mini` (480p) | 7 credits per second (28 credit minimum per generation) |
| `gen4_image` | 5 credits per 720p image, or 8 credits per 1080p image |
| `gen4_image_turbo` | 2 credits per image, any resolution |
"""
WRITER = "| Input | $0.75 | 1M tokens |\n| Output | $4.5 | 1M tokens |"
FX = "幣別,匯率,現金,即期,a,b,c,d,e,f,g,匯率,現金,即期\nUSD,本行買入,32,32.7,0,0,0,0,0,0,0,本行賣出,33.1,32.88"


class BudgetTests(unittest.TestCase):
    def test_actual_480p_budget_and_all_costs(self):
        result = budget(parse_prices(RATES, WRITER, FX))
        self.assertEqual(result["reserved_runway_credits"], 114)
        self.assertLessEqual(result["estimated_upper_twd"], 50)
        self.assertGreater(result["estimated_upper_twd"], 45)

    def test_price_increase_blocks(self):
        with self.assertRaises(FilmError):
            budget(parse_prices(RATES.replace("7 credits", "15 credits"), WRITER, FX))

    def test_unreadable_price_fails_closed(self):
        with self.assertRaises(FilmError):
            parse_prices("unavailable", WRITER, FX)

    def test_fx_move_is_included(self):
        with self.assertRaises(FilmError):
            budget(parse_prices(RATES, WRITER, FX.replace("32.88", "45.00")))

    def test_nonfinite_or_negative_budget_rejected(self):
        prices = parse_prices(RATES, WRITER, FX)
        for limit in (float("nan"), float("inf"), -1, 51):
            with self.assertRaises(FilmError):
                budget(prices, limit)
        with self.assertRaises(FilmError):
            budget(prices, overhead=-3)


class StoryTests(unittest.TestCase):
    def test_valid_sample(self):
        validate_story(STORY)

    def test_overlong_line_and_unknown_character(self):
        for field, value in (("text", "長" * 25), ("speaker", "不存在"), ("scene", True)):
            story = copy.deepcopy(STORY)
            story["shots"][0][field] = value
            with self.assertRaises(FilmError):
                validate_story(story)

    def test_duplicate_ending_rejected(self):
        with self.assertRaises(FilmError):
            validate_story(STORY, [{"ending": STORY["ending"]}])

    def test_state_survives_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            store.record_story("2026-11-09", STORY)
            restored = StateStore(path)
            with patch("filmfactory.providers.fetch") as request:
                self.assertEqual(new_story(restored, "2026-11-09"), STORY)
                request.assert_not_called()


class BillingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(Path(self.directory.name) / "state.json")
        self.day = "2026-11-09"
        self.store.episode(self.day)["budget"] = budget(parse_prices(RATES, WRITER, FX))
        self.target = Path(self.directory.name) / "media.mp4"
        self.body = {"model": "seedance2_mini", "ratio": "496:864", "duration": 5, "audio": True}

    @patch.dict("os.environ", {"RUNWAYML_API_SECRET": "test-not-a-real-key"})
    def test_uncertain_submission_never_retried(self):
        with patch("filmfactory.providers.fetch", side_effect=FilmError("timeout")) as request:
            for _ in range(2):
                with self.assertRaises(FilmError):
                    Runway(self.store, self.day).generate("shot", "/text_to_video", self.body, 35, self.target)
            self.assertEqual(request.call_count, 1)
        restored = StateStore(self.store.path)
        self.assertEqual(restored.episode(self.day)["tasks"]["shot"]["status"], "submitting")

    @patch.dict("os.environ", {"RUNWAYML_API_SECRET": "test-not-a-real-key"})
    def test_resume_saved_task_only_gets_not_posts(self):
        digest = hashlib.sha256(json.dumps(self.body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        self.store.episode(self.day)["tasks"]["shot"] = {"request_hash": digest,
            "reserved_credits": 35, "task_id": "existing", "status": "submitted"}
        def download(_url, path):
            path.write_bytes(b"local-test-media")
        with patch("filmfactory.providers.fetch", return_value={"status": "SUCCEEDED", "output": ["https://example.invalid/media"]}) as request, patch("filmfactory.providers.download", side_effect=download):
            Runway(self.store, self.day).generate("shot", "/text_to_video", self.body, 35, self.target)
            self.assertEqual(request.call_count, 1)
            self.assertNotIn("method", request.call_args.kwargs)
            self.assertTrue(request.call_args.args[0].endswith("/tasks/existing"))

    def test_budget_reservation_blocks_before_post(self):
        with patch("filmfactory.providers.fetch") as request:
            with self.assertRaises(FilmError):
                Runway(self.store, self.day).generate("shot", "/text_to_video", self.body, 200, self.target)
            request.assert_not_called()


class MediaTests(unittest.TestCase):
    def test_provider_requests_match_budget_and_preserve_native_audio(self):
        from unittest.mock import Mock
        runway = Mock()
        runway.generate.side_effect = lambda name, endpoint, body, cost, target: target
        with tempfile.TemporaryDirectory() as directory, patch("filmfactory.media.reference_png"), patch("filmfactory.providers.image_uri", return_value="data:image/png;base64,test"):
            assets = produce_assets(STORY, directory, runway, parse_prices(RATES, WRITER, FX))
        calls = runway.generate.call_args_list
        videos = [call for call in calls if call.args[1] == "/text_to_video"]
        self.assertEqual(len(videos), 3)
        self.assertEqual(sum(call.args[3] for call in calls), 114)
        for call in videos:
            body = call.args[2]
            self.assertEqual(body["ratio"], "496:864")
            self.assertEqual(body["duration"], 5)
            self.assertTrue(body["audio"])
            self.assertNotIn("resolution", body)
            self.assertIn("exactly:", body["promptText"])
        self.assertEqual(sum(asset["kind"] == "still" for asset in assets), 9)

    def test_minute_boundary_and_subtitle_wrapping(self):
        self.assertEqual(stamp(60, True), "0:01:00.00")
        self.assertEqual(stamp(60), "00:01:00,000")
        with tempfile.TemporaryDirectory() as directory:
            subtitles(STORY, Path(directory), demo=True)
            ass = (Path(directory) / "subtitles.ass").read_text()
            self.assertIn("技術測試畫面", ass)
            self.assertIn(r"\N", ass)

    def test_native_video_does_not_call_tts(self):
        with tempfile.TemporaryDirectory() as directory, patch("edge_tts.Communicate") as speech:
            asyncio.run(synthesize(STORY, [{"kind": "native_video"}] * 12, Path(directory)))
            speech.assert_not_called()

    def test_silent_native_video_rejected(self):
        with patch("filmfactory.media.probe", return_value={"format": {"duration": "5"},
                   "streams": [{"codec_type": "video"}]}):
            with self.assertRaises(FilmError):
                verify_native("missing-audio.mp4")


if __name__ == "__main__":
    unittest.main()
