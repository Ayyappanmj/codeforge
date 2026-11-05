import unittest

import tests  # noqa: F401
from codeforge_shared.config import ConfigError, ResourceLimits, Settings
from codeforge_shared.languages import UnsupportedLanguage, build_registry, get_language


class LanguageRegistryTests(unittest.TestCase):
    def test_known_languages(self):
        reg = build_registry({})
        self.assertEqual(set(reg), {"python", "javascript"})
        self.assertEqual(get_language("python", reg).image, "codeforge-python-runner:latest")

    def test_unsupported_language(self):
        with self.assertRaises(UnsupportedLanguage) as cm:
            get_language("cobol", build_registry({}))
        self.assertIn("python", str(cm.exception))

    def test_non_string_language(self):
        with self.assertRaises(UnsupportedLanguage):
            get_language(["python"], build_registry({}))  # unhashable

    def test_image_override_is_validated(self):
        self.assertEqual(build_registry({"IMAGE_PYTHON": "my/py:1"})["python"].image, "my/py:1")
        with self.assertRaises(ValueError):
            build_registry({"IMAGE_PYTHON": "evil --privileged"})


class LimitsTests(unittest.TestCase):
    def test_defaults_match_spec(self):
        d = ResourceLimits()
        self.assertEqual((d.timeout_seconds, d.memory_mb, d.cpus, d.pids), (5.0, 128, 1.0, 32))
        self.assertEqual((d.max_source_bytes, d.max_stdin_bytes, d.max_output_bytes),
                         (65536, 16384, 65536))

    def test_from_env(self):
        lim = ResourceLimits.from_env({"EXEC_TIMEOUT_SECONDS": "2", "EXEC_MEMORY_MB": "64"})
        self.assertEqual((lim.timeout_seconds, lim.memory_mb), (2.0, 64))

    def test_out_of_range_rejected(self):
        for env in ({"EXEC_TIMEOUT_SECONDS": "0"}, {"EXEC_MEMORY_MB": "999999"},
                    {"EXEC_PIDS_LIMIT": "1"}, {"EXEC_CPUS": "64"}):
            with self.assertRaises(ConfigError):
                ResourceLimits.from_env(env)

    def test_non_numeric_rejected(self):
        with self.assertRaises(ConfigError):
            ResourceLimits.from_env({"EXEC_MEMORY_MB": "lots"})

    def test_settings_docker_bin_validated(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"DOCKER_BIN": "docker; rm -rf /"})
        self.assertEqual(Settings.from_env({}).max_attempts, 3)

    def test_stale_threshold_exceeds_timeout(self):
        s = Settings.from_env({"EXEC_TIMEOUT_SECONDS": "10"})
        self.assertGreater(s.stale_running_seconds, 10)


if __name__ == "__main__":
    unittest.main()
