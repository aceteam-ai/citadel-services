from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator

from scripts import validate_catalog

ROOT = Path(__file__).resolve().parents[1]


class RegistrySchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = yaml.safe_load(
            (ROOT / "schema" / "registry-schema.yaml").read_text()
        )
        cls.validator = Draft202012Validator(cls.schema)

    def test_current_registry_is_valid(self) -> None:
        registry = yaml.safe_load((ROOT / "registry.yaml").read_text())

        self.assertEqual(list(self.validator.iter_errors(registry)), [])

    def test_mutable_execution_tag_is_rejected(self) -> None:
        registry = self.registry_with_image(
            name="python",
            image="ghcr.io/aceteam-ai/aceteam-app-python:stable",
            discovery_alias="ghcr.io/aceteam-ai/aceteam-app-python:stable",
        )

        self.assertNotEqual(list(self.validator.iter_errors(registry)), [])

    def test_runtime_name_must_match_digest_and_discovery_repositories(self) -> None:
        registry = self.registry_with_image(
            name="python",
            image=f"ghcr.io/aceteam-ai/aceteam-app-node@sha256:{'a' * 64}",
            discovery_alias="ghcr.io/aceteam-ai/aceteam-app-node:stable",
        )

        self.assertNotEqual(list(self.validator.iter_errors(registry)), [])

    def test_different_runtime_objects_cannot_share_a_name(self) -> None:
        registry = yaml.safe_load(
            (ROOT / "tests" / "fixtures" / "duplicate-runtime-names.yaml").read_text()
        )
        # Whole-object uniqueness is insufficient: the objects are different.
        self.assertEqual(list(self.validator.iter_errors(registry)), [])
        errors = validate_catalog.validate_registry(registry, self.schema, "fixture")
        self.assertEqual(len(errors), 1)
        self.assertIn("duplicate runtime name 'python'", errors[0])

    def test_catalog_command_rejects_duplicate_runtime_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "schema", root / "schema")
            shutil.copyfile(
                ROOT / "tests" / "fixtures" / "duplicate-runtime-names.yaml",
                root / "registry.yaml",
            )
            stderr = io.StringIO()
            with (
                patch.object(validate_catalog, "ROOT", root),
                patch("sys.stderr", stderr),
            ):
                self.assertEqual(validate_catalog.main(), 1)
            self.assertIn("registry.yaml:runtime_images.1.name", stderr.getvalue())

    def test_distinct_runtime_names_remain_valid(self) -> None:
        registry = yaml.safe_load(
            (ROOT / "tests" / "fixtures" / "duplicate-runtime-names.yaml").read_text()
        )
        registry["runtime_images"][1] = {
            "name": "node",
            "image": f"ghcr.io/aceteam-ai/aceteam-app-node@sha256:{'b' * 64}",
            "discovery_alias": "ghcr.io/aceteam-ai/aceteam-app-node:stable",
            "architectures": ["arm64"],
        }
        self.assertEqual(
            validate_catalog.validate_registry(registry, self.schema, "fixture"), []
        )

    @staticmethod
    def registry_with_image(
        *, name: str, image: str, discovery_alias: str
    ) -> dict[str, object]:
        return {
            "version": 1,
            "runtime_images": [
                {
                    "name": name,
                    "image": image,
                    "discovery_alias": discovery_alias,
                    "architectures": ["amd64", "arm64"],
                }
            ],
            "services": [],
        }


if __name__ == "__main__":
    unittest.main()
