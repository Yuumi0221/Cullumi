import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

WEB_SCRIPT_FILES = (
    "js/runtime.js",
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
                r"icons\.svg[^#\"']*#([A-Za-z0-9_-]+)",
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
        self.assertIn("const esc=", scripts)
        self.assertIn('headers:{"Content-Type":"application/json","X-App-Token":TOKEN}', scripts)
        self.assertIn("safe_relative_path", server)
        for function in (
            "confirmDeleteProfile",
            "confirmClearDecisions",
            "confirmAiRemoveSuggestions",
            "quarantine",
        ):
            self.assertIn(f"function {function}(", scripts)

    def test_binary_assets_and_model_licenses_are_present(self):
        for relative in (
            "web/assets/images/brand-icon.png",
            "web/assets/icons/brand-icon.ico",
            "models/LICENSE-YUNET.txt",
            "models/LICENSE-OCEC.txt",
        ):
            self.assertTrue((self.root / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
