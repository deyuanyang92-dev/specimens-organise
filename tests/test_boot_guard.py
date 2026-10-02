import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from specimen_app import boot_guard

REPO = Path(__file__).resolve().parents[1]


class BootGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._env = patch.dict(os.environ, {"APPDATA": str(self.tmp)})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_startup_failure_writes_log_with_traceback(self):
        try:
            raise RuntimeError("boom-启动")
        except RuntimeError as exc:
            path = boot_guard.write_startup_failure(exc)
        self.assertIsNotNone(path)
        self.assertEqual(path.parent, self.tmp / boot_guard.APP_DIR_NAME)
        text = path.read_text(encoding="utf-8")
        self.assertIn("boom-启动", text)
        self.assertIn("Traceback", text)

    def test_show_fatal_survives_missing_stderr(self):
        # PyInstaller --windowed：sys.stderr 是 None，旧代码的 print 静默丢失
        with patch.object(sys, "stderr", None):
            boot_guard.show_fatal("x")  # 不抛

    def test_app_dir_name_matches_settings(self):
        from specimen_app import app_settings

        self.assertEqual(boot_guard.APP_DIR_NAME, app_settings.APP_DIR_NAME)


class SmokeFlagTests(unittest.TestCase):
    def test_smoke_flag_exits_zero(self):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", APPDATA=tempfile.mkdtemp())
        r = subprocess.run([sys.executable, "run_app.py", "--smoke"], cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("[smoke] ok", r.stdout)

    def test_import_failure_is_reported_not_silent(self):
        appdata = tempfile.mkdtemp()
        code = ("import sys, runpy; sys.frozen = True; sys.modules['specimen_app.main'] = None; "
                "sys.argv=['run_app.py']; runpy.run_path('run_app.py', run_name='__main__')")
        env = dict(os.environ, APPDATA=appdata)
        r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        logs = list((Path(appdata) / boot_guard.APP_DIR_NAME).glob("startup_failure_*.log"))
        self.assertEqual(len(logs), 1, r.stderr)
        self.assertIn("启动失败", r.stderr)


if __name__ == "__main__":
    unittest.main()
