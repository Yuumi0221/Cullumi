import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from cullumi.http_api import static_asset_revision

WEB_SCRIPT_FILES = (
    "js/runtime.js",
    "js/gallery-tools.js",
    "js/session.js",
    "js/similar.js",
    "js/settings.js",
    "js/gallery.js",
    "js/viewer.js",
    "js/app.js",
)
WEB_STYLE_FILES = (
    "css/base.css",
    "css/workspace.css",
    "css/viewer.css",
    "css/settings.css",
    "css/theme.css",
    "css/responsive.css",
    "css/home.css",
)


class WebResourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.web = cls.root / "web"
        cls.markup = (cls.web / "index.html").read_text(encoding="utf-8")

    def test_resources_are_grouped_and_loaded_once_in_dependency_order(self):
        self.assertEqual(
            {path.name for path in (self.web / "js").glob("*.js")},
            {Path(path).name for path in WEB_SCRIPT_FILES},
        )
        self.assertEqual(
            {path.name for path in (self.web / "css").glob("*.css")},
            {Path(path).name for path in WEB_STYLE_FILES},
        )
        for resources in (WEB_STYLE_FILES, WEB_SCRIPT_FILES):
            positions = []
            for filename in resources:
                source = f"/static/{filename}"
                self.assertEqual(self.markup.count(source), 1)
                positions.append(self.markup.index(source))
            self.assertEqual(positions, sorted(positions))

    def test_static_references_use_one_asset_revision_placeholder(self):
        revisions = re.findall(
            r"/static/[^\"'#?]+\?v=([^\"'#]+)", self.markup
        )
        self.assertTrue(revisions)
        self.assertEqual(set(revisions), {"__ASSET_REVISION__"})
        self.assertIn(
            'window.ASSET_REVISION="__ASSET_REVISION__"', self.markup
        )

    def test_asset_revision_changes_only_when_static_content_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            web = Path(temporary)
            (web / "css").mkdir()
            index = web / "index.html"
            asset = web / "css" / "app.css"
            index.write_text("first index", encoding="utf-8")
            asset.write_text("first asset", encoding="utf-8")
            first = static_asset_revision(web)

            index.write_text("second index", encoding="utf-8")
            self.assertEqual(static_asset_revision(web), first)

            asset.write_text("second asset", encoding="utf-8")
            second = static_asset_revision(web)
            self.assertNotEqual(second, first)
            self.assertRegex(second, r"^[0-9a-f]{12}$")

    def test_svg_symbols_and_references_are_exactly_in_sync(self):
        sprite = self.web / "assets" / "icons.svg"
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        symbols = {
            element.attrib["id"]
            for element in ET.parse(sprite).getroot().findall(
                "svg:symbol", namespace
            )
        }
        sources = [self.web / "index.html", *(self.web / "js").glob("*.js")]
        references = {
            match.group(1)
            for source in sources
            for match in re.finditer(
                r"(?:icons\.svg[^#\"']*|ICONS_URL\})#([A-Za-z0-9_-]+)",
                source.read_text(encoding="utf-8"),
            )
        }
        self.assertEqual(symbols, references)

    def test_component_styles_and_scripts_have_single_owners(self):
        workspace = (self.web / "css" / "workspace.css").read_text(
            encoding="utf-8"
        )
        viewer_style = (self.web / "css" / "viewer.css").read_text(
            encoding="utf-8"
        )
        settings_style = (self.web / "css" / "settings.css").read_text(
            encoding="utf-8"
        )
        gallery = (self.web / "js" / "gallery.js").read_text(encoding="utf-8")
        viewer = (self.web / "js" / "viewer.js").read_text(encoding="utf-8")
        self.assertNotRegex(workspace, r"(?m)^\.viewer(?:\s|::)")
        self.assertNotRegex(workspace, r"(?m)^\.settings-")
        self.assertIn(".viewer {", viewer_style)
        self.assertIn("#settings.panel-dialog {", settings_style)
        self.assertNotIn("function openViewer(", gallery)
        self.assertIn("function openViewer(", viewer)
        self.assertIn("function bindGalleryEvents()", gallery)
        self.assertIn("function bindViewerEvents()", viewer)

    def test_security_and_destructive_action_contracts_remain_explicit(self):
        scripts = "\n".join(
            (self.web / filename).read_text(encoding="utf-8")
            for filename in WEB_SCRIPT_FILES
        )
        server = (self.root / "cullumi" / "http_api.py").read_text(
            encoding="utf-8"
        )
        self.assertRegex(scripts, r"\bconst\s+esc\s*=")
        self.assertRegex(
            scripts,
            r'''headers\s*:\s*\{\s*["']Content-Type["']\s*:\s*'''
            r'''["']application/json["']\s*,\s*["']X-App-Token["']\s*:\s*'''
            r"TOKEN\s*,?\s*\}",
        )
        self.assertIn("safe_relative_path", server)
        for function in (
            "confirmDeleteProfile",
            "confirmClearDecisions",
            "confirmAiRemoveSuggestions",
            "quarantine",
        ):
            self.assertIn(f"function {function}(", scripts)

    def test_binary_assets_and_model_licenses_are_present(self):
        model_root = self.root / "models"
        self.assertEqual(
            [path.name for path in model_root.iterdir() if path.is_file()], []
        )
        for relative in (
            "web/assets/images/brand-icon.png",
            "web/assets/icons/brand-icon.ico",
            "models/blink/LICENSE-YUNET.txt",
            "models/blink/LICENSE-OCEC.txt",
            "models/blink/LICENSE-ONNXRUNTIME.txt",
            "models/blink/README.md",
            "models/niqe/LICENSE.txt",
            "models/niqe/SOURCE.json",
            "models/niqe/ADAPTATION.md",
        ):
            self.assertTrue((self.root / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
