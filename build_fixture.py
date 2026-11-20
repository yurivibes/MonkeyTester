"""Test helper: a Build & Test build record for /api/run-test.

The viewer takes a run only when its upload names a known build_request, so
tests that upload an app record a build under the test's Run folder first.
"""
from pathlib import Path

import app
import builds

BUILD_ID = "build_0123456789ab"


def add_build(repo=""):
    """Record BUILD_ID under the viewer's current TEST_DIR (patched by the test)."""
    builds.BUILDS_DIR = Path(app.TEST_DIR) / ".builds"
    builds.save_build({
        "id": BUILD_ID, "repo": repo, "branch": "feat/x", "platforms": ["android", "ios"],
        "cases": ["eula"], "state": "building", "runs": [], "created": 0,
        "plugin": "build-local", "build_ref": "br_0000000000000000",
    })
    return BUILD_ID
