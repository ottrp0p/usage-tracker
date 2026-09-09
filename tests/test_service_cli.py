"""Lifecycle failures must not masquerade as a successful service change."""

from contextlib import redirect_stderr
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch
import io
import plistlib
import tempfile
import unittest

from usage_tracker.cli import main
from usage_tracker.config import DEFAULT_PORT
from usage_tracker.service import manage_service, plist_payload


class ServiceTests(unittest.TestCase):
    def test_install_retries_transient_launchd_bootstrap_race(self):
        with tempfile.TemporaryDirectory() as temp, patch("usage_tracker.service.sys.platform", "darwin"), patch("usage_tracker.service.Path.home", return_value=Path(temp)):
            outcomes = [CompletedProcess([], 1), CompletedProcess([], 5), CompletedProcess([], 0)]
            with patch("usage_tracker.service.launchctl", side_effect=outcomes) as launch, patch("usage_tracker.server.make_server"), patch("usage_tracker.service.time.sleep") as sleep:
                self.assertIn("installed", manage_service("install", Path(temp) / "data"))
                sleep.assert_called_once_with(0.2)
                self.assertEqual([entry.args[0] for entry in launch.call_args_list], ["print", "bootstrap", "bootstrap"])

    def old_plist(self, root):
        """The temporary 8447 deployment remains recognizable during restoration."""
        path = root / "Library/LaunchAgents/local.usage-tracker.plist"
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({"Label": "local.usage-tracker", "ProgramArguments": ["python", "-m", "usage_tracker", "serve", "--port", "8447"]}))
        return path

    def test_install_unloads_temporary_8447_job_then_probes_and_bootstraps_8787(self):
        with tempfile.TemporaryDirectory() as temp, patch("usage_tracker.service.sys.platform", "darwin"), patch("usage_tracker.service.Path.home", return_value=Path(temp)):
            root = Path(temp)
            plist = self.old_plist(root)
            config = root / "sources.json"
            config.write_text('{"include_defaults": false, "sources": []}')
            data = root / "data"
            data.mkdir()
            sentinel = data / "preserve-me"
            sentinel.write_text("existing usage remains")
            order = []
            prints = 0

            def launch(*args):
                nonlocal prints
                order.append(args[0])
                if args[0] == "print":
                    prints += 1
                    return CompletedProcess([], 0 if prints == 1 else 1)
                if args[0] == "bootstrap":
                    payload = plistlib.loads(plist.read_bytes())
                    self.assertIn(str(DEFAULT_PORT), payload["ProgramArguments"])
                    self.assertNotIn("8447", payload["ProgramArguments"])
                return CompletedProcess([], 0)

            def probe(*args, **kwargs):
                self.assertEqual(DEFAULT_PORT, kwargs["port"])
                order.append("probe8787")
                return MagicMock(server_close=lambda: order.append("close-probe"))

            with patch("usage_tracker.service.launchctl", side_effect=launch), patch("usage_tracker.server.make_server", side_effect=probe):
                self.assertIn("http://localhost:8787", manage_service("install", data, config_path=config))
            self.assertEqual(["print", "bootout", "print", "probe8787", "close-probe", "bootstrap"], order)
            self.assertEqual("existing usage remains", sentinel.read_text())
            self.assertEqual('{"include_defaults": false, "sources": []}', config.read_text())
            self.assertIn(str(config.resolve()), plistlib.loads(plist.read_bytes())["ProgramArguments"])

    def test_install_rejects_other_ports_before_lifecycle_actions(self):
        with tempfile.TemporaryDirectory() as temp, patch("usage_tracker.service.sys.platform", "darwin"), patch("usage_tracker.service.launchctl") as launch, patch("usage_tracker.server.make_server") as probe:
            data = Path(temp) / "data"
            for port in (8447, 8788):
                with self.subTest(port=port), self.assertRaisesRegex(ValueError, "8787"):
                    manage_service("install", data, port=port)
            launch.assert_not_called()
            probe.assert_not_called()
            self.assertFalse(data.exists())

    def test_plist_rejects_other_ports(self):
        with self.assertRaisesRegex(ValueError, "8787"):
            plist_payload(Path("/unused"), port=8447)

    def test_install_unload_failure_preserves_plist_and_prevents_bind_probe(self):
        with tempfile.TemporaryDirectory() as temp, patch("usage_tracker.service.sys.platform", "darwin"), patch("usage_tracker.service.Path.home", return_value=Path(temp)):
            plist = self.old_plist(Path(temp))
            original = plist.read_bytes()
            with patch("usage_tracker.service.launchctl", side_effect=[CompletedProcess([], 0), CompletedProcess([], 5)]) as launch, patch("usage_tracker.server.make_server") as probe:
                with self.assertRaisesRegex(ValueError, "Could not unload"):
                    manage_service("install", Path(temp) / "data")
                probe.assert_not_called()
                self.assertEqual(["print", "bootout"], [entry.args[0] for entry in launch.call_args_list])
            self.assertEqual(original, plist.read_bytes())

    def test_install_occupied_8787_preserves_plist_without_bootstrap_or_other_port(self):
        with tempfile.TemporaryDirectory() as temp, patch("usage_tracker.service.sys.platform", "darwin"), patch("usage_tracker.service.Path.home", return_value=Path(temp)):
            plist = self.old_plist(Path(temp))
            original = plist.read_bytes()
            data = Path(temp) / "data"
            with patch("usage_tracker.service.launchctl", return_value=CompletedProcess([], 1)) as launch, patch("usage_tracker.server.make_server", side_effect=ValueError("port became occupied")) as probe:
                with self.assertRaisesRegex(ValueError, "TCP 8787 is unavailable"):
                    manage_service("install", data)
                probe.assert_called_once_with(data, port=DEFAULT_PORT)
                self.assertEqual(["print"], [entry.args[0] for entry in launch.call_args_list])
            self.assertEqual(original, plist.read_bytes())

    def test_install_permanent_bootstrap_failure_does_not_try_another_port(self):
        with tempfile.TemporaryDirectory() as temp, patch("usage_tracker.service.sys.platform", "darwin"), patch("usage_tracker.service.Path.home", return_value=Path(temp)):
            data = Path(temp) / "data"
            with patch("usage_tracker.service.launchctl", side_effect=[CompletedProcess([], 1), CompletedProcess([], 64)]) as launch, patch("usage_tracker.server.make_server") as probe, patch("usage_tracker.service.time.sleep") as sleep:
                with self.assertRaisesRegex(ValueError, "exit 64"):
                    manage_service("install", data)
                probe.assert_called_once_with(data, port=DEFAULT_PORT)
                sleep.assert_not_called()
                self.assertEqual(["print", "bootstrap"], [entry.args[0] for entry in launch.call_args_list])

    def test_explicit_missing_config_fails_without_creating_data(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with redirect_stderr(io.StringIO()) as stderr:
                status = main(["--data-dir", str(root / "data"), "--config", str(root / "missing.json"), "collect"])
            self.assertEqual(status, 2)
            self.assertIn("Explicit source configuration", stderr.getvalue())
            self.assertFalse((root / "data").exists())

    def test_uninstall_attempts_unload_even_without_plist(self):
        with tempfile.TemporaryDirectory() as temp, patch("usage_tracker.service.sys.platform", "darwin"), patch("usage_tracker.service.Path.home", return_value=Path(temp)):
            with patch("usage_tracker.service.launchctl", side_effect=[CompletedProcess([], 0), CompletedProcess([], 0), CompletedProcess([], 1)]) as launch:
                self.assertIn("removed", manage_service("uninstall", Path(temp) / "data"))
                self.assertEqual([call.args[0] for call in launch.call_args_list], ["print", "bootout", "print"])

    def test_uninstall_unload_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp, patch("usage_tracker.service.sys.platform", "darwin"), patch("usage_tracker.service.Path.home", return_value=Path(temp)):
            with patch("usage_tracker.service.launchctl", side_effect=[CompletedProcess([], 0), CompletedProcess([], 5)]):
                with self.assertRaisesRegex(ValueError, "Could not unload"):
                    manage_service("uninstall", Path(temp) / "data")
