import unittest

from cullumi import core
from cullumi.classification import project_photo_counts
from cullumi.config import ConfigStore
from cullumi.project_store import ProjectManager, connect_db
from cullumi.scanner import DiscoveryResult, Scanner


class CoreCompatibilityTests(unittest.TestCase):
    def test_former_core_exports_still_point_to_their_owners(self):
        self.assertIs(core.ConfigStore, ConfigStore)
        self.assertIs(core.ProjectManager, ProjectManager)
        self.assertIs(core.connect_db, connect_db)
        self.assertIs(core.DiscoveryResult, DiscoveryResult)
        self.assertIs(core.Scanner, Scanner)
        self.assertIs(core.project_photo_counts, project_photo_counts)


if __name__ == "__main__":
    unittest.main()
