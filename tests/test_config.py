from pathlib import Path
import tempfile
import unittest

from tg_archiver.config import load


class ConfigTests(unittest.TestCase):
    def read_config(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(source, encoding="utf-8")
            return load(path)

    def test_category_override_and_size_cap(self):
        config = self.read_config('''
[archive]
excluded_chats = [10]
max_file_bytes = 100
[media]
videos = false
[chats."20".media]
videos = true
''')
        self.assertFalse(config.included(10))
        self.assertFalse(config.allows(30, "videos", 1))
        self.assertTrue(config.allows(20, "videos", 100))
        self.assertFalse(config.allows(20, "videos", 101))

    def test_fingerprint_detects_reenable(self):
        disabled = self.read_config('[archive]\nexcluded_chats=[42]\n')
        enabled = self.read_config("")
        self.assertNotEqual(disabled.fingerprint(42), enabled.fingerprint(42))

    def test_rejects_typos_and_invalid_types(self):
        for source in ['[media]\nvideo=true', '[media]\nvideos="false"',
                       '[archive]\nport=true', '[archive]\nmax_file_bytes=-1',
                       '[chats."42"]\nenabled="false"', '[archive]\nexcluded_chats=["42"]']:
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.read_config(source)


if __name__ == "__main__":
    unittest.main()
