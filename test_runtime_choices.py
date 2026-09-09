"""The long-lived launcher must discover models added after its startup."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import server


class RuntimeChoicesTest(unittest.TestCase):
    def test_new_cli_model_appears_without_server_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "models.json")
            def write(slugs):
                with open(path, "w") as handle:
                    json.dump({"models": [dict(slug=s, visibility="list",
                                               supported_in_api=True, priority=i)
                                          for i, s in enumerate(slugs)]}, handle)
            with patch.object(server, "CODEX_MODELS_CACHE", path), patch.dict(os.environ, {}, clear=True):
                write(["gpt-5.6-sol"])
                self.assertEqual(["gpt-5.6-sol"], server.codex_models())
                offered = ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
                write(offered)
                codex = next(r for r in server.runtime_choices() if r["type"] == "codex")
                self.assertEqual(offered, codex["models"])
                with patch.dict(os.environ, {"VOXTERM_CODEX_MODELS": "gpt-5.6-sol"}):
                    codex = next(r for r in server.runtime_choices() if r["type"] == "codex")
                    self.assertEqual(["gpt-5.6-sol"], codex["models"])

    def test_hidden_and_non_api_models_are_not_offered(self):
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps({"models": [
                {"slug": "gpt-6-astra", "visibility": "list", "supported_in_api": True},
                {"slug": "gpt-reserve", "visibility": "hide", "supported_in_api": True},
                {"slug": "gpt-test", "visibility": "list", "supported_in_api": False}]}))):
            self.assertEqual(["gpt-6-astra"], server.codex_models())


if __name__ == "__main__":
    unittest.main()
