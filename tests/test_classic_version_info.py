import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ClassicVersionInfoTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "version_info.txt").is_file(), "version_info.txt ausente na cópia pública")
    def test_release_metadata_matches_classic_232(self):
        metadata = (ROOT / "version_info.txt").read_text(encoding="utf-8")

        self.assertIn("filevers=(2, 3, 2, 0)", metadata)
        self.assertIn("prodvers=(2, 3, 2, 0)", metadata)
        self.assertIn("StringStruct(u'FileVersion', u'2.3.2')", metadata)
        self.assertIn("StringStruct(u'ProductVersion', u'2.3.2')", metadata)
        self.assertIn("StringStruct(u'ProductName', u'Viral Cutter Classic')", metadata)
        self.assertIn("StringStruct(u'FileDescription', u'Viral Cutter Classic')", metadata)
        self.assertIn("StringStruct(u'InternalName', u'ViralCutter')", metadata)
        self.assertIn("StringStruct(u'OriginalFilename', u'ViralCutter.exe')", metadata)

    @unittest.skipUnless((ROOT / "viral_cutter.spec").is_file(), "viral_cutter.spec ausente na cópia pública")
    def test_pyinstaller_spec_uses_version_info(self):
        spec = (ROOT / "viral_cutter.spec").read_text(encoding="utf-8")
        self.assertIn('version="version_info.txt"', spec)


if __name__ == "__main__":
    unittest.main()
