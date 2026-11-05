"""
Comprehensive test suite for the Global Permission Confirmation System in ARCDVNCLI.

Tests:
1. Approving commands with 'y', 'Y', 'yes', 'YES'.
2. Denying commands with 'n', 'N', 'no', 'NO', and empty Enter.
3. Invalid input handling followed by valid decision.
4. Two consecutive permission requests (ensuring independence & no stale state).
5. Permission requests during active spinner / Live redraw activity.
6. KeyboardInterrupt (Ctrl+C) and EOFError handling during waiting.
7. All distinct permission handlers (run_command, edit_file, write_file, _uninstall_self).
8. Verification that denied commands never execute on the operating system.
9. Verification that the agent loop resumes correctly after approval and denial.
"""
import builtins
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

for _s in (sys.stdout, sys.stderr):
    try:
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from arcdvncli import agent, cli, llm, tools
from arcdvncli.tools import confirm_action, run_command, edit_file, write_file, console


class BasePermissionTest(unittest.TestCase):
    """Base test class providing an isolated workspace and HOME directory."""

    def setUp(self):
        self.orig_environ = dict(os.environ)
        self.temp_home = tempfile.mkdtemp()
        self.temp_proj = tempfile.mkdtemp()

        os.environ["HOME"] = self.temp_home
        os.environ["USERPROFILE"] = self.temp_home
        os.environ["ARCDVN_API_KEY"] = "sk-test-fake"

        for k in list(os.environ.keys()):
            if k.startswith("ARCDVN_") and k != "ARCDVN_API_KEY":
                del os.environ[k]

        self.orig_cwd = os.getcwd()
        os.chdir(self.temp_proj)
        tools.ROOT = Path(self.temp_proj).resolve()

        self.orig_input = builtins.input

    def tearDown(self):
        os.chdir(self.orig_cwd)
        tools.ROOT = Path(self.orig_cwd).resolve()
        builtins.input = self.orig_input
        os.environ.clear()
        os.environ.update(self.orig_environ)

        try:
            shutil.rmtree(self.temp_home, ignore_errors=True)
            shutil.rmtree(self.temp_proj, ignore_errors=True)
        except Exception:
            pass


class TestPermissionConfirmationBasics(BasePermissionTest):
    """Tests basic confirmation decisions and input variations."""

    def test_approve_lowercase_y(self):
        builtins.input = lambda *a: "y"
        self.assertTrue(confirm_action(prompt="Test approve?"))

    def test_approve_uppercase_y(self):
        builtins.input = lambda *a: "Y"
        self.assertTrue(confirm_action(prompt="Test approve uppercase?"))

    def test_approve_yes_variations(self):
        for val in ["yes", "YES", "Yes", "ya", "YA", "sim", "1"]:
            builtins.input = lambda *a, v=val: v
            self.assertTrue(confirm_action(prompt=f"Test {val}?"), f"Failed for {val}")

    def test_deny_lowercase_n(self):
        builtins.input = lambda *a: "n"
        self.assertFalse(confirm_action(prompt="Test deny?"))

    def test_deny_uppercase_n(self):
        builtins.input = lambda *a: "N"
        self.assertFalse(confirm_action(prompt="Test deny uppercase?"))

    def test_deny_no_variations(self):
        for val in ["no", "NO", "No", "tidak", "TIDAK", "t", "0"]:
            builtins.input = lambda *a, v=val: v
            self.assertFalse(confirm_action(prompt=f"Test {val}?"), f"Failed for {val}")

    def test_deny_empty_enter_default(self):
        builtins.input = lambda *a: ""
        self.assertFalse(confirm_action(prompt="Test default deny?"))

    def test_invalid_input_then_approve(self):
        answers = iter(["invalid_1", "???", "maybe", "Y"])
        builtins.input = lambda *a: next(answers)
        self.assertTrue(confirm_action(prompt="Test retry loop?"))

    def test_invalid_input_then_deny(self):
        answers = iter(["gibberish", "xyz", "n"])
        builtins.input = lambda *a: next(answers)
        self.assertFalse(confirm_action(prompt="Test retry loop deny?"))

    def test_ctrl_c_keyboard_interrupt(self):
        def raise_ki(*a):
            raise KeyboardInterrupt()
        builtins.input = raise_ki
        self.assertFalse(confirm_action(prompt="Test Ctrl+C?"))

    def test_eof_handling(self):
        def raise_eof(*a):
            raise EOFError()
        builtins.input = raise_eof
        self.assertFalse(confirm_action(prompt="Test EOF?"))


class TestSpinnerAndRedrawInteractions(BasePermissionTest):
    """Tests confirmation when active status spinners or live redraws are present."""

    def test_permission_request_during_active_spinner(self):
        builtins.input = lambda *a: "y"
        with console.status("Active long-running spinner...", spinner="dots"):
            # Inside the status context, confirmation is requested
            decision = confirm_action("Allow action inside active spinner?")
            self.assertTrue(decision)

    def test_run_command_during_active_spinner_approved(self):
        builtins.input = lambda *a: "y"
        with console.status("Outer spinner...", spinner="dots"):
            out = run_command("python -c \"print('ran_ok')\"")
            self.assertIn("ran_ok", out)
            self.assertIn("exit_code=0", out)

    def test_run_command_during_active_spinner_denied(self):
        builtins.input = lambda *a: "n"
        with console.status("Outer spinner...", spinner="dots"):
            out = run_command("python -c \"print('should_not_run')\"")
            self.assertEqual(out, "Command rejected by user.")


class TestConsecutiveAndIndependentRequests(BasePermissionTest):
    """Tests consecutive permission requests to ensure no stale approvals or state leakage."""

    def test_two_consecutive_commands_first_approved_second_denied(self):
        answers = iter(["y", "n"])
        builtins.input = lambda *a: next(answers)

        out1 = run_command("python -c \"print('first_ran')\"")
        self.assertIn("first_ran", out1)

        out2 = run_command("python -c \"print('second_ran')\"")
        self.assertEqual(out2, "Command rejected by user.")

    def test_two_consecutive_commands_first_denied_second_approved(self):
        answers = iter(["n", "y"])
        builtins.input = lambda *a: next(answers)

        out1 = run_command("python -c \"print('first_ran')\"")
        self.assertEqual(out1, "Command rejected by user.")

        out2 = run_command("python -c \"print('second_ran')\"")
        self.assertIn("second_ran", out2)


class TestDistinctPermissionHandlers(BasePermissionTest):
    """Verifies all distinct permission handlers across ARCDVNCLI."""

    def test_handler_run_command_denied_never_executes(self):
        marker_file = Path(self.temp_proj) / "marker_never_created.txt"
        builtins.input = lambda *a: "n"

        cmd = f"python -c \"import pathlib; pathlib.Path('{marker_file.name}').write_text('executed')\""
        out = run_command(cmd)

        self.assertEqual(out, "Command rejected by user.")
        self.assertFalse(marker_file.exists(), "Denied command must NEVER execute on OS!")

    def test_handler_edit_file_approved_and_denied(self):
        target = Path(self.temp_proj) / "sample.py"
        target.write_text("def hello(): return 'old'\n", encoding="utf-8")

        # Denied first
        builtins.input = lambda *a: "n"
        out_deny = edit_file(path="sample.py", old_str="return 'old'", new_str="return 'new'")
        self.assertEqual(out_deny, "Edit rejected by user.")
        self.assertIn("return 'old'", target.read_text(encoding="utf-8"))

        # Approved second
        builtins.input = lambda *a: "y"
        out_ok = edit_file(path="sample.py", old_str="return 'old'", new_str="return 'new'")
        self.assertIn("edited sample.py", out_ok)
        self.assertIn("return 'new'", target.read_text(encoding="utf-8"))

    def test_handler_write_file_overwrite_approved_and_denied(self):
        target = Path(self.temp_proj) / "data.txt"
        target.write_text("ORIGINAL", encoding="utf-8")

        # Denied
        builtins.input = lambda *a: "n"
        out_deny = write_file(path="data.txt", content="NEW_DATA")
        self.assertEqual(out_deny, "Write rejected by user.")
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL")

        # Approved
        builtins.input = lambda *a: "y"
        out_ok = write_file(path="data.txt", content="NEW_DATA")
        self.assertIn("wrote data.txt", out_ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "NEW_DATA")

    def test_handler_cli_uninstall_approved_and_denied(self):
        # Denied
        builtins.input = lambda *a: "n"
        self.assertFalse(cli._uninstall_self())

        # Mock pipx subprocess to prevent actual package removal during tests
        with patch("subprocess.run") as mock_sub:
            builtins.input = lambda *a: "y"
            self.assertTrue(cli._uninstall_self())
            mock_sub.assert_called_once()


class TestAgentResumptionAfterDecision(BasePermissionTest):
    """Verifies that the agent loop resumes cleanly after approvals and denials."""

    def test_agent_resumes_cleanly_after_command_denial(self):
        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c_cmd",
                    "function": {
                        "name": "run_command",
                        "arguments": json.dumps({"command": "python -c \"print('danger')\""})
                    }
                }]
            },
            {
                "role": "assistant",
                "content": "Perintah dibatalkan oleh pengguna. Tidak ada tindakan yang dijalankan."
            }
        ])

        builtins.input = lambda *a: "n"

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent("Jalankan skrip bahaya", history)

        self.assertIn("dibatalkan oleh pengguna", res)
        # Verify tool result recorded in history
        self.assertTrue(len(history) >= 2)

    def test_agent_resumes_cleanly_after_command_approval(self):
        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c_cmd2",
                    "function": {
                        "name": "run_command",
                        "arguments": json.dumps({"command": "python -c \"print('success_output')\""})
                    }
                }]
            },
            {
                "role": "assistant",
                "content": "Perintah sukses dijalankan dan output terverifikasi."
            }
        ])

        builtins.input = lambda *a: "y"

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent("Jalankan skrip aman", history)

        self.assertIn("sukses dijalankan", res)
        self.assertTrue(len(history) >= 2)


if __name__ == "__main__":
    unittest.main()
