"""Static S1 port contracts; no container, compiler, model or node is started."""

import os
import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "services" / "paw-compile"


def registry_defaults(source: str) -> dict[str, int]:
    """Read the registry's literal default claims, refusing unknown Go shapes.

    vLLM is the existing env-resolved exception: use its declared default, not
    the CI runner's environment. This reads source and never executes Go code.
    """
    constants = {
        name: int(value)
        for name, value in re.findall(
            r"(?m)^\s*(?:const\s+)?(\w+)\s*=\s*(\d+)\s*(?://[^\n]*)?$", source
        )
    }
    match = re.search(r"(?ms)^var ServiceHostPorts = map\[string\]int\{\n(.*?)^\}", source)
    if match is None:
        raise ValueError("ServiceHostPorts literal map not found")
    ports = {}
    for line in match.group(1).splitlines():
        line = line.split("//", 1)[0].strip()
        if not line:
            continue
        entry = re.fullmatch(r'"([^"]+)"\s*:\s*(\w+)\s*,', line)
        if entry is None:
            raise ValueError("Review the changed registry entry shape")
        name, symbol = entry.groups()
        if name in ports:
            raise ValueError("Duplicate registry service")
        if symbol == "VLLMHostPort":
            symbol = "defaultVLLMHostPort"
        ports[name] = int(symbol) if symbol.isdecimal() else constants[symbol]
    return ports


class PAWCompilePortContract(unittest.TestCase):
    def setUp(self):
        self.manifest = yaml.safe_load((MODULE / "service.yaml").read_text())
        self.compose = yaml.safe_load((MODULE / "compose.yml").read_text())["services"]["paw-compile"]

    def test_publish_manifest_and_health_agree(self):
        self.assertEqual(len(self.manifest["ports"]), 1)
        port = self.manifest["ports"][0]
        self.assertEqual(port["host"], 8217)
        self.assertEqual(port["container"], 8100)
        self.assertEqual(self.manifest["health_check"]["port"], port["host"])
        self.assertEqual(self.manifest["health_check"]["endpoint"], "/health")
        self.assertEqual(self.compose["ports"], [
            f'127.0.0.1:${{CITADEL_PAW_COMPILE_HOST_PORT:-{port["host"]}}}:{port["container"]}'
        ])

    def test_container_listener_and_health_stay_internal(self):
        command = self.compose["command"]
        self.assertEqual(command[command.index("--port") + 1], "8100")
        self.assertIn("http://localhost:8100/health", self.compose["healthcheck"]["test"][-1])

    def test_no_other_catalog_module_claims_the_port(self):
        host = self.manifest["ports"][0]["host"]
        for path in sorted((ROOT / "services").glob("*/service.yaml")):
            if path.parent == MODULE:
                continue
            manifest = yaml.safe_load(path.read_text())
            for port in manifest.get("ports", []) or []:
                self.assertNotEqual(port.get("host"), host, f"Catalog collision: {path.parent.name}")

    def test_no_other_cli_service_claims_the_port(self):
        source = os.environ.get("CITADEL_CLI_PORTS_GO")
        if not source:
            self.skipTest("Set CITADEL_CLI_PORTS_GO for the companion registry check")
        registry = registry_defaults(Path(source).read_text())
        self.assertTrue(registry, "The companion registry check must inspect actual claims")
        host = self.manifest["ports"][0]["host"]
        for name, claimed in registry.items():
            if name == "paw-compile":
                self.assertEqual(claimed, host)
            else:
                self.assertNotEqual(claimed, host, f"CLI collision: {name}")

    def test_registry_reader_does_not_ignore_unknown_claims(self):
        with self.assertRaises(ValueError):
            registry_defaults('var ServiceHostPorts = map[string]int{\n"other": port(),\n}\n')


if __name__ == "__main__":
    unittest.main()
