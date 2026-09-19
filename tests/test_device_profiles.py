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
    parse_cpus,
    parse_memory_bytes,
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
VALID_V2 = dict(VALID, schema_version=2, timezone="Asia/Tehran",
                resources={"cpus": 4.0, "memory_gib": 4.0})


class DeviceProfileTests(unittest.TestCase):
    def test_example_loads_and_normalises_locale(self):
        profile = load(Path("installer/device-profile.example.json"))
        self.assertIsInstance(profile, DeviceProfile)
        self.assertEqual(profile.android_version, 12)
        self.assertEqual(profile.locale, "en-US")
        self.assertEqual(profile.timezone, "Asia/Tehran")
        self.assertEqual((profile.cpus, profile.memory_mib, profile.memory_limit), (4.0, 4096, "4096m"))
        self.assertEqual(profile.to_dict(), VALID_V2)
        self.assertRegex(profile.digest, r"^[0-9a-f]{64}$")

        lower = validate(dict(VALID, locale="FA-ir"))
        self.assertEqual(lower.locale, "fa-IR")

    def test_schema_one_profiles_keep_their_digest_and_have_no_optional_fields(self):
        profile = validate(VALID)
        self.assertEqual(profile.to_dict(), VALID)
        self.assertIsNone(profile.timezone)
        self.assertIsNone(profile.cpus)
        self.assertIsNone(profile.memory_limit)
        # Optional keys are a schema 2 feature; schema 1 files stay strict.
        for extra in ({"timezone": "UTC"}, {"resources": {"cpus": 2, "memory_gib": 2}}):
            with self.subTest(extra=extra), self.assertRaisesRegex(ValueError, "unsupported"):
                validate(dict(VALID, **extra))
        self.assertNotEqual(profile.digest, validate(VALID_V2).digest)

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
        for version in (10, 14, True):
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate(dict(VALID, android_version=version))
        self.assertEqual(validate(dict(VALID, android_version=13)).android_version, 13)

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
            dict(VALID, schema_version=3),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)

    def test_timezone_must_be_a_real_iana_zone(self):
        for zone in ("UTC", "Europe/Berlin", "America/Argentina/Buenos_Aires", "Etc/GMT+3"):
            with self.subTest(zone=zone):
                self.assertEqual(validate(dict(VALID_V2, timezone=zone)).timezone, zone)
        for zone in ("Tehran", "asia/tehran", "../../etc/passwd", "Asia/Tehran; rm -rf /",
                     "Mars/Olympus", "", 3):
            with self.subTest(zone=zone), self.assertRaisesRegex(ValueError, "timezone"):
                validate(dict(VALID_V2, timezone=zone))

    def test_resource_ceilings_stay_within_the_audited_budget(self):
        profile = validate(dict(VALID_V2, resources={"cpus": 1.5, "memory_gib": 2.25}))
        self.assertEqual((profile.cpus, profile.memory_mib, profile.memory_limit), (1.5, 2304, "2304m"))
        for resources in ({"cpus": 0.5, "memory_gib": 4}, {"cpus": 4.5, "memory_gib": 4},
                          {"cpus": 4, "memory_gib": 1}, {"cpus": 4, "memory_gib": 8},
                          {"cpus": 1.1, "memory_gib": 4}, {"cpus": True, "memory_gib": 4},
                          {"cpus": 4}, {"cpus": 4, "memory_gib": 4, "pids": 1}, "4 cpus"):
            with self.subTest(resources=resources), self.assertRaisesRegex(ValueError, "resources"):
                validate(dict(VALID_V2, resources=resources))

    def test_compose_resource_values_are_parsed_in_every_rendering(self):
        self.assertEqual(parse_memory_bytes("4g"), 4 * 1024 ** 3)
        self.assertEqual(parse_memory_bytes("2304m"), 2304 * 1024 ** 2)
        self.assertEqual(parse_memory_bytes("4294967296"), 4 * 1024 ** 3)
        self.assertEqual(parse_memory_bytes(4294967296), 4 * 1024 ** 3)
        self.assertEqual(parse_cpus(4), 4.0)
        self.assertEqual(parse_cpus("1.5"), 1.5)
        for value in ("4 cows", None, True, "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_memory_bytes(value)
        for value in (None, True, "many", 0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_cpus(value)

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
        # A profile without ceilings leaves the audited budget untouched.
        self.assertEqual((android["cpus"], android["mem_limit"]), (4.0, "4g"))
        for prefix in ("androidboot.redroid_width=", "ro.product.model=", "ro.product.brand="):
            self.assertEqual(sum(item.startswith(prefix) for item in command), 1)

    def test_apply_profile_lowers_only_the_android_ceiling(self):
        rendered = apply_profile(generate(1), "num01",
                                 validate(dict(VALID_V2, android_version=13,
                                               resources={"cpus": 2, "memory_gib": 3})))
        android = rendered["services"]["android-num01"]
        self.assertEqual(android["image"], "redroid/redroid:13.0.0-latest")
        self.assertEqual((android["cpus"], android["mem_limit"]), (2.0, "3072m"))
        self.assertEqual(rendered["services"]["proxy-num01"]["cpus"], 0.5)
        self.assertEqual(rendered["services"]["screen-num01"]["mem_limit"], "1g")
        # Timezone is applied through ADB after boot, never as a kernel argument.
        self.assertFalse(any("timezone" in item.lower() for item in android["command"]))

    def test_apply_profile_validates_compose_shape(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            apply_profile({"services": {}}, "num01", VALID)
        malformed = generate(1)
        malformed["services"]["android-num01"]["command"] = "not-a-list"
        with self.assertRaisesRegex(ValueError, "list of strings"):
            apply_profile(malformed, "num01", VALID)


if __name__ == "__main__":
    unittest.main()
