"""Outside alarm checker: reads a heartbeat value and sends at most ONE push when the beat goes quiet (and one "back" line).
Stdlib only, Python 3.11 and 3.12. Zero AI. It runs on a GitHub Linux runner.

  outside_beat.py check   Reads env HOUSE_BEAT ("<ISO time with offset>|ha=up" or "|ha=down") and NTFY_TOPIC (a repository secret: never
                          printed, never logged, never in a URL; it goes only in the JSON body of the POST). State comes from BEAT_STATE
                          (default state/state.json on the house-beat-state branch). decide() is pure: no clock, no network, no files.
Rules: beat age <= 60 min = fine; older = ONE "late" alert per outage; fresh beat but ha=down for over 60 min = ONE "ha_down";
flapping = at most 4 messages in 24 h; unreadable beat = one calm line at most once a day; a failed send is retried (3 tries per outage).
An empty topic sends nothing and marks nothing."""

import copy
import datetime
import json
import os
import shutil
import subprocess
import sys
import urllib.request

VAR = "HOUSE_BEAT"
LATE_S = 3600          # beat older than this = the PC went quiet (the workflow cron is hourly: alert lands 60-120 min after the last beat)
FUTURE_S = 600         # a beat up to 10 min in the future (clock skew) still counts as fresh
CAP_N = 4              # at most this many messages ...
CAP_S = 86400          # ... in this many seconds
UNREADABLE_S = 86400   # "cannot read the beat" at most once a day
MAX_TRIES = 3          # sends tried per outage before it is given up


# ---------- pure ----------
def _dt(s):
    try:
        d = datetime.datetime.fromisoformat(str(s).strip())
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else None


def parse_beat(text):
    """(datetime with offset, "up"|"down") or None for anything missing, empty, naive or garbled."""
    if not isinstance(text, str):
        return None
    parts = text.strip().strip("'\"").split("|")
    if len(parts) != 2:
        return None
    when = _dt(parts[0])
    ha = parts[1].strip().lower()
    if when is None or ha not in ("ha=up", "ha=down"):
        return None
    return when, ha[3:]


def _norm(state):
    st = copy.deepcopy(state) if isinstance(state, dict) else {}
    st.setdefault("open", "")          # key of the outage already told and not yet closed ("late:<beat>" / "ha_down:<since>")
    st.setdefault("kind", "")          # late | ha_down
    st.setdefault("down_since", "")    # beat time of the first ha=down beat of the current run of down beats
    st.setdefault("sent", [])          # ISO times of messages sent (24 h cap)
    st.setdefault("fails", {})         # alert key -> failed sends
    st.setdefault("unreadable_at", "")
    st.setdefault("last_key", "")
    if not isinstance(st["sent"], list) or not isinstance(st["fails"], dict):
        st["sent"], st["fails"] = [], {}
    return st


def _approx(seconds):
    m = int(round(seconds / 600.0)) * 10           # round to 10 min: "about 70 min", never "4213 s"
    return f"about {m} min" if m < 120 else f"about {int(round(m / 60.0))} h"


def message(alert, age_s=0):
    """Short, simple, no tech words, at most 4 lines under the title. Red only for 'late'. No names, no places."""
    if alert == "late":
        return ("\U0001F6DF Failsafe: \U0001F534 House PC went quiet\n"
                f"\u2753 Why: no sign of life for {_approx(age_s)}\n"
                "\U0001F449 Do: nothing, the wall switches still work\n"
                "\u23ED\uFE0F Next: one line when it is back")
    if alert == "ha_down":
        return ("\U0001F6DF Failsafe: \U0001F7E1 House system is down\n"
                "\u2753 Why: the PC is alive but the house system does not answer\n"
                "\U0001F449 Do: nothing, the wall switches still work\n"
                "\u23ED\uFE0F Next: the PC tries a fix and tells you")
    if alert == "back":
        return ("\U0001F6DF Failsafe: \U0001F7E2 House is back\n"
                "\u2753 Why: the PC checked in again\n"
                "\U0001F449 Do: nothing")
    return ("\U0001F6DF Failsafe: \U0001F535 Cannot read the alive note\n"
            "\u2753 Why: it is missing or garbled\n"
            "\U0001F449 Do: nothing, the wall switches still work\n"
            "\u23ED\uFE0F Next: I look again later")


def _emit(st, now, alert, key, age_s=0, mark=None):
    """Apply the give-up rule and the 24 h cap. Returns (alert|None, text, state). The state already says 'told'; settle() undoes
    that when the send fails.
    `mark` = the open-alert marker (open / kind / down_since) to record. It is applied ONLY when an alert is really returned: a capped
    or given-up alert must leave the marker alone, or the outage is 'told' but never sent and stays silent for good (review 13/11).
    Cap rule: a 'late' alert (the PC is really dead) is EXEMPT from the 24 h cap, so a real outage is always told once per outage
    (the outage is named by the last beat, so it cannot repeat); 'back', 'ha_down' and 'unreadable' stay capped. A capped 'back'
    keeps the outage open, so it is retried when the window frees. A late alert still counts in the 24 h list for the others."""
    if st["fails"].get(key, 0) >= MAX_TRIES:
        return None, "", st                      # given up on this one
    recent = [t for t in st["sent"] if _dt(t) is not None and 0 <= (now - _dt(t)).total_seconds() < CAP_S]
    st["sent"] = recent
    if alert != "late" and len(recent) >= CAP_N:
        return None, "", st                      # flapping: silent until the window frees (marker untouched, so it is retried)
    st["sent"] = recent + [now.isoformat()]
    st["last_key"] = key
    for k, v in (mark or {}).items():
        st[k] = v
    if alert == "unreadable":
        st["unreadable_at"] = now.isoformat()
    return alert, message(alert, age_s), st


def decide(beat_text, now, state, late_s=LATE_S):
    """(alert, text, new_state); alert is None | late | ha_down | back | unreadable. Pure. `now` must carry an offset."""
    st = _norm(state)
    parsed = parse_beat(beat_text)
    if parsed is None or (parsed[0] - now).total_seconds() > FUTURE_S:
        last = _dt(st["unreadable_at"])
        if last is not None and 0 <= (now - last).total_seconds() < UNREADABLE_S:
            return None, "", st
        return _emit(st, now, "unreadable", "unreadable:" + now.date().isoformat())
    beat, ha = parsed
    age = (now - beat).total_seconds()
    if age > late_s:                             # the PC went quiet
        key = "late:" + beat.isoformat()
        if st["open"] == key:
            return None, "", st                  # already told for this outage (the outage is named by the last beat)
        return _emit(st, now, "late", key, age, {"open": key, "kind": "late", "down_since": ""})
    # a fresh beat
    if st["open"] and (ha == "up" or st["kind"] == "late"):
        key = "back:" + st["open"]
        return _emit(st, now, "back", key, 0, {"open": "", "kind": "", "down_since": beat.isoformat() if ha == "down" else ""})
    if ha == "up":
        st["down_since"] = ""
        return None, "", st
    since = _dt(st["down_since"]) or beat        # ha=down
    st["down_since"] = since.isoformat()
    if st["open"] or (beat - since).total_seconds() <= late_s:
        return None, "", st
    key = "ha_down:" + since.isoformat()
    return _emit(st, now, "ha_down", key, 0, {"open": key, "kind": "ha_down"})


def settle(old_state, new_state, alert, sent_ok):
    """After the send: success (or nothing to send) keeps the new state; a failed send goes back to the OLD state with one more
    failed try on that alert's key, so the next run tries again (MAX_TRIES per outage, then decide() gives up quietly)."""
    if alert is None or sent_ok:
        return new_state
    st = _norm(old_state)
    key = new_state.get("last_key", "")
    st["fails"][key] = st["fails"].get(key, 0) + 1
    st["fails"] = dict(list(st["fails"].items())[-20:])
    return st


def split_message(text):
    title, _, body = text.partition("\n")
    return title, body


# ---------- check (runner side) ----------
def read_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
        reason = "not an object"
    except FileNotFoundError:
        return {}                                # first run: no state yet, nothing to say
    except (OSError, ValueError) as ex:
        reason = type(ex).__name__
    # a corrupt state forgets the open alert and the 24 h list: say so once (no secret in the line); the cap stays bounded
    print(f"state file unreadable ({reason}): starting from an empty state", file=sys.stderr)
    return {}


def write_state(path, st):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(st, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def ntfy_send(topic, text, alert, url="https://ntfy.sh"):
    """POST JSON: the topic is only in the body, never in the URL, never printed."""
    title, body = split_message(text)
    prio = {"late": 4, "ha_down": 3}.get(alert, 2)
    data = json.dumps({"topic": topic, "title": title, "message": body, "priority": prio, "tags": ["house"]}).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/"), data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception:
        return False


def run_check(env, now, state_path, send=ntfy_send):
    """One checker run. Returns a short line (never the topic, never the message). Empty topic = send nothing, mark nothing."""
    topic = (env.get("NTFY_TOPIC") or "").strip()
    if not topic:
        return "no topic set: sent nothing, state untouched"
    old = read_state(state_path)
    alert, text, new = decide(env.get(VAR, ""), now, old)
    ok = True
    if alert:
        ok = bool(send(topic, text, alert))
    final = settle(old, new, alert, ok)
    if final != old:
        write_state(state_path, final)
    return f"alert={alert or 'none'}" + (f" sent={'yes' if ok else 'FAILED'}" if alert else "")




def main(argv):
    if len(argv) >= 2 and argv[1] == "check":
        now = datetime.datetime.now(datetime.timezone.utc)
        print(run_check(os.environ, now, os.environ.get("BEAT_STATE", "state/state.json")))
        return 0
    print(__doc__.split("\n\n")[0])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
