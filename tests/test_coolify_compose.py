"""Regression coverage for Coolify's Raw Compose label append behavior.

Run the real Compose boundary check with COMPOSE_BIN=/path/to/docker-compose.
It needs only `config`, not a Docker daemon or running containers.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
LABEL_BLOCK = re.compile(r"^    labels:\n((?:      [^\n]+\n)+)", re.MULTILINE)
MANAGED_LABELS = ["coolify.managed=true", "coolify.applicationId=42", "coolify.type=application"]


def label_lists(text):
    """Read only our quoted KEY=VALUE label blocks, not arbitrary YAML."""
    result = []
    for block in LABEL_BLOCK.finditer(text):
        values = []
        for line in block[1].splitlines():
            if not line.startswith("      - "):
                raise AssertionError("Coolify Raw Compose requires list-style labels: " + line.strip())
            value = json.loads(line[len("      - "):])
            if not isinstance(value, str) or "=" not in value:
                raise AssertionError("Each label must be an explicit KEY=VALUE string")
            values.append(value)
        result.append(values)
    return result


class CoolifyComposeTests(unittest.TestCase):
    def test_every_core_service_survives_raw_parser_label_append(self):
        text = COMPOSE.read_text(encoding="utf-8")
        services = re.findall(r"^  ([\w-]+):$", text.split("\nnetworks:", 1)[0], re.MULTILINE)
        labels = label_lists(text)
        self.assertGreater(len(services), 0)
        self.assertEqual(len(labels), len(services))
        for values in labels:
            # PHP Collection::push preserves numeric list keys. A mapping here
            # would instead produce mixed string/numeric keys and fail Compose.
            appended = values + MANAGED_LABELS
            keys = [entry.partition("=")[0] for entry in appended]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertIn("farm.stack=core", values)
        with self.assertRaisesRegex(AssertionError, "list-style"):
            label_lists('    labels:\n      farm.stack: core\n      0: coolify.managed=true\n')

    @unittest.skipUnless(os.environ.get("COMPOSE_BIN"), "set COMPOSE_BIN for the real Compose check")
    def test_build_and_raw_start_use_the_same_release_images_despite_different_projects(self):
        # Coolify builds inside /artifacts with -p <resource UUID>, but its raw
        # start runs from a host directory containing Compose/env, not the repo.
        # Implicit <project>-<service> image names therefore cannot bridge them.
        text = COMPOSE.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "coolify-application"
            runtime.mkdir()
            copied_compose = runtime / "docker-compose.yaml"
            copied_compose.write_text(text, encoding="utf-8")
            self.assertFalse((runtime / "web").exists())
            previous_images = None
            for release in ("a" * 16, "b" * 16):
                environment = {key: value for key, value in os.environ.items()
                               if not key.startswith("COMPOSE_")}
                environment["FARM_RELEASE_ID"] = release
                stages = []
                for path, folder, project_flags in (
                    (COMPOSE, ROOT, ["--project-name", "coolify-resource-uuid"]),
                    (copied_compose, runtime, []),
                ):
                    result = subprocess.run(
                        [os.environ["COMPOSE_BIN"], *project_flags, "--project-directory", str(folder),
                         "-f", str(path), "config", "--images"],
                        capture_output=True, text=True, timeout=30, env=environment,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    stages.append(set(result.stdout.splitlines()))
                self.assertEqual(stages[0], stages[1])
                for service in ("console", "gateway"):
                    self.assertIn(f"android-farm/{service}:{release}", stages[1])
                if previous_images is not None:
                    self.assertNotEqual(previous_images, stages[1])
                previous_images = stages[1]

    @unittest.skipUnless(os.environ.get("COMPOSE_BIN"), "set COMPOSE_BIN for the real Compose check")
    def test_real_compose_accepts_coolify_augmented_core_without_changing_our_settings(self):
        text = COMPOSE.read_text(encoding="utf-8")
        label_lists(text)  # Refuse to accidentally test an unsupported mapping.
        additions = "".join("      - " + json.dumps(value) + "\n" for value in MANAGED_LABELS)
        augmented = LABEL_BLOCK.sub(lambda match: match[0] + additions, text)
        with tempfile.TemporaryDirectory() as directory:
            monitoring = Path(directory) / "immutable-release" / "monitoring"
            (monitoring / "grafana" / "provisioning").mkdir(parents=True)
            (monitoring / "grafana" / "dashboards").mkdir()
            for name in ("prometheus.yml", "alerts.yml"):
                (monitoring / name).write_text("{}\n", encoding="utf-8")
            environment = {**os.environ, "FARM_MONITORING_DIR": str(monitoring)}
            broken = Path(directory) / "mixed-labels.yml"
            broken.write_text(
                "services:\n  farm-anchor:\n    image: alpine:3.21\n"
                "    labels:\n      farm.stack: core\n      0: coolify.managed=true\n",
                encoding="utf-8",
            )
            failure = subprocess.run(
                [os.environ["COMPOSE_BIN"], "-f", str(broken), "config", "--quiet"],
                capture_output=True, text=True, timeout=30,
            )
            self.assertNotEqual(failure.returncode, 0)
            self.assertIn("non-string key in services.farm-anchor.labels", failure.stderr.lower())
            transformed = Path(directory) / "coolify-compose.yml"
            transformed.write_text(augmented, encoding="utf-8")
            documents = []
            for path in (COMPOSE, transformed):
                result = subprocess.run(
                    [os.environ["COMPOSE_BIN"], "--project-directory", str(ROOT),
                     "-f", str(path), "config", "--format", "json"],
                    capture_output=True, text=True, timeout=30, env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                documents.append(json.loads(result.stdout))
        original, processed = documents
        for service in processed["services"].values():
            labels = service["labels"]
            for entry in MANAGED_LABELS:
                key, _, value = entry.partition("=")
                self.assertEqual(labels.pop(key), value)
        # Routes/auth, release label, no-network helpers, secret mounts and
        # dependency conditions must all survive the upstream label append.
        self.assertEqual(processed, original)
        self.assertEqual(processed["name"], "android-farm-core")
        for service_name in ("console", "gateway"):
            service = processed["services"][service_name]
            self.assertEqual(service["pull_policy"], "never")
            self.assertTrue(service["image"].startswith(f"android-farm/{service_name}:"))
        for service_name, target, relative in (
            ("prometheus", "/etc/prometheus/prometheus.yml", "prometheus.yml"),
            ("prometheus", "/etc/prometheus/alerts.yml", "alerts.yml"),
            ("grafana", "/etc/grafana/provisioning", "grafana/provisioning"),
            ("grafana", "/var/lib/grafana/dashboards", "grafana/dashboards"),
        ):
            mount = next(item for item in processed["services"][service_name]["volumes"]
                         if item["target"] == target)
            self.assertEqual(Path(mount["source"]), monitoring / relative)
            self.assertTrue(mount["read_only"])


if __name__ == "__main__":
    unittest.main()
