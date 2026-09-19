"""Validated, transparent Redroid QA device profiles.

Profiles describe display and locale settings only.  They intentionally do not
contain hardware identifiers and never impersonate a commercial handset.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Mapping


SCHEMA_VERSION = 1
SUPPORTED_ANDROID_VERSIONS = frozenset({11, 12})
REDROID_IMAGES = {
    11: "redroid/redroid:11.0.0-latest",
    12: "redroid/redroid:12.0.0-latest",
}
PROFILE_DIGEST_LABEL = "farm.qa-profile.digest"

_PROFILE_KEYS = frozenset({
    "schema_version", "android_version", "resolution", "dpi", "fps",
    "device_model", "locale",
})
_RESOLUTION_KEYS = frozenset({"width", "height"})
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()/-]{2,63}\Z")
_LOCALE_RE = re.compile(
    r"(?P<language>[A-Za-z]{2,3})"
    r"(?:-(?P<script>[A-Za-z]{4}))?"
    r"(?:-(?P<region>[A-Za-z]{2}|[0-9]{3}))?\Z"
)
_TRANSPARENT_MODEL_MARKERS = frozenset({"qa", "test", "redroid", "virtual", "emulator", "lab"})
_COMMERCIAL_MODEL_MARKERS = frozenset({
    "apple", "asus", "galaxy", "google", "honor", "huawei", "iphone", "lenovo",
    "lg", "motorola", "nokia", "nothing", "oneplus", "oppo", "pixel", "realme",
    "redmi", "samsung", "sony", "vivo", "xiaomi",
})


@dataclass(frozen=True)
class Resolution:
    width: int
    height: int

    def to_dict(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height}


@dataclass(frozen=True)
class DeviceProfile:
    schema_version: int
    android_version: int
    resolution: Resolution
    dpi: int
    fps: int
    device_model: str
    locale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "android_version": self.android_version,
            "resolution": self.resolution.to_dict(),
            "dpi": self.dpi,
            "fps": self.fps,
            "device_model": self.device_model,
            "locale": self.locale,
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _normalise_locale(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("locale must be a BCP47-like string")
    match = _LOCALE_RE.fullmatch(value)
    if not match:
        raise ValueError("locale must resemble en-US, fa-IR, or zh-Hans-CN")
    parts = [match.group("language").lower()]
    if match.group("script"):
        parts.append(match.group("script").title())
    if match.group("region"):
        region = match.group("region")
        parts.append(region if region.isdigit() else region.upper())
    return "-".join(parts)


def _model(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or not _MODEL_RE.fullmatch(value):
        raise ValueError("device_model must be 3-64 safe ASCII characters without surrounding whitespace")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("device_model may not contain control or formatting characters")
    words = set(re.findall(r"[a-z0-9]+", value.casefold()))
    commercial = sorted(words & _COMMERCIAL_MODEL_MARKERS)
    if commercial:
        raise ValueError("device_model may not impersonate a commercial handset: " + ", ".join(commercial))
    if not words & _TRANSPARENT_MODEL_MARKERS:
        raise ValueError("device_model must clearly identify a QA, test, Redroid, virtual, emulator, or lab device")
    return value


def validate(value: Mapping[str, Any]) -> DeviceProfile:
    """Validate and normalise an in-memory JSON profile."""
    if not isinstance(value, Mapping):
        raise ValueError("device profile must be a JSON object")
    keys = set(value)
    missing = sorted(_PROFILE_KEYS - keys)
    extra = sorted(keys - _PROFILE_KEYS)
    if missing:
        raise ValueError("device profile is missing: " + ", ".join(missing))
    if extra:
        raise ValueError("unsupported device profile fields: " + ", ".join(extra))
    schema = _integer(value["schema_version"], "schema_version", SCHEMA_VERSION, SCHEMA_VERSION)
    android = _integer(value["android_version"], "android_version", 1, 99)
    if android not in SUPPORTED_ANDROID_VERSIONS:
        raise ValueError("android_version must be one of: " + ", ".join(map(str, sorted(SUPPORTED_ANDROID_VERSIONS))))
    resolution = value["resolution"]
    if not isinstance(resolution, Mapping) or set(resolution) != _RESOLUTION_KEYS:
        raise ValueError("resolution must contain only width and height")
    width = _integer(resolution["width"], "resolution.width", 320, 4320)
    height = _integer(resolution["height"], "resolution.height", 320, 4320)
    return DeviceProfile(
        schema_version=schema,
        android_version=android,
        resolution=Resolution(width, height),
        dpi=_integer(value["dpi"], "dpi", 120, 640),
        fps=_integer(value["fps"], "fps", 10, 120),
        device_model=_model(value["device_model"]),
        locale=_normalise_locale(value["locale"]),
    )


def load(path: Path | str) -> DeviceProfile:
    """Load a small UTF-8 JSON profile and return its validated representation."""
    source = Path(path)
    if source.stat().st_size > 16 * 1024:
        raise ValueError("device profile is unexpectedly large")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("device profile must be valid UTF-8 JSON") from exc
    return validate(value)


def apply_profile(
    compose_doc: Mapping[str, Any], device: str, profile: DeviceProfile | Mapping[str, Any]
) -> dict[str, Any]:
    """Return a Compose copy with the validated QA profile applied to one Android service."""
    selected = profile if isinstance(profile, DeviceProfile) else validate(profile)
    if not isinstance(compose_doc, Mapping):
        raise ValueError("Compose document must be an object")
    if not isinstance(device, str) or not re.fullmatch(r"num[0-9]{2,6}", device):
        raise ValueError("device must be a canonical numXX identifier")
    document = copy.deepcopy(dict(compose_doc))
    services = document.get("services")
    service_name = f"android-{device}"
    if not isinstance(services, dict) or not isinstance(services.get(service_name), dict):
        raise ValueError(f"Compose service is missing: {service_name}")
    android = services[service_name]
    command = android.get("command", [])
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError(f"{service_name}.command must be a list of strings")

    owned_keys = {
        "androidboot.redroid_width", "androidboot.redroid_height", "androidboot.redroid_dpi",
        "androidboot.redroid_fps", "ro.product.brand", "ro.product.manufacturer",
        "ro.product.model", "ro.product.locale",
    }
    preserved = [item for item in command if item.partition("=")[0] not in owned_keys]
    android["command"] = [
        *preserved,
        f"androidboot.redroid_width={selected.resolution.width}",
        f"androidboot.redroid_height={selected.resolution.height}",
        f"androidboot.redroid_dpi={selected.dpi}",
        f"androidboot.redroid_fps={selected.fps}",
        "ro.product.brand=redroid",
        "ro.product.manufacturer=remote-android",
        f"ro.product.model={selected.device_model}",
        f"ro.product.locale={selected.locale}",
    ]
    android["image"] = REDROID_IMAGES[selected.android_version]
    labels = android.setdefault("labels", {})
    if not isinstance(labels, dict):
        raise ValueError(f"{service_name}.labels must be an object")
    labels[PROFILE_DIGEST_LABEL] = selected.digest
    screen = services.get(f"screen-{device}")
    if isinstance(screen, dict):
        environment = screen.setdefault("environment", {})
        if not isinstance(environment, dict):
            raise ValueError(f"screen-{device}.environment must be an object")
        environment.update({
            "SCREEN_WIDTH": str(selected.resolution.width),
            "SCREEN_HEIGHT": str(selected.resolution.height),
            "SCREEN_FPS": str(selected.fps),
        })
    return document
