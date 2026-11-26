"""The .githooks/pre-push hook runs ci/docs-drift.sh on the commits being
pushed, so a source change without an AGENTS.md change is refused before
the PR exists (CI runs the same script on the PR)."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZERO = "0" * 40


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class PrePushHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        repo = self.tmp / "repo"
        (repo / "ci").mkdir(parents=True)
        (repo / ".githooks").mkdir()
        shutil.copy(ROOT / "ci" / "docs-drift.sh", repo / "ci")
        shutil.copy(ROOT / ".githooks" / "pre-push", repo / ".githooks")
        (repo / "app.py").write_text("x = 1\n")
        (repo / "AGENTS.md").write_text("docs\n")
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        self.env.pop("CHANGED_FILES", None)
        subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
        git(repo, "add", "-A")
        subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, env=self.env, check=True)
        # origin/master = the base commit, as after a fetch
        git(repo, "update-ref", "refs/remotes/origin/master", "HEAD")
        git(repo, "checkout", "-qb", "feature")
        self.repo = repo

    def commit(self, files):
        for name, text in files.items():
            (self.repo / name).write_text(text)
        git(self.repo, "add", "-A")
        subprocess.run(["git", "commit", "-qm", "change"], cwd=self.repo, env=self.env, check=True)
        return git(self.repo, "rev-parse", "HEAD")

    def push(self, sha, local_ref="refs/heads/feature"):
        line = f"{local_ref} {sha} refs/heads/feature {ZERO}\n"
        return subprocess.run(["bash", ".githooks/pre-push", "origin", "url"],
                              cwd=self.repo, input=line, env=self.env,
                              capture_output=True, text=True)

    def test_source_change_without_agents_md_is_refused(self):
        r = self.push(self.commit({"app.py": "x = 2\n"}))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("push refused", r.stderr)

    def test_source_change_with_agents_md_passes(self):
        self.commit({"app.py": "x = 2\n"})
        r = self.push(self.commit({"AGENTS.md": "docs 2\n"}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_non_source_change_passes(self):
        r = self.push(self.commit({"notes.txt": "hi\n"}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_branch_delete_is_ignored(self):
        r = self.push(ZERO, local_ref="(delete)")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_hook_is_executable(self):
        self.assertTrue(os.access(ROOT / ".githooks" / "pre-push", os.X_OK))


if __name__ == "__main__":
    unittest.main()
