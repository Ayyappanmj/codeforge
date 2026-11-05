"""Language registry. Images/commands are fixed server-side; clients only pick a key."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

IMAGE_RE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,100}(:[A-Za-z0-9._-]{1,64})?")


class UnsupportedLanguage(ValueError):
    pass


@dataclass(frozen=True)
class LanguageSpec:
    key: str
    display_name: str
    image: str
    # The entrypoint of each image receives the payload on stdin (see protocol.py).
    # No source code is ever interpolated into a command line.


def _spec(key, display, default_image, env):
    image = env.get(f"IMAGE_{key.upper()}", default_image)
    if not IMAGE_RE.fullmatch(image):
        raise ValueError(f"invalid image name for {key}: {image!r}")
    return LanguageSpec(key, display, image)


def build_registry(env=None) -> dict[str, LanguageSpec]:
    env = os.environ if env is None else env
    specs = [
        _spec("python", "Python 3.12", "codeforge-python-runner:latest", env),
        _spec("javascript", "JavaScript (Node.js 22)", "codeforge-javascript-runner:latest", env),
    ]
    return {s.key: s for s in specs}


def supported_languages(registry=None) -> list[str]:
    return sorted((registry or build_registry()).keys())


def get_language(key: str, registry=None) -> LanguageSpec:
    registry = registry or build_registry()
    try:
        return registry[key]
    except (KeyError, TypeError) as exc:
        raise UnsupportedLanguage(
            f"Unsupported language {str(key)[:32]!r}. Supported: {', '.join(sorted(registry))}"
        ) from exc
