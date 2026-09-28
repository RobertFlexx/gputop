from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


@unittest.skipUnless(os.name == "posix", "the shell installer targets Unix")
class ShellInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.script = self.root / "install.sh"
        shutil.copy2(INSTALLER, self.script)
        self.env = os.environ.copy()
        self.env.update(
            HOME=str(self.home),
            XDG_DATA_HOME=str(self.home / ".local" / "share"),
            PATH=f"{self.root / 'commands'}:{os.environ.get('PATH', '')}",
        )
        for key in (
            "GPUTOP_DIR",
            "GPUTOP_BIN_DIR",
            "GPUTOP_METHOD",
            "GPUTOP_PYTHON",
            "GPUTOP_REF",
            "GPUTOP_REPO",
            "GPUTOP_SOURCE",
            "GPUTOP_RC_FILE",
        ):
            self.env.pop(key, None)

    def run_installer(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(self.script), *args],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def managed_layout(self) -> tuple[Path, Path]:
        data = self.root / "custom install"
        commands = self.root / "commands"
        venv_bin = data / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        commands.mkdir(exist_ok=True)
        python = venv_bin / "python"
        python.write_text('#!/bin/sh\nprintf "0.2.0\\n"\n')
        python.chmod(0o755)
        app = venv_bin / "gputop"
        app.write_text("#!/bin/sh\nexit 0\n")
        app.chmod(0o755)
        (commands / "gputop").symlink_to(app)
        (data / "install.state").write_text(
            "version=1\n"
            f"data_dir={data}\n"
            f"bin_dir={commands}\n"
            f"launcher={commands / 'gputop'}\n"
            "method=pip\n"
            "ref=release-test\n"
            "repo=example/gputop\n"
            "source=\n"
        )
        return data, commands

    def test_update_discovers_custom_install_and_keeps_source_settings(self) -> None:
        data, commands = self.managed_layout()
        result = self.run_installer("--update", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(data), result.stdout)
        self.assertIn(f"example/gputop at release-test", result.stdout)
        self.assertIn(str(commands / "gputop"), result.stdout)
        self.assertTrue((data / "install.state").exists())

    def test_explicit_ref_overrides_saved_local_source(self) -> None:
        data, _ = self.managed_layout()
        source = self.root / "old-source"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            "[project]\nname='gputop'\nversion='0.2.0'\n"
        )
        state = data / "install.state"
        state.write_text(state.read_text().replace("source=\n", f"source={source}\n"))
        result = self.run_installer("--update", "--dry-run", "--ref", "new-release")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("example/gputop at new-release", result.stdout)
        self.assertNotIn(str(source), result.stdout)

    def test_update_discovers_custom_shim_launcher(self) -> None:
        data, commands = self.managed_layout()
        launcher = commands / "gputop"
        launcher.unlink()
        launcher.write_text(f'#!/bin/sh\nexec "{data}/venv/bin/gputop" "$@"\n')
        launcher.chmod(0o755)
        result = self.run_installer("--update", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(data), result.stdout)

    def test_active_command_wins_over_other_managed_install(self) -> None:
        data, _ = self.managed_layout()
        default = self.home / ".local" / "share" / "gputop"
        default.mkdir(parents=True)
        (default / "install.state").write_text("version=1\n")
        result = self.run_installer("--update", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(data), result.stdout)
        self.assertNotIn(f"update gputop in {default}", result.stdout)

    def test_uv_dry_run_plans_managed_python_without_bootstrapping(self) -> None:
        limited_path = self.root / "limited-bin"
        limited_path.mkdir()
        (limited_path / "basename").symlink_to("/usr/bin/basename")
        self.env["PATH"] = str(limited_path)
        result = self.run_installer("--dry-run", "--method", "uv")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uv-managed Python", result.stdout)
        self.assertFalse((self.home / ".local" / "share" / "gputop").exists())

    def test_update_detects_user_install_with_env_shebang(self) -> None:
        commands = self.root / "commands"
        commands.mkdir()
        python = commands / "fakepython"
        python.write_text(
            "#!/bin/sh\n"
            'case "$2" in\n'
            f"  *locate_file*) printf '%s\\n' '{self.home}/.local/lib/site-packages' ;;\n"
            "  *) printf '0.2.0\\n' ;;\n"
            "esac\n"
        )
        python.chmod(0o755)
        launcher = commands / "gputop"
        launcher.write_text("#!/usr/bin/env fakepython\n")
        launcher.chmod(0o755)
        result = self.run_installer("--update", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("update the existing gputop", result.stdout)
        self.assertIn(str(launcher), result.stdout)
        self.assertIn(str(python), result.stdout)

    def test_uninstall_leaves_unrelated_launcher_and_unmanaged_data(self) -> None:
        data, commands = self.managed_layout()
        launcher = commands / "gputop"
        launcher.unlink()
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        self.env["GPUTOP_DIR"] = str(data)
        self.env["GPUTOP_BIN_DIR"] = str(commands)
        result = self.run_installer("--uninstall", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(launcher.exists())
        self.assertFalse(data.exists())

        unmanaged = self.home / ".local" / "share" / "gputop"
        unmanaged.mkdir(parents=True)
        (unmanaged / "personal.txt").write_text("keep")
        self.env.pop("GPUTOP_DIR")
        self.env.pop("GPUTOP_BIN_DIR")
        result = self.run_installer("--uninstall", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((unmanaged / "personal.txt").read_text(), "keep")

    def test_piped_script_reads_menu_choice_from_terminal(self) -> None:
        import pty

        data, _ = self.managed_layout()
        self.env["NO_COLOR"] = "1"
        source = self.root / "source"
        source.mkdir()
        (source / "pyproject.toml").write_text(
            "[project]\nname='gputop'\nversion='0.2.0'\n"
        )
        state = data / "install.state"
        state.write_text(state.read_text().replace("source=\n", f"source={source}\n"))

        read_fd, write_fd = os.pipe()
        pid, master_fd = pty.fork()
        if pid == 0:
            os.dup2(read_fd, 0)
            os.close(read_fd)
            os.close(write_fd)
            os.execve("/bin/sh", ["sh", "-s"], self.env)

        os.close(read_fd)
        os.write(write_fd, self.script.read_bytes())
        os.close(write_fd)
        output = bytearray()
        answered = False
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                readable, _, _ = select.select([master_fd], [], [], 0.2)
                if not readable:
                    continue
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                if b"Choice [1]:" in output and not answered:
                    os.write(master_fd, b"9\n3\n")
                    answered = True
                if b"left the existing installation unchanged" in output:
                    break
            self.assertTrue(answered, output.decode(errors="replace"))
            self.assertIn(b"Choose a number from 1 to 3.", output)
            self.assertIn(b"left the existing installation unchanged", output)
        finally:
            if not answered:
                os.kill(pid, 9)
            os.close(master_fd)
            os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
