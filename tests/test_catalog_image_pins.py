"""Guard the published catalog image references and their platform claims."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "claudecode": (
        "ghcr.io/aceteam-ai/claudecode-service:"
        "6fad6be3951e28d6a4ad34a6e39e8c90113328ae@"
        "sha256:c2285d2bfe1db43086e240c4c95b3f605bbf83f87579fa29d1b97bade77feee2"
    ),
    "kokoro": (
        "ghcr.io/aceteam-ai/kokoro-service:sha-ad043e8@"
        "sha256:0f99603bc21c89b5bfbc941eaec0329805ba52b923b05414c26a111636452241"
    ),
}


class CatalogImagePinsTest(unittest.TestCase):
    def test_compose_images_match_verified_indexes(self):
        for service, expected in EXPECTED.items():
            with self.subTest(service=service):
                compose = (ROOT / "services" / service / "compose.yml").read_text()
                images = re.findall(r"(?m)^\s+image:\s*(\S+)\s*$", compose)
                self.assertEqual(images, [expected])

    def test_advertised_architectures_match_published_indexes(self):
        for service in EXPECTED:
            with self.subTest(service=service):
                manifest = (ROOT / "services" / service / "service.yaml").read_text()
                arch = re.findall(r"(?m)^\s+arch:\s*(\[[^\]]+\])\s*$", manifest)
                self.assertEqual(arch, ["[amd64]"])

    def test_kokoro_default_cannot_build_around_pinned_image(self):
        kokoro = ROOT / "services" / "kokoro"
        default = (kokoro / "compose.yml").read_text()
        gpu = (kokoro / "compose.gpu.yml").read_text()
        self.assertNotRegex(default, r"(?m)^\s+build:\s*$")
        self.assertRegex(gpu, r"(?m)^\s+build:\s*$")


if __name__ == "__main__":
    unittest.main()
