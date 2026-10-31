"""Tests for deploy/hem-funnel-watchdog.sh using fake dig/curl/systemctl shims."""
from __future__ import annotations

import os
import time
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hem-funnel-watchdog.sh"

DIG = """#!/usr/bin/env bash
echo "dig $*" >> "$SHIM_DIR/calls.log"
for a in "$@"; do
  case "$a" in @*) f="$SHIM_DIR/dig_${a#@}"; [ -f "$f" ] && cat "$f";; esac
done
exit 0
"""

CURL = """#!/usr/bin/env bash
if printf '%s\\n' "$@" | grep -q 'api.telegram.org'; then
  echo "telegram $*" >> "$SHIM_DIR/calls.log"
  exit 0
fi
ip=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--resolve" ]; then ip="${a##*:}"; fi
  prev="$a"
done
echo "probe $ip" >> "$SHIM_DIR/calls.log"
rc=0
[ -f "$SHIM_DIR/probe_$ip" ] && rc="$(cat "$SHIM_DIR/probe_$ip")"
if [ "$rc" = "0" ]; then printf '503'; exit 0; fi
printf '000'
exit "$rc"
"""

SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$SHIM_DIR/calls.log"
case "$*" in
  *"show -p ActiveEnterTimestamp"*) [ -f "$SHIM_DIR/active_enter" ] && cat "$SHIM_DIR/active_enter"; exit 0;;
  "restart tailscaled") [ -f "$SHIM_DIR/restart_rc" ] && exit "$(cat "$SHIM_DIR/restart_rc")";;
esac
exit 0
"""


class Env:
    def __init__(self, tmp: Path) -> None:
        self.shim = tmp / "shims"
        self.shim.mkdir()
        self.state = tmp / "state"
        self.envfile = tmp / "hem.env"
        self.envfile.write_text('TELEGRAM_BOT_TOKEN="123:ABC"\nTELEGRAM_CHAT_ID=42\n')
        for name, body in (("dig", DIG), ("curl", CURL), ("systemctl", SYSTEMCTL)):
            p = self.shim / name
            p.write_text(body)
            p.chmod(0o755)
        (self.shim / "dig_1.1.1.1").write_text("10.0.0.1\n")
        (self.shim / "dig_9.9.9.9").write_text("10.0.0.1\n")

    def set_probe(self, ip: str, rc: int) -> None:
        (self.shim / f"probe_{ip}").write_text(str(rc))

    def run(self, state: bool = True, **extra: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{self.shim}:{os.environ['PATH']}",
            "SHIM_DIR": str(self.shim),
            "HEM_ENV_FILE": str(self.envfile),
            "HOME": str(self.shim),
            "TMPDIR": str(self.shim),
        }
        if state:
            env["FUNNEL_STATE_DIR"] = str(self.state)
        env.update(extra)
        res = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
        )
        assert res.returncode == 0, res.stderr
        return res

    def calls(self, prefix: str) -> list[str]:
        log = self.shim / "calls.log"
        if not log.exists():
            return []
        return [x for x in log.read_text().splitlines() if x.startswith(prefix)]

    def restarts(self) -> list[str]:
        return self.calls("systemctl restart tailscaled")

    def telegrams(self) -> list[str]:
        return self.calls("telegram")

    def fails(self) -> int:
        return int((self.state / "fails").read_text().strip())


@pytest.fixture
def w(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_alive_no_action(w: Env) -> None:
    w.run()
    assert not w.restarts() and not w.telegrams()
    assert w.fails() == 0


def test_single_failure_no_action(w: Env) -> None:
    w.set_probe("10.0.0.1", 28)
    w.run()
    assert w.fails() == 1
    assert not w.restarts() and not w.telegrams()


def test_second_failure_restarts_once(w: Env) -> None:
    w.set_probe("10.0.0.1", 28)
    w.run()
    w.run()
    assert len(w.restarts()) == 1
    assert len(w.telegrams()) == 1
    assert "reiniciando" in w.telegrams()[0]
    assert "bot123:ABC" in w.telegrams()[0]
    assert "HEM host:" in w.telegrams()[0]


def test_cooldown_blocks_second_restart_and_alerts_once(w: Env) -> None:
    w.set_probe("10.0.0.1", 28)
    w.run()
    w.run()
    w.run()  # 3rd failure, inside cooldown
    w.run()  # 4th, must not repeat the message
    assert len(w.restarts()) == 1
    tg = w.telegrams()
    assert len(tg) == 2
    assert "reiniciando" in tg[0]
    assert "não resolveu" in tg[1]


def test_recovery_message_and_state_reset(w: Env) -> None:
    w.set_probe("10.0.0.1", 28)
    w.run()
    w.run()
    w.set_probe("10.0.0.1", 0)
    w.run()
    tg = w.telegrams()
    assert len(tg) == 2 and "voltou" in tg[1]
    assert w.fails() == 0
    assert not (w.state / "alerted").exists()
    w.run()  # no repeat
    assert len(w.telegrams()) == 2


def test_all_resolvers_fail_counts_nothing(w: Env) -> None:
    (w.shim / "dig_1.1.1.1").unlink()
    (w.shim / "dig_9.9.9.9").unlink()
    w.set_probe("10.0.0.1", 28)
    w.run()
    w.run()
    assert not w.calls("probe")
    assert not w.restarts() and not w.telegrams()
    assert not (w.state / "fails").exists()


def test_second_resolver_used_when_first_empty(w: Env) -> None:
    (w.shim / "dig_1.1.1.1").write_text("")
    (w.shim / "dig_9.9.9.9").write_text("10.0.0.7\n")
    w.run()
    assert w.calls("probe 10.0.0.7")


def test_second_ingress_ip_rescues(w: Env) -> None:
    (w.shim / "dig_1.1.1.1").write_text("10.0.0.1\n10.0.0.2\n")
    w.set_probe("10.0.0.1", 28)
    w.set_probe("10.0.0.2", 0)
    w.run()
    w.run()
    assert len(w.calls("probe")) == 4
    assert w.fails() == 0
    assert not w.restarts() and not w.telegrams()


def test_dry_run_hits_nothing(w: Env) -> None:
    w.set_probe("10.0.0.1", 28)
    w.run(FUNNEL_DRY_RUN="1")
    res = w.run(FUNNEL_DRY_RUN="1")
    assert not w.restarts() and not w.telegrams()
    assert "DRY-RUN" in res.stderr


def test_recent_unit_start_blocks_restart(w: Env) -> None:
    ts = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() - 60))
    (w.shim / "active_enter").write_text(ts + "\n")
    w.set_probe("10.0.0.1", 28)
    w.run()
    w.run()
    assert not w.restarts()
    tg = w.telegrams()
    assert len(tg) == 1
    assert "(re)iniciado" in tg[0] and "não resolveu" not in tg[0]


def test_zero_cooldown_restarts_again_and_stops_unresolved_noise(w: Env) -> None:
    w.set_probe("10.0.0.1", 28)
    w.run(FUNNEL_RESTART_COOLDOWN_S="0")
    w.run(FUNNEL_RESTART_COOLDOWN_S="0")
    w.run(FUNNEL_RESTART_COOLDOWN_S="0")
    assert len(w.restarts()) == 2
    tg = w.telegrams()
    assert len(tg) == 2
    assert "nº 1" in tg[0] and "nº 2" in tg[1]
    assert not any("não resolveu" in t for t in tg)


def test_cooldown_doubles_per_restart(w: Env) -> None:
    w.set_probe("10.0.0.1", 28)
    w.run(FUNNEL_RESTART_COOLDOWN_S="100")
    w.run(FUNNEL_RESTART_COOLDOWN_S="100")
    assert len(w.restarts()) == 1
    # after restart 1 the gap is the base 100 s; after restart 2 it doubles to 200 s
    (w.state / "last_restart_epoch").write_text(str(int(time.time()) - 150))
    w.run(FUNNEL_RESTART_COOLDOWN_S="100")
    assert len(w.restarts()) == 2
    (w.state / "last_restart_epoch").write_text(str(int(time.time()) - 150))
    w.run(FUNNEL_RESTART_COOLDOWN_S="100")
    assert len(w.restarts()) == 2
    (w.state / "last_restart_epoch").write_text(str(int(time.time()) - 250))
    w.run(FUNNEL_RESTART_COOLDOWN_S="100")
    assert len(w.restarts()) == 3


def test_restart_failure_reported(w: Env) -> None:
    (w.shim / "restart_rc").write_text("5")
    w.set_probe("10.0.0.1", 28)
    w.run()
    w.run()
    tg = w.telegrams()
    assert len(tg) == 2 and "FALHOU (rc=5)" in tg[1]
    assert (w.state / "last_restart_epoch").exists()


def test_corrupt_state_is_tolerated(w: Env) -> None:
    w.state.mkdir()
    (w.state / "fails").write_text("garbage\n")
    (w.state / "last_restart_epoch").write_text("1x\n")
    w.set_probe("10.0.0.1", 28)
    w.run()
    assert w.fails() == 1


def test_missing_token_no_telegram(w: Env) -> None:
    w.envfile.write_text("OTHER=1\n")
    w.set_probe("10.0.0.1", 28)
    w.run()
    w.run()
    assert len(w.restarts()) == 1
    assert not w.telegrams()


def test_env_parsing_variants(w: Env) -> None:
    w.envfile.write_text("export TELEGRAM_BOT_TOKEN='1:x'\nTELEGRAM_CHAT_ID=42 # me\n")
    w.set_probe("10.0.0.1", 28)
    w.run()
    w.run()
    tg = w.telegrams()
    assert len(tg) == 1
    assert "bot1:x/sendMessage" in tg[0]
    assert "chat_id=42" in tg[0] and "#" not in tg[0].split("chat_id=")[1].split()[0]


def test_dry_run_without_state_dir_uses_throwaway(w: Env) -> None:
    w.set_probe("10.0.0.1", 28)
    res = w.run(state=False, FUNNEL_DRY_RUN="1")
    assert "throwaway state dir" in res.stderr
    assert not w.state.exists()


def test_script_does_not_source_defaults_file() -> None:
    text = SCRIPT.read_text()
    code = [x for x in text.splitlines() if not x.lstrip().startswith("#")]
    assert not any("/etc/default" in x for x in code)
