from __future__ import annotations

import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

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
