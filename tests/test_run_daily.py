"""scripts/run-daily.sh, run as a real process. The notifier and the daily-pulse binary are
injectable (DAILY_PULSE_NOTIFY, DAILY_PULSE_BIN); nothing here touches the network or the real
~/bin/notify."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run-daily.sh"
DATE = "2026-01-15"

STUB = '''#!{python}
import json, os, sys, time
from pathlib import Path
argv = sys.argv[1:]
cmd = argv[0]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\\n")
Path(os.environ["STUB_LOG"] + ".stdin").write_text(os.readlink("/proc/self/fd/0"))
rc = int(os.environ.get("STUB_" + cmd.upper() + "_RC", "0"))
if cmd == "run" and os.environ.get("STUB_HOLD"):
    Path(os.environ["STUB_HOLD"] + ".started").touch()
    for _ in range(200):
        if Path(os.environ["STUB_HOLD"] + ".release").exists():
            break
        time.sleep(0.05)
if cmd == "run" and os.environ.get("STUB_BG"):  # a child that outlives the script, keeping every fd it inherited
    import subprocess
    bg = subprocess.Popen(["sleep", "30"], close_fds=False, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(os.environ["STUB_BG"]).write_text(str(bg.pid))
if cmd == "run" and rc == 0 and not os.environ.get("STUB_NO_RESULT"):
    out = Path(argv[argv.index("--out") + 1])
    out.mkdir(parents=True, exist_ok=True)
    flagged = [n for n in os.environ.get("STUB_FLAGGED", "").split(",") if n]
    (out / ("run-" + argv[argv.index("--date") + 1] + ".json")).write_text(json.dumps({"flagged": flagged}))
sys.exit(rc)
'''

NOTIFIER = '''#!/bin/sh
printf '%s\\n' "$*" >> "$NOTIFY_LOG"
exit "${NOTIFY_RC:-0}"
'''

REAL = "#!{python}\nimport sys\nfrom daily_pulse.cli import main\nsys.exit(main())\n"


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.data = tmp / "data"
        self.site = tmp / "site"
        self.remote = tmp / "remote.git"
        self.log = tmp / "stub.log"
        self.notify_log = tmp / "notify.log"
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.stub = self._script("stub", STUB)
        self.real = self._script("real", REAL)
        self.notifier = self._script("notify", NOTIFIER)
        self.config = tmp / "config.toml"
        self.config.write_text(f'[paths]\ndata_dir = "{self.data}"\n\n[sources.x]\nenabled = true\n')
        self.vars = {}

    def _script(self, name, body):
        path = self.tmp / name
        path.write_text(body.replace("{python}", sys.executable))
        path.chmod(0o755)
        return path

    def environ(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("DAILY_PULSE_", "STUB_", "IG_"))}
        env.update(
            DAILY_PULSE_BIN=str(self.stub), DAILY_PULSE_CONFIG=str(self.config),
            DAILY_PULSE_DATA_DIR=str(self.data), DAILY_PULSE_SITE=str(self.site),
            DAILY_PULSE_REMOTE=str(self.remote), DAILY_PULSE_NOTIFY=str(self.notifier),
            DAILY_PULSE_DATE=DATE, STUB_LOG=str(self.log), NOTIFY_LOG=str(self.notify_log),
            GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null",
            PYTHONPATH=str(ROOT), HOME=str(self.tmp / "home"))
        env.update(self.vars)
        env.update(extra)
        return env

    def popen(self, **extra):
        return subprocess.Popen(["bash", str(SCRIPT)], env=self.environ(**extra), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def run(self, **extra):
        return subprocess.run(["bash", str(SCRIPT)], env=self.environ(**extra), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        return [json.loads(line)[0] for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def notifications(self):
        return self.notify_log.read_text().splitlines() if self.notify_log.exists() else []


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_a_clean_run_runs_digest_and_publish_in_order_and_stays_quiet(env):
    done = env.run()
    assert done.returncode == 0, done.stdout + done.stderr
    assert env.calls() == ["run", "digest", "publish"]
    assert env.notifications() == []


def test_a_failed_run_notifies_and_exits_non_zero_without_publishing(env):
    done = env.run(STUB_RUN_RC="1")
    assert done.returncode != 0
    assert env.calls() == ["run"]
    [message] = env.notifications()
    assert "run failed" in message and DATE in message


@pytest.mark.parametrize("step", ["DIGEST", "PUBLISH"])
def test_a_failed_digest_or_publish_notifies_and_exits_non_zero(env, step):
    done = env.run(**{f"STUB_{step}_RC": "1"})
    assert done.returncode != 0
    [message] = env.notifications()
    assert step.lower() in message


def test_modules_failing_twice_in_a_row_are_notified_but_the_site_is_still_published(env):
    done = env.run(STUB_FLAGGED="youtube,github")
    assert done.returncode == 0
    assert env.calls() == ["run", "digest", "publish"]
    [message] = env.notifications()
    assert "youtube" in message and "github" in message


def test_a_run_that_leaves_no_result_is_a_failure(env):
    done = env.run(STUB_NO_RESULT="1")
    assert done.returncode != 0
    assert len(env.notifications()) == 1


def test_a_failing_notifier_does_not_change_the_exit_code_of_a_clean_run_or_hide_a_failure(env):
    assert env.run(NOTIFY_RC="1", STUB_FLAGGED="x").returncode == 0
    assert env.run(NOTIFY_RC="1", STUB_RUN_RC="3").returncode != 0


def test_a_missing_notifier_is_logged_not_fatal(env):
    done = env.run(DAILY_PULSE_NOTIFY=str(env.tmp / "absent"), STUB_RUN_RC="1")
    assert done.returncode != 0
    assert "notify" in done.stdout


def test_the_wrapper_passes_config_out_site_and_date_to_every_step(env):
    env.run()
    steps = {json.loads(l)[0]: json.loads(l) for l in env.log.read_text().splitlines()}
    for argv in steps.values():
        assert argv[argv.index("--config") + 1] == str(env.config)
        assert argv[argv.index("--out") + 1] == str(env.data / "out")
        assert argv[argv.index("--date") + 1] == DATE
    pub = steps["publish"]
    assert pub[pub.index("--site") + 1] == str(env.site)
    assert pub[pub.index("--remote") + 1] == str(env.remote)


def test_a_second_invocation_while_one_runs_is_blocked_by_the_lock(env, tmp_path):
    hold = str(tmp_path / "hold")
    first = env.popen(STUB_HOLD=hold)
    try:
        for _ in range(200):
            if Path(hold + ".started").exists():
                break
            time.sleep(0.05)
        assert Path(hold + ".started").exists(), "first run never started"
        second = env.run()
        assert second.returncode == 0
        assert "already running" in second.stdout
        assert env.calls() == ["run"]  # the second invocation ran nothing
        assert env.notifications() == []
    finally:
        Path(hold + ".release").touch()
        out, _ = first.communicate(timeout=60)
    assert first.returncode == 0, out
    assert env.calls() == ["run", "digest", "publish"]
    assert env.run().returncode == 0  # the lock is released afterwards


def test_it_never_reads_the_terminal_or_prompts_for_git_credentials(env):
    live = subprocess.Popen(["bash", str(SCRIPT)], env=env.environ(), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # stdin left open
    out = live.stdout.read()
    live.wait(timeout=60)
    assert live.returncode == 0, out
    assert Path(str(env.log) + ".stdin").read_text() == "/dev/null"
    live.stdin.close()


def test_end_to_end_with_the_real_cli_publishes_to_the_bare_remote(env):
    done = env.run(DAILY_PULSE_BIN=str(env.real))
    assert done.returncode == 0, done.stdout + done.stderr
    files = subprocess.run(["git", "--git-dir", str(env.remote), "ls-tree", "-r", "--name-only", "gh-pages"],
                           capture_output=True, text=True, check=True).stdout.split()
    assert "index.html" in files and "archive/index.html" in files
    assert not any(f.startswith(("state", "transcripts", "logs")) for f in files)
    before = subprocess.run(["git", "--git-dir", str(env.remote), "rev-parse", "gh-pages"],
                            capture_output=True, text=True, check=True).stdout
    assert env.run(DAILY_PULSE_BIN=str(env.real)).returncode == 0
    after = subprocess.run(["git", "--git-dir", str(env.remote), "rev-parse", "gh-pages"],
                           capture_output=True, text=True, check=True).stdout
    assert before == after  # a rerun with nothing new publishes no new commit
    assert env.notifications() == []


def test_flagged_modules_are_notified_even_when_publish_then_fails(env):
    done = env.run(STUB_FLAGGED="youtube", STUB_PUBLISH_RC="1")
    assert done.returncode != 0
    messages = env.notifications()
    assert len(messages) == 2
    assert "youtube" in messages[0] and "publish" in messages[1]


def test_a_child_that_outlives_the_script_does_not_keep_the_lock_held(env, tmp_path):
    pidfile = tmp_path / "bg.pid"
    try:
        assert env.run(STUB_BG=str(pidfile)).returncode == 0
        assert pidfile.exists() and Path(f"/proc/{pidfile.read_text()}").exists()  # the child is still alive
        again = env.run()
        assert "already running" not in again.stdout
        assert env.calls().count("run") == 2
    finally:
        if pidfile.exists():
            subprocess.run(["kill", pidfile.read_text()], check=False)


def test_an_already_running_lock_held_for_hours_is_notified(env):
    env.data.mkdir(parents=True)
    (env.data / "cron.started").write_text(str(int(time.time()) - 10 * 3600))
    holder = subprocess.Popen(["flock", str(env.data / "cron.lock"), "sleep", "30"])
    try:
        time.sleep(0.5)
        done = env.run()
        assert done.returncode == 0 and "already running" in done.stdout
        [message] = env.notifications()
        assert "already running" in message and "10" in message  # says for how many hours
        assert env.calls() == []
    finally:
        holder.kill()
        holder.wait()
