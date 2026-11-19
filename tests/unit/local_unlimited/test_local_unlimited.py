"""Unit + loopback integration + security/regression for local unlimited launcher."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from jarvis.local_unlimited import (
    LocalModelUnavailable,
    build_local_environment,
    local_downloads,
    select_local_model,
)


class TestFunctional(unittest.TestCase):
    def test_selected_model(self):
        self.assertEqual(select_local_model(model="qwen3:4b", installed=("qwen3:4b",)), "qwen3:4b")

    def test_auto_model(self):
        self.assertIsNone(select_local_model(model=None, installed=("qwen3:4b",)))

    def test_cloud_model_rejected(self):
        with self.assertRaises(LocalModelUnavailable):
            select_local_model(model="gemma4:cloud", installed=("qwen3:4b",))

    def test_missing_model_rejected(self):
        with self.assertRaises(LocalModelUnavailable):
            select_local_model(model="does-not-exist", installed=("qwen3:4b",))


class TestIntegration(unittest.TestCase):
    def test_loopback_tags_and_cloud_filtering(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/api/tags":
                    self.send_error(404)
                    return
                result = json.dumps({"models": [
                    {"name": "qwen3:4b"}, {"name": "gemma4:cloud"}, {"name": "qwen3:4b"}
                ]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                models = local_downloads(endpoint=f"http://127.0.0.1:{server.server_port}")
                self.assertEqual(models, ("qwen3:4b",))
            finally:
                server.shutdown()
                thread.join(timeout=2)


class TestSecurityRegression(unittest.TestCase):
    def test_external_endpoint_never_contacted(self):
        for endpoint in ("https://ollama.com", "http://evil.example", "file:///etc/passwd"):
            with self.assertRaises(ValueError):
                local_downloads(endpoint=endpoint)

    def test_local_env_overrides_cloud_preferences_without_mutation(self):
        original = {"JARVIS__BRAIN__PRIMARY": "openai", "DODAIOS__BRAIN__PRIMARY": "gemini"}
        env = build_local_environment(original)
        self.assertEqual(env["JARVIS__BRAIN__PRIMARY"], "ollama")
        self.assertEqual(env["DODAIOS__BRAIN__PRIMARY"], "ollama")
        self.assertEqual(json.loads(env["JARVIS__BRAIN__TOOL_MODEL"])["provider"], "ollama")
        self.assertEqual(original["JARVIS__BRAIN__PRIMARY"], "openai")
        self.assertNotIn("JARVIS__STT__PROVIDER", env)

    def test_voice_is_explicit_opt_in(self):
        env = build_local_environment({}, local_voice=True)
        self.assertEqual(env["JARVIS__STT__PROVIDER"], "faster-whisper")
        self.assertEqual(env["JARVIS__TTS__PROVIDER"], "piper-local")


if __name__ == "__main__":
    unittest.main()
