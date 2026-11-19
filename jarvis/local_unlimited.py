"""Opt-in, local-only Ollama brain launcher.

No billing quota is imposed by this launcher, but context windows, disk, RAM,
model output size, and independently configured cloud features remain bounded.
The opt-in configuration is per process and never modifies jarvis.toml.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

OLLAMA_ROOT = "http://127.0.0.1:11434"


class LocalModelUnavailable(RuntimeError):
    """No usable local model is ready. Never silently switch to paid cloud."""


def local_downloads(*, endpoint: str = OLLAMA_ROOT, timeout: float = 2.0) -> tuple[str, ...]:
    """Fetch installed model names from a loopback Ollama server (no external requests)."""
    # The endpoint is fixed for normal usage; tests may pass a loopback mock.
    from urllib.parse import urlsplit

    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Ollama muss für diesen Modus lokal erreichbar sein.")
    try:
        with urlopen(f"{endpoint.rstrip('/')}/api/tags", timeout=timeout) as response:  # noqa: S310; localhost checked
            if response.status != 200:
                raise LocalModelUnavailable("Ollama hat keine Modellliste geliefert.")
            payload = response.read(2 * 1024 * 1024)
    except (OSError, HTTPError, URLError, TimeoutError) as exc:
        raise LocalModelUnavailable(
            "Lokaler Ollama-Server nicht erreichbar. Installiere/starte Ollama: "
            "https://ollama.com/download"
        ) from exc
    try:
        models = json.loads(payload)["models"]
        if not isinstance(models, list):
            raise ValueError("models is not a list")
        names = [row["name"] for row in models if isinstance(row, dict) and isinstance(row.get("name"), str)]
    except (ValueError, KeyError, TypeError) as exc:
        raise LocalModelUnavailable("Ollama lieferte eine ungültige Modellliste.") from exc
    # Ollama :cloud models run remotely and may charge credits. Never count them.
    return tuple(sorted({name for name in names if name and not name.lower().endswith(":cloud")}))


def select_local_model(*, model: str | None, installed: tuple[str, ...]) -> str | None:
    """Validate a selected model against actual local weights; None = auto choice."""
    if not installed:
        raise LocalModelUnavailable(
            "Keine lokalen Modelle installiert. Zum Beispiel: ollama pull qwen3:4b"
        )
    if model is None:
        return None
    name = model.strip()
    if not name or name.lower().endswith(":cloud") or name not in installed:
        raise LocalModelUnavailable(
            "Das ausgewählte Modell liegt nicht lokal vor. Verfügbar: " + ", ".join(installed)
        )
    return name


def build_local_environment(
    source: Mapping[str, str], *, model: str | None = None, local_voice: bool = False
) -> dict[str, str]:
    """Override configured brain tiers, including inherited DODAIOS_ values.

    This is a best-effort brain-only mode; independent external tools/mission
    subprocesses may have their own credentials and limits.
    """
    env = dict(source)
    local_tier = json.dumps({"provider": "ollama", "model": model or "", "fallback_provider": "ollama", "fallback_model": model or ""})
    setting = {
        "BRAIN__PRIMARY": "ollama",
        "BRAIN__DEEP_BRAIN": "ollama",
        "BRAIN__ROUTING_PROVIDER": "ollama",
        "BRAIN__ROUTING_MODEL": model or "",
        "BRAIN__LOCAL_FALLBACK": "ollama",
        "BRAIN__LOCAL_FALLBACK_MODEL": model or "",
        "BRAIN__ROUTER": local_tier,
        "BRAIN__WORKER": local_tier,
        "BRAIN__TOOL_MODEL": local_tier,
        "BRAIN__PROVIDERS__OLLAMA__BASE_URL": OLLAMA_ROOT,
        "BRAIN__PROVIDERS__OLLAMA__MODEL": model or "",
    }
    if local_voice:
        setting.update({"STT__PROVIDER": "faster-whisper", "STT__FALLBACK": "", "TTS__PROVIDER": "piper-local"})
    for key, val in setting.items():
        # A DODAIOS__ override wins over the JARVIS__ override on import.
        # Matching them prevents an inherited cloud-setting shadow.
        env[f"JARVIS__{key}"] = val
        env[f"DODAIOS__{key}"] = val
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JARVIS ohne Cloud-Brain-Tokens starten (lokales Ollama).")
    parser.add_argument("--model", help="Exakter Name eines installierten lokalen Modells")
    parser.add_argument("--check", action="store_true", help="Nur Verbindung/Modell überprüfen")
    parser.add_argument("--local-voice", action="store_true", help="Auch STT/TTS lokal (erfordert installierte Sprachmodelle)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args(argv)
    try:
        names = local_downloads()
        chosen = select_local_model(model=args.model, installed=names)
    except LocalModelUnavailable as exc:
        print(f"[JARVIS Lokal] {exc}", file=sys.stderr)
        return 2
    print("[JARVIS Lokal] Lokale Modelle: " + ", ".join(names), flush=True)
    if args.check:
        print("[JARVIS Lokal] Bereit. Kein Cloud-Brain für diesen Start konfiguriert.")
        return 0
    print("[JARVIS Lokal] Brain: Ollama; Modell: " + (chosen or "automatisch"), flush=True)
    print("[JARVIS Lokal] Einzelne Antworten sind weiterhin durch Kontextfenster/Hardware begrenzt.", flush=True)
    if not args.local_voice:
        print("[JARVIS Lokal] Achtung: STT/TTS und externe Agenten können weiter Cloud-Dienste verwenden.", flush=True)
    else:
        print("[JARVIS Lokal] Sprache: lokale Modelle erforderlich; kein automatischer Download.", flush=True)
    command = [sys.executable, "-m", "jarvis.ui.web.launcher"]
    if args.headless:
        command.append("--headless")
    env = build_local_environment(os.environ, model=chosen, local_voice=args.local_voice)
    if args.debug:
        env["JARVIS_DEBUG"] = "1"
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
