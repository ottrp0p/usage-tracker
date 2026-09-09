"""The public deployment entry points must never switch away from TCP 8787."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from usage_tracker.cli import parser
from usage_tracker.server import serve


class DeploymentPortTests(unittest.TestCase):
    def test_cli_defaults_and_explicit_port_remain_8787(self):
        for command in (["serve"], ["service", "install"]):
            with self.subTest(command=command):
                self.assertEqual(parser().parse_args(command).port, 8787)
                self.assertEqual(parser().parse_args([*command, "--port", "8787"]).port, 8787)

    def test_cli_rejects_an_alternate_port_before_deployment(self):
        for command in (["serve"], ["service", "install"]):
            with self.subTest(command=command), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as rejected:
                    parser().parse_args([*command, "--port", "8447"])
                self.assertEqual(rejected.exception.code, 2)

    def test_direct_serve_rejects_an_alternate_port_before_binding(self):
        # Guard the callable entry point as well as argparse. The HTTP factory's
        # ephemeral test sockets remain covered by existing integration tests.
        with patch("usage_tracker.server.make_server") as bind:
            with self.assertRaisesRegex(ValueError, "8787"):
                serve(Path("unused"), port=8447)
            bind.assert_not_called()
