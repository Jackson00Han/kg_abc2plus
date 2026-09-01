"""The domain package must import without provider or database credentials."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


class CredentialFreeImportTests(unittest.TestCase):
    def test_domain_import_has_no_secret_dependency(self) -> None:
        environment = os.environ.copy()
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "NEO4J_URI",
            "NEO4J_PASSWORD",
        ):
            environment.pop(name, None)
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [sys.executable, "-c", "import graphrag_prod.domain"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
