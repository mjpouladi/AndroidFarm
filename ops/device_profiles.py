"""Validated, transparent Redroid QA device profiles.

Profiles describe display, locale, timezone and resource-ceiling settings
only.  They intentionally do not contain hardware identifiers and never
impersonate a commercial handset.

Schema 1 carries display and locale.  Schema 2 adds two optional fields:

* ``timezone``: an IANA zone applied through ADB after boot (``persist``
  property, verified with ``getprop``);
* ``resources``: ``{"cpus": 1-4, "memory_gib": 2-4}`` ceilings for the Android
  container.  They may only lower the audited per-device budget that the
  capacity model in ``ops/resources.py`` reserves, so admission stays honest.
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
import zoneinfo


SCHEMA_VERSION = 2
SCHEMA_VERSIONS = frozenset({1, 2})
SUPPORTED_ANDROID_VERSIONS = frozenset({11, 12, 13})
REDROID_IMAGES = {
    11: "redroid/redroid:11.0.0-latest",
    12: "redroid/redroid:12.0.0-latest",
    13: "redroid/redroid:13.0.0-latest",
}
PROFILE_DIGEST_LABEL = "farm.qa-profile.digest"
# Audited Android container budget from generate_farm.py; the capacity model
# reserves exactly this much per device, so a profile may only lower it.
CPU_BUDGET = 4.0
CPU_MINIMUM = 1.0
MEMORY_BUDGET_MIB = 4096
MEMORY_MINIMUM_MIB = 2048

_PROFILE_KEYS = frozenset({
    "schema_version", "android_version", "resolution", "dpi", "fps",
    "device_model", "locale",
})
_OPTIONAL_KEYS = frozenset({"timezone", "resources"})
_RESOURCE_KEYS = frozenset({"cpus", "memory_gib"})
_RESOLUTION_KEYS = frozenset({"width", "height"})
_TIMEZONE_RE = re.compile(r"[A-Z][A-Za-z]{1,31}(?:/[A-Za-z0-9_+-]{1,32}){0,2}\Z")
_MEMORY_UNITS = {"": 1, "b": 1, "k": 1024, "kb": 1024, "m": 1024 ** 2, "mb": 1024 ** 2,
                 "g": 1024 ** 3, "gb": 1024 ** 3}
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
    timezone: str | None = None
    cpus: float | None = None
    memory_mib: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "android_version": self.android_version,
            "resolution": self.resolution.to_dict(),
            "dpi": self.dpi,
            "fps": self.fps,
            "device_model": self.device_model,
            "locale": self.locale,
        }
        # Optional keys are emitted only when set, so a schema 1 profile keeps
        # the digest that existing baselines and Compose labels already carry.
        if self.timezone is not None:
            value["timezone"] = self.timezone
        if self.cpus is not None and self.memory_mib is not None:
            value["resources"] = {"cpus": self.cpus, "memory_gib": self.memory_mib / 1024}
        return value

    @property
    def memory_limit(self) -> str | None:
        """Compose ``mem_limit`` for the Android service, or None for the budget default."""
        return None if self.memory_mib is None else f"{self.memory_mib}m"

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


def _timezone(value: Any) -> str:
    if not isinstance(value, str) or not _TIMEZONE_RE.fullmatch(value):
        raise ValueError("timezone must be an IANA zone name such as Asia/Tehran or UTC")
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"timezone is not present in the host tz database: {value}") from None
    return value


def _quarter(value: Any, field: str, minimum: float, maximum: float) -> float:
    """Accept a number on a 0.25 grid so Compose renders it without rounding drift."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"{field} must be a number")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum:g} and {maximum:g}")
    if (value * 4) != int(value * 4):
        raise ValueError(f"{field} must be a multiple of 0.25")
    return float(value)


def _resources(value: Any) -> tuple[float, int]:
    if not isinstance(value, Mapping) or set(value) != _RESOURCE_KEYS:
        raise ValueError("resources must contain only cpus and memory_gib")
    cpus = _quarter(value["cpus"], "resources.cpus", CPU_MINIMUM, CPU_BUDGET)
    memory = _quarter(value["memory_gib"], "resources.memory_gib",
                      MEMORY_MINIMUM_MIB / 1024, MEMORY_BUDGET_MIB / 1024)
    return cpus, int(memory * 1024)


def parse_memory_bytes(value: Any) -> int:
    """Parse a Compose memory limit (``4g``, ``4096m``, ``4294967296``) into bytes."""
    if isinstance(value, bool):
        raise ValueError("memory limit must be a number or a size string")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if not isinstance(value, str):
        raise ValueError("memory limit must be a number or a size string")
    match = re.fullmatch(r"\s*([0-9]+)\s*([A-Za-z]*)\s*", value)
    if not match or match.group(2).lower() not in _MEMORY_UNITS:
        raise ValueError(f"unsupported memory limit: {value!r}")
    return int(match.group(1)) * _MEMORY_UNITS[match.group(2).lower()]


def parse_cpus(value: Any) -> float:
    """Parse a Compose ``cpus`` value rendered as a number or numeric string."""
    if isinstance(value, bool) or value is None:
        raise ValueError("cpus must be a number")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("cpus must be a number") from None
    if result != result or result <= 0:
        raise ValueError("cpus must be a positive number")
    return result


def validate(value: Mapping[str, Any]) -> DeviceProfile:
    """Validate and normalise an in-memory JSON profile."""
    if not isinstance(value, Mapping):
        raise ValueError("device profile must be a JSON object")
    keys = set(value)
    missing = sorted(_PROFILE_KEYS - keys)
    if missing:
        raise ValueError("device profile is missing: " + ", ".join(missing))
    schema = _integer(value["schema_version"], "schema_version", min(SCHEMA_VERSIONS), max(SCHEMA_VERSIONS))
    allowed = _PROFILE_KEYS | (_OPTIONAL_KEYS if schema >= 2 else frozenset())
    extra = sorted(keys - allowed)
    if extra:
        raise ValueError("unsupported device profile fields: " + ", ".join(extra))
    android = _integer(value["android_version"], "android_version", 1, 99)
    if android not in SUPPORTED_ANDROID_VERSIONS:
        raise ValueError("android_version must be one of: " + ", ".join(map(str, sorted(SUPPORTED_ANDROID_VERSIONS))))
    resolution = value["resolution"]
    if not isinstance(resolution, Mapping) or set(resolution) != _RESOLUTION_KEYS:
        raise ValueError("resolution must contain only width and height")
    width = _integer(resolution["width"], "resolution.width", 320, 4320)
    height = _integer(resolution["height"], "resolution.height", 320, 4320)
    timezone = _timezone(value["timezone"]) if "timezone" in value else None
    cpus, memory_mib = _resources(value["resources"]) if "resources" in value else (None, None)
    return DeviceProfile(
        schema_version=schema,
        android_version=android,
        resolution=Resolution(width, height),
        dpi=_integer(value["dpi"], "dpi", 120, 640),
        fps=_integer(value["fps"], "fps", 10, 120),
        device_model=_model(value["device_model"]),
        locale=_normalise_locale(value["locale"]),
        timezone=timezone,
        cpus=cpus,
        memory_mib=memory_mib,
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
    if selected.cpus is not None and selected.memory_limit is not None:
        # Ceilings only; the proxy/screen budgets and the capacity model stay unchanged.
        android["cpus"] = selected.cpus
        android["mem_limit"] = selected.memory_limit
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
