import json
from pathlib import Path
import tempfile
import unittest

from generate_farm import generate
from ops.device_profiles import (
    PROFILE_DIGEST_LABEL,
    DeviceProfile,
    apply_profile,
    load,
    validate,
)


VALID = {
    "schema_version": 1,
    "android_version": 12,
    "resolution": {"width": 720, "height": 1280},
    "dpi": 240,
    "fps": 20,
    "device_model": "Android Farm QA Phone HD",
    "locale": "en-US",
}


class DeviceProfileTests(unittest.TestCase):
    def test_example_loads_and_normalises_locale(self):
        profile = load(Path("installer/device-profile.example.json"))
        self.assertIsInstance(profile, DeviceProfile)
        self.assertEqual(profile.android_version, 12)
        self.assertEqual(profile.locale, "en-US")
        self.assertEqual(profile.to_dict(), VALID)
        self.assertRegex(profile.digest, r"^[0-9a-f]{64}$")

        lower = validate(dict(VALID, locale="FA-ir"))
        self.assertEqual(lower.locale, "fa-IR")

    def test_digest_is_canonical_and_load_rejects_invalid_json(self):
        reordered = {key: VALID[key] for key in reversed(VALID)}
        self.assertEqual(validate(VALID).digest, validate(reordered).digest)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "profile.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
                load(path)

    def test_rejects_unknown_identity_fields_and_unsupported_android(self):
        for extra in ("imei", "android_id", "serial", "ro.serialno"):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "unsupported"):
                validate(dict(VALID, **{extra: "forbidden"}))
        for version in (10, 13, True):
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate(dict(VALID, android_version=version))

    def test_rejects_real_brand_impersonation_and_opaque_models(self):
        for model in ("Samsung Galaxy S21", "Google Pixel 6 QA", "Ordinary Phone", "QA Phone\nInjected"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                validate(dict(VALID, device_model=model))

    def test_rejects_invalid_ranges_structure_and_locale(self):
        invalid = [
            dict(VALID, dpi=0),
            dict(VALID, fps=121),
            dict(VALID, resolution={"width": 100, "height": 1280}),
            dict(VALID, resolution={"width": 720, "height": 1280, "depth": 24}),
            dict(VALID, locale="en_US"),
            dict(VALID, locale="en-US-extra-variant"),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)

    def test_apply_profile_is_copy_on_write_and_preserves_honest_identity(self):
        original = generate(1)
        before = json.dumps(original, sort_keys=True)
        original_command = original["services"]["android-num01"]["command"]
        serial = next(item for item in original_command if item.startswith("androidboot.serialno="))

        profile = validate(dict(
            VALID,
            android_version=11,
            resolution={"width": 1080, "height": 1920},
            dpi=420,
            fps=30,
            locale="fa-ir",
        ))
        rendered = apply_profile(original, "num01", profile)
        android = rendered["services"]["android-num01"]
        command = android["command"]

        self.assertEqual(json.dumps(original, sort_keys=True), before)
        self.assertEqual(android["image"], "redroid/redroid:11.0.0-latest")
        self.assertIn("androidboot.redroid_width=1080", command)
        self.assertIn("androidboot.redroid_height=1920", command)
        self.assertIn("androidboot.redroid_dpi=420", command)
        self.assertIn("androidboot.redroid_fps=30", command)
        self.assertIn("ro.product.brand=redroid", command)
        self.assertIn("ro.product.manufacturer=remote-android", command)
        self.assertIn("ro.product.model=Android Farm QA Phone HD", command)
        self.assertIn("ro.product.locale=fa-IR", command)
        self.assertIn(serial, command)
        self.assertFalse(any("imei" in item.lower() or "android_id" in item.lower() for item in command))
        self.assertEqual(android["labels"][PROFILE_DIGEST_LABEL], profile.digest)
        for prefix in ("androidboot.redroid_width=", "ro.product.model=", "ro.product.brand="):
            self.assertEqual(sum(item.startswith(prefix) for item in command), 1)

    def test_apply_profile_validates_compose_shape(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            apply_profile({"services": {}}, "num01", VALID)
        malformed = generate(1)
        malformed["services"]["android-num01"]["command"] = "not-a-list"
        with self.assertRaisesRegex(ValueError, "list of strings"):
            apply_profile(malformed, "num01", VALID)


if __name__ == "__main__":
    unittest.main()
