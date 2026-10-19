"""Unprivileged launcher tests: neither systemd nor the vendor GUI is started."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wukong-launcher-")
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.log = self.root / "events"
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime),
                        MOCK_ROOT=str(self.root),
                        PATH=f"{self.bin}:{os.environ['PATH']}")
        self.script("systemctl", '''#!/bin/bash
case "$2" in
  start)
    echo service-start >> "$MOCK_ROOT/events"
    [[ ${MOCK_START_FAIL:-0} == 1 ]] && exit 1
    ;;
  stop) echo service-stop >> "$MOCK_ROOT/events" ;;
  *) exit 2 ;;
esac
exit 0
''')
        self.script("notify-send", "#!/bin/sh\nexit 0\n")
        self.client = self.script("client", '''#!/bin/bash
exec 8>"$MOCK_ROOT/client.lock"
if ! flock -n 8; then
  printf 'forward:%s\\n' "$*" >> "$MOCK_ROOT/events"
  exit 0
fi
echo gui-start >> "$MOCK_ROOT/events"
trap 'exit 0' TERM
while [[ ! -e "$MOCK_ROOT/exit-gui" ]]; do sleep 0.05; done
exit "${MOCK_GUI_STATUS:-0}"
''')
        source = (Path(__file__).parent / "wukong-auth-launcher").read_text()
        source = source.replace(
            "_client=/opt/apps/cn.tiger-sec.tsinclient/files/lib/tsinclient/TSINClient",
            f"_client={shlex.quote(str(self.client))}",
        )
        self.launcher = self.script("launcher", source)
        self.processes = []

    def script(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def events(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def launch(self, *args):
        process = subprocess.Popen([str(self.launcher), *args], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        return process

    def await_event(self, event):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if event in self.events():
                return
            time.sleep(0.02)
        self.fail(f"Missing {event}: {self.events()}")

    def tearDown(self):
        (self.root / "exit-gui").touch()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        self.temp.cleanup()

    def test_start_then_stop_when_gui_exits(self):
        process = self.launch()
        self.await_event("gui-start")
        self.assertEqual(self.events(), ["service-start", "gui-start"])
        (self.root / "exit-gui").touch()
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(self.events()[-1], "service-stop")

    def test_failed_start_does_not_launch_gui(self):
        self.env["MOCK_START_FAIL"] = "1"
        self.assertNotEqual(self.launch().wait(timeout=10), 0)
        self.assertNotIn("gui-start", self.events())

    def test_duplicate_uri_does_not_stop_daemon(self):
        first = self.launch()
        self.await_event("gui-start")
        second = self.launch("tigersec://example")
        self.assertEqual(second.wait(timeout=10), 0)
        self.assertIn("forward:tigersec://example", self.events())
        self.assertNotIn("service-stop", self.events())
        self.assertIsNone(first.poll())
        (self.root / "exit-gui").touch()
        self.assertEqual(first.wait(timeout=10), 0)
        self.assertEqual(self.events().count("service-stop"), 1)

    def test_signal_and_crash_cleanup(self):
        process = self.launch()
        self.await_event("gui-start")
        process.terminate()
        self.assertEqual(process.wait(timeout=10), 143)
        self.assertEqual(self.events()[-1], "service-stop")

    def test_gui_failure_preserves_exit_status(self):
        self.env["MOCK_GUI_STATUS"] = "17"
        process = self.launch()
        self.await_event("gui-start")
        (self.root / "exit-gui").touch()
        self.assertEqual(process.wait(timeout=10), 17)
        self.assertEqual(self.events()[-1], "service-stop")


if __name__ == "__main__":
    unittest.main()
