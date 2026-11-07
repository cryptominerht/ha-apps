#!/usr/bin/env python3
"""
TV Kid Lock - Samsung Tizen TV + Home Assistant + UniFi

After N minutes of real watch time the TV is locked:
  1. playback is paused (KEY_PAUSE)
  2. the TV's internet is blocked via Home Assistant's UniFi integration
  3. a quiz page opens in the TV's own browser (answered with the remote)
While locked, streaming apps that come to the foreground are closed and the
quiz is brought back. A correct answer (or a parent override in HA) unlocks.

Usage:
  python kidlock.py                  # run the service
  python kidlock.py --pair           # first-time pairing (press Allow on TV)
  python kidlock.py --list-apps      # print installed app IDs
  python kidlock.py --test-block     # block TV internet for 20 s via HA, then unblock
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests
import urllib3
import yaml
from flask import Flask, jsonify, request, send_from_directory

urllib3.disable_warnings()  # TV REST on 8002 uses a self-signed cert

log = logging.getLogger("kidlock")
BASE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- config ----
def load_config(path: Path) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    apply_app_options(cfg)
    d = Path(cfg.get("data_dir") or "data")
    cfg["data_dir"] = str(d if d.is_absolute() else BASE / d)   # relative = next to kidlock.py
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def apply_app_options(cfg: dict):
    """Home Assistant app: settings from the Configuration tab override config.yaml."""
    f = Path(os.environ.get("APP_OPTIONS", "/data/options.json"))
    if not f.exists():
        return
    try:
        o = json.loads(f.read_text())
    except ValueError:
        return
    def put(section, key, value):
        if value in (None, ""):
            return
        (cfg.setdefault(section, {}) if section else cfg)[key] = value
    put("tv", "host", o.get("tv_host"))
    put("tv", "port", o.get("tv_port"))
    put(None, "parent_pin", o.get("parent_pin"))
    put(None, "watch_minutes", o.get("watch_minutes"))
    put(None, "quiz_url", o.get("quiz_url"))
    put("home_assistant", "internet_block_switch", o.get("internet_block_switch"))
    put("home_assistant", "block_when", o.get("block_when"))
    put("home_assistant", "enable_entity", o.get("enable_entity"))
    put("quiz", "correct_to_unlock", o.get("correct_to_unlock"))
    if o.get("categories") is not None:
        cfg.setdefault("quiz", {})["categories"] = o.get("categories") or []
    if not o.get("quiz_url") and cfg.get("tv", {}).get("host"):
        cfg["quiz_url"] = f"http://{local_ip_towards(cfg['tv']['host'])}:{cfg.get('web_port', 5000)}/"


def local_ip_towards(host: str) -> str:
    """This machine's LAN IP on the route to the TV (no packets are sent)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------------------------- TV -----
class TV:
    """Thin wrapper: a fresh WebSocket per command (Tizen drops idle sockets)."""

    def __init__(self, cfg: dict):
        t = cfg["tv"]
        self.host = t["host"]
        self.port = int(t.get("port", 8002))
        self.token_file = str(Path(cfg["data_dir"]) / "tv_token.txt")
        self.dry_run = cfg.get("dry_run", False)

    def _ws(self, timeout: float = 5):
        from samsungtvws import SamsungTVWS
        return SamsungTVWS(host=self.host, port=self.port, token_file=self.token_file,
                           timeout=timeout, name="KidLock")

    def _do(self, label: str, fn):
        if self.dry_run:
            log.info("[dry-run] TV %s", label)
            return None
        tv = self._ws()
        try:
            return fn(tv)
        except Exception as e:  # noqa: BLE001
            log.warning("TV %s failed: %s", label, e)
            return None
        finally:
            try:
                tv.close()
            except Exception:  # noqa: BLE001
                pass

    def is_on(self) -> bool:
        if self.dry_run:
            return Path(BASE / "data" / "dry_tv_on").exists()
        try:
            info = self._ws(timeout=3).rest_device_info()
        except Exception:  # noqa: BLE001
            return False
        # Newer models report PowerState; older ones only answer when on.
        state = info.get("device", {}).get("PowerState")
        return state is None or state == "on"

    @property
    def paired(self) -> bool:
        return self.port != 8002 or Path(self.token_file).exists()

    def pair(self):
        """Open a WebSocket; on 8002 the TV shows Allow/Deny and returns a token."""
        self._do("pair", lambda tv: tv.open())

    def key(self, k: str):
        self._do(f"key {k}", lambda tv: tv.send_key(k))

    BROWSER = "org.tizen.browser"

    def open_browser(self, url: str):
        # If the browser is already running (e.g. he was watching video in it),
        # a launch request just brings the old tab back instead of loading the
        # quiz. So close it first, then open it fresh on the quiz page.
        if not self.dry_run:
            ws = self._ws(timeout=3)
            try:
                running = bool(ws.rest_app_status(self.BROWSER).get("running"))
            except Exception:  # noqa: BLE001  some models don't report it
                running = None
            if running is not False:
                try:
                    ws.rest_app_close(self.BROWSER)
                    if running:
                        log.info("Browser was already open - closed it so the quiz opens fresh")
                    time.sleep(2)
                except Exception as e:  # noqa: BLE001
                    log.debug("browser close: %s", e)
        self._do(f"browser {url}", lambda tv: tv.open_browser(url))

    def run_app(self, app_id: str):
        self._do(f"run {app_id}", lambda tv: tv.run_app(app_id))

    def app_visible(self, app_id: str) -> bool:
        if self.dry_run:
            return False
        try:
            st = self._ws(timeout=3).rest_app_status(app_id)
            return bool(st.get("visible"))
        except Exception:  # noqa: BLE001
            return False

    def close_app(self, app_id: str):
        if self.dry_run:
            log.info("[dry-run] TV close %s", app_id)
            return
        try:
            self._ws(timeout=3).rest_app_close(app_id)
        except Exception as e:  # noqa: BLE001
            log.warning("close %s failed: %s", app_id, e)


# --------------------------------------------------------- Home Assistant ---
class HA:
    def __init__(self, cfg: dict):
        h = cfg.get("home_assistant") or {}
        self.url = (h.get("url") or "").rstrip("/")
        self.token = h.get("token") or os.environ.get("HA_TOKEN", "")
        # Running as a Home Assistant add-on: no URL/token needed
        if not (self.url and self.token) and os.environ.get("SUPERVISOR_TOKEN"):
            self.url, self.token = "http://supervisor/core", os.environ["SUPERVISOR_TOKEN"]
        # A switch that blocks the TV's INTERNET only (a UniFi traffic rule /
        # firewall policy). Don't use the UniFi "block client" switch: that cuts
        # the TV off the LAN too, so the quiz page and remote control stop working.
        self.block_entity = h.get("internet_block_switch")
        self.block_state = str(h.get("block_when", "on")).lower()   # state that means blocked
        self.enable_entity = h.get("enable_entity")          # input_boolean.tv_kid_lock
        self.status_sensor = h.get("status_sensor", "sensor.tv_kid_lock")
        self.enabled = bool(self.url and self.token)
        self.dry_run = cfg.get("dry_run", False)

    def _h(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _call(self, domain: str, service: str, entity: str):
        if not (self.enabled and entity):
            return
        if self.dry_run:
            log.info("[dry-run] HA %s.%s %s", domain, service, entity)
            return
        try:
            r = requests.post(f"{self.url}/api/services/{domain}/{service}",
                              headers=self._h(), json={"entity_id": entity}, timeout=5)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.warning("HA %s.%s failed: %s", domain, service, e)

    def block_internet(self):
        log.info("Blocking TV internet")
        self._call("switch", "turn_on" if self.block_state == "on" else "turn_off",
                   self.block_entity)

    def allow_internet(self):
        log.info("Allowing TV internet")
        self._call("switch", "turn_off" if self.block_state == "on" else "turn_on",
                   self.block_entity)

    def lock_enabled(self) -> bool:
        """True unless the HA toggle exists and is explicitly off."""
        if not (self.enabled and self.enable_entity) or self.dry_run:
            return True
        try:
            r = requests.get(f"{self.url}/api/states/{self.enable_entity}",
                             headers=self._h(), timeout=5)
            r.raise_for_status()
            return r.json().get("state") != "off"
        except Exception as e:  # noqa: BLE001
            log.debug("HA enable check failed (%s) - assuming enabled", e)
            return True  # fail closed: keep enforcing if HA is down

    def publish(self, state: str, attrs: dict):
        if not self.enabled or self.dry_run:
            return
        try:
            requests.post(f"{self.url}/api/states/{self.status_sensor}", headers=self._h(),
                          json={"state": state, "attributes": {
                              "friendly_name": "TV Kid Lock", "icon": "mdi:television-guide",
                              **attrs}}, timeout=5)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ quiz ----
LETTERS = "ABCDEFGH"


def generated_question(cfg: dict) -> dict:
    q = cfg.get("quiz", {})
    kinds = q.get("types", ["multiply"])
    lo, hi = q.get("min", 2), q.get("max", 9)
    kind = random.choice(kinds)
    a, b = random.randint(lo, hi), random.randint(lo, hi)
    if kind == "add":
        text, ans = f"{a} + {b}", a + b
    elif kind == "subtract":
        a, b = max(a, b), min(a, b)
        text, ans = f"{a} − {b}", a - b
    elif kind == "divide":
        text, ans = f"{a * b} ÷ {a}", b
    else:
        text, ans = f"{a} × {b}", a * b
    return {"id": f"gen:{text}", "kind": "number", "category": "arithmetic",
            "question": f"{text} = ?", "answer": ans, "big": True}


class QuestionBank:
    """Loads every *.yaml in the questions folder; reloads when files change.

    Picks questions he hasn't seen or has got wrong more often, and avoids
    repeating the last few.
    """

    def __init__(self, cfg: dict):
        q = cfg.get("quiz", {})
        self.cfg = cfg
        d = Path(q.get("bank_dir", "questions"))
        self.dir = d if d.is_absolute() else BASE / d
        self.categories = q.get("categories") or []
        self.source = q.get("source", "bank")            # bank | generated | mixed
        self.mix = float(q.get("generated_share", 0.3))  # for "mixed"
        self.stats_file = Path(cfg["data_dir"]) / "question_stats.json"
        self.results_file = Path(cfg["data_dir"]) / "results.jsonl"
        self.questions: list[dict] = []
        self._mtimes: dict = {}
        self.recent: list[str] = []
        try:
            self.stats = json.loads(self.stats_file.read_text())
        except Exception:  # noqa: BLE001
            self.stats = {}

    # ---- loading / validation
    def _files(self):
        return sorted(self.dir.glob("*.yaml")) + sorted(self.dir.glob("*.yml"))

    def reload_if_changed(self):
        mt = {str(p): p.stat().st_mtime for p in self._files()} if self.dir.exists() else {}
        if mt != self._mtimes:
            self._mtimes = mt
            self.questions, errors = self.load()
            for e in errors:
                log.warning("question bank: %s", e)
            log.info("Question bank: %d questions loaded", len(self.questions))

    def load(self) -> tuple[list[dict], list[str]]:
        out, errors = [], []
        for path in self._files():
            try:
                items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
            except Exception as e:  # noqa: BLE001
                errors.append(f"{path.name}: YAML error: {e}")
                continue
            if isinstance(items, dict):
                items = items.get("questions", [])
            for i, raw in enumerate(items, 1):
                where = f"{path.name} #{i}"
                try:
                    out.append(self._normalise(raw, path.stem))
                except ValueError as e:
                    errors.append(f"{where}: {e}")
        if self.categories:
            out = [q for q in out if q["category"] in self.categories]
        return out, errors

    @staticmethod
    def _normalise(raw: dict, default_cat: str) -> dict:
        if not isinstance(raw, dict) or not raw.get("question"):
            raise ValueError("missing 'question'")
        if "answer" not in raw:
            raise ValueError("missing 'answer'")
        q = {
            "id": str(raw.get("id") or hashlib.sha1(raw["question"].encode()).hexdigest()[:10]),
            "category": raw.get("category", default_cat),
            "question": str(raw["question"]).strip(),
            "passage": re.sub(r"(?<!\n)\n(?!\n)", " ", str(raw.get("passage", "")).strip()),
            "image": raw.get("image", ""),
            "explanation": str(raw.get("explanation", "")).strip(),
        }
        choices = raw.get("choices")
        if choices:
            if not 2 <= len(choices) <= len(LETTERS):
                raise ValueError("choices must have 2-8 options")
            ans = str(raw["answer"]).strip().upper()
            if ans in LETTERS[:len(choices)]:
                idx = LETTERS.index(ans)
            else:  # answer given as the choice text itself
                texts = [str(c).strip() for c in choices]
                if str(raw["answer"]).strip() not in texts:
                    raise ValueError(f"answer '{raw['answer']}' is not A-{LETTERS[len(choices)-1]}"
                                     " or one of the choices")
                idx = texts.index(str(raw["answer"]).strip())
            q.update(kind="choice", choices=[str(c) for c in choices], answer=idx)
        else:
            try:
                q.update(kind="number", answer=float(raw["answer"]))
            except (TypeError, ValueError):
                raise ValueError("no 'choices', so 'answer' must be a number") from None
        return q

    # ---- picking
    def next(self) -> dict:
        self.reload_if_changed()
        use_bank = self.questions and (
            self.source == "bank" or (self.source == "mixed" and random.random() > self.mix))
        if not use_bank:
            return generated_question(self.cfg)
        pool = [q for q in self.questions if q["id"] not in self.recent] or self.questions
        weights = []
        for q in pool:
            st = self.stats.get(q["id"], {})
            seen, wrong = st.get("seen", 0), st.get("wrong", 0)
            w = 3.0 if seen == 0 else 1.0 + 4.0 * wrong / seen
            if st.get("streak", 0) >= 3:     # mastered: show rarely
                w *= 0.2
            weights.append(w)
        q = random.choices(pool, weights=weights)[0]
        self.recent = (self.recent + [q["id"]])[-min(20, max(1, len(self.questions) // 2)):]
        return q

    # ---- marking
    @staticmethod
    def check(q: dict, given) -> bool | None:
        g = str(given).strip().upper()
        if q["kind"] == "choice":
            if g in LETTERS[:len(q["choices"])]:
                return LETTERS.index(g) == q["answer"]
            return None
        try:
            return abs(float(g) - float(q["answer"])) < 1e-9
        except ValueError:
            return None

    @staticmethod
    def answer_text(q: dict) -> str:
        if q["kind"] == "choice":
            return f"{LETTERS[q['answer']]}: {q['choices'][q['answer']]}"
        a = q["answer"]
        return str(int(a)) if float(a).is_integer() else str(a)

    def record(self, q: dict, given, ok: bool):
        st = self.stats.setdefault(q["id"], {"seen": 0, "wrong": 0, "streak": 0})
        st["seen"] += 1
        st["wrong"] += 0 if ok else 1
        st["streak"] = st["streak"] + 1 if ok else 0
        try:
            self.stats_file.write_text(json.dumps(self.stats))
            with open(self.results_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "id": q["id"],
                                    "category": q["category"], "question": q["question"][:120],
                                    "given": str(given), "correct": self.answer_text(q),
                                    "ok": ok}, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001
            log.warning("could not write results: %s", e)


def public_question(q: dict) -> dict:
    """What the TV page gets - never the answer."""
    keep = ("kind", "category", "question", "passage", "image", "choices", "big")
    return {k: q[k] for k in keep if q.get(k) not in (None, "", [])}


# ----------------------------------------------------------------- state ----
@dataclass
class State:
    watched_s: float = 0.0
    locked: bool = False
    q: dict = field(default_factory=dict)   # current question (with answer)
    correct_streak: int = 0
    wrong_count: int = 0
    cooldown_until: float = 0.0
    last_on: float = 0.0
    resume_app: str = ""
    unlocked_at: float = 0.0
    log: list = field(default_factory=list)


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tv = TV(cfg)
        self.ha = HA(cfg)
        self.lk = threading.RLock()
        self.state_file = Path(cfg["data_dir"]) / "state.json"
        self.s = self._load()
        self.last_heartbeat = 0.0
        self.last_browser_open = 0.0
        self.reopens_unseen = 0          # browser opens with no page load since
        self.page_loads: dict = {}       # client IP -> last time it loaded "/"
        self.last_status_from: tuple = ("", 0.0)
        self.apps: dict = cfg.get("block_apps", {})
        self.bank = QuestionBank(cfg)
        self.bank.reload_if_changed()

    # persistence: a restart (or the kid pulling the plug) doesn't reset anything
    def _load(self) -> State:
        try:
            data = json.loads(self.state_file.read_text())
            return State(**{k: v for k, v in data.items() if k in State.__dataclass_fields__})
        except Exception:  # noqa: BLE001
            return State()

    def _save(self):
        self.state_file.write_text(json.dumps(asdict(self.s)))

    @property
    def limit_s(self) -> int:
        return int(self.cfg.get("watch_minutes", 10) * 60)

    # ------------------------------------------------------------ lock ---
    def lock(self):
        with self.lk:
            if self.s.locked:
                return
            log.info("Watch limit reached - locking")
            self.s.locked = True
            self.s.correct_streak = 0
            self.s.wrong_count = 0
            self.s.q = self.bank.next()
            self.s.resume_app = next((aid for aid in self.apps if self.tv.app_visible(aid)), "")
            self._save()
        self.tv.key("KEY_PAUSE")
        self.ha.block_internet()
        time.sleep(1)
        self._show_quiz()

    def unlock(self, reason: str):
        with self.lk:
            if not self.s.locked:
                return
            log.info("Unlocking (%s)", reason)
            self.s.locked = False
            self.s.watched_s = 0
            self.s.unlocked_at = time.time()
            self.s.log = (self.s.log + [{"t": time.strftime("%Y-%m-%d %H:%M"),
                                         "how": reason}])[-50:]
            resume = self.s.resume_app
            self._save()
        self.ha.allow_internet()
        if resume and self.cfg.get("resume_app_on_unlock", True):
            time.sleep(3)  # let the success screen show
            self.tv.run_app(resume)

    def _show_quiz(self):
        self.last_browser_open = time.time()
        self.last_heartbeat = time.time()  # grace period while the browser loads
        log.info("Opening quiz on TV: %s", self.cfg["quiz_url"])
        self.tv.open_browser(self.cfg["quiz_url"])

    def page_loaded(self, ip: str):
        first = ip not in self.page_loads
        self.page_loads[ip] = time.time()
        self.reopens_unseen = 0
        log.info("Quiz page loaded by %s%s", ip,
                 " (the TV)" if ip == self.tv.host else "")
        if first and ip != self.tv.host:
            log.info("(That isn't the TV's IP %s - fine if it's your phone/PC)", self.tv.host)

    # ------------------------------------------------------------ loop ---
    def run(self):
        if self.s.locked:  # re-assert after a restart
            self.ha.block_internet()
        else:
            self.ha.allow_internet()  # never leave the TV blocked by accident
        poll = self.cfg.get("poll_seconds", 15)
        enforce = self.cfg.get("enforce_seconds", 3)
        idle_reset = self.cfg.get("idle_reset_minutes", 30) * 60
        last_tick = time.time()
        last_poll = 0.0

        while True:
            now = time.time()
            if not self.ha.lock_enabled():
                if self.s.locked:
                    self.unlock("parent override (HA toggle off)")
                with self.lk:
                    self.s.watched_s = 0
                self._publish("disabled")
                last_tick = now
                time.sleep(poll)
                continue

            if self.s.locked:
                self._enforce(now)
                time.sleep(enforce)
                last_tick = time.time()
                continue

            if now - last_poll >= poll:
                last_poll = now
                on = self.tv.is_on()
                with self.lk:
                    if on:
                        self.s.watched_s += min(now - last_tick, poll * 2)
                        self.s.last_on = now
                    elif self.s.last_on and now - self.s.last_on > idle_reset:
                        self.s.watched_s = 0  # long break earns a fresh allowance
                    self._save()
                last_tick = now
                self._publish("watching" if on else "off")
                if on and self.s.watched_s >= self.limit_s:
                    self.lock()
            time.sleep(1)

    def _enforce(self, now: float):
        # Kid switched back to a streaming app -> close it, reshow quiz
        for app_id, name in self.apps.items():
            if self.tv.app_visible(app_id):
                log.info("%s came to front while locked - closing", name)
                self.tv.close_app(app_id)
                self._show_quiz()
                return
        # Quiz page stopped polling (browser closed / HDMI / live TV) -> reshow.
        # If the TV never loads the page, back off instead of reopening forever.
        retry = 15 if self.reopens_unseen < 3 else 120
        if (now - self.last_heartbeat > self.cfg.get("heartbeat_timeout", 20)
                and now - self.last_browser_open > retry and self.tv.is_on()):
            self.reopens_unseen += 1
            log.info("Quiz not on screen - reopening (attempt %d)", self.reopens_unseen)
            if self.reopens_unseen == 3:
                self._diagnose()
            self.tv.key("KEY_PAUSE")
            self._show_quiz()
        self._publish("locked")

    def _diagnose(self):
        url = self.cfg["quiz_url"]
        tv_loaded = self.tv.host in self.page_loads
        log.warning("The TV has not shown the quiz page after 3 tries. Retrying every 2 min.")
        log.warning("  Quiz address: %s", url)
        if not self.page_loads:
            log.warning("  Nothing has loaded that address yet. Check on the TV whether the "
                        "Internet browser opened, and open %s on your phone to test it.", url)
        elif not tv_loaded:
            log.warning("  Other devices loaded it (%s) but never the TV: either the browser "
                        "isn't opening, or the TV can't reach this address (VLAN/firewall).",
                        ", ".join(self.page_loads))
        else:
            log.warning("  The TV loaded the page but it stopped reporting back - "
                        "possibly the browser closed or the page script isn't running.")

    def _publish(self, state: str):
        self.ha.publish(state, {
            "watched_minutes": round(self.s.watched_s / 60, 1),
            "limit_minutes": self.limit_s / 60,
            "locked": self.s.locked,
            "recent_unlocks": self.s.log[-5:],
        })

    # ------------------------------------------------------------ quiz ---
    def status(self, ip: str = "") -> dict:
        self.last_heartbeat = time.time()
        self.reopens_unseen = 0
        self.last_status_from = (ip, self.last_heartbeat)
        with self.lk:
            q = self.cfg.get("quiz", {})
            return {
                "locked": self.s.locked,
                "q": public_question(self.s.q) if self.s.locked and self.s.q else None,
                "qid": self.s.q.get("id", "") if self.s.locked else "",
                "needed": q.get("correct_to_unlock", 2),
                "streak": self.s.correct_streak,
                "cooldown": max(0, int(self.s.cooldown_until - time.time())),
                "minutes_left": max(0, round((self.limit_s - self.s.watched_s) / 60)),
            }

    def answer(self, value) -> dict:
        q = self.cfg.get("quiz", {})
        with self.lk:
            if not self.s.locked:
                return {"result": "unlocked"}
            if time.time() < self.s.cooldown_until:
                return {"result": "cooldown"}
            cur = self.s.q
            if not cur:
                self.s.q = self.bank.next()
                return {"result": "invalid"}
            ok = self.bank.check(cur, value)
            if ok is None:
                return {"result": "invalid"}
            self.bank.record(cur, value, ok)
            done = False
            if ok:
                self.s.correct_streak += 1
                self.s.wrong_count = 0
                done = self.s.correct_streak >= q.get("correct_to_unlock", 2)
            else:
                self.s.correct_streak = 0
                self.s.wrong_count += 1
                if self.s.wrong_count >= q.get("max_wrong", 3):
                    self.s.cooldown_until = time.time() + q.get("cooldown_seconds", 30)
                    self.s.wrong_count = 0
            if not done:
                self.s.q = self.bank.next()
            self._save()
        if done:
            threading.Thread(target=self.unlock, args=("quiz",), daemon=True).start()
            return {"result": "unlocked"}
        if ok:
            return {"result": "correct"}
        resp = {"result": "wrong"}
        if q.get("show_answer_when_wrong", True):
            resp.update(correct=self.bank.answer_text(cur), explanation=cur.get("explanation", ""))
        return resp


def check_bank(cfg: dict) -> tuple[dict, list[str]]:
    bank = QuestionBank(cfg)
    qs, errors = bank.load()
    cats: dict = {}
    for q in qs:
        cats[q["category"]] = cats.get(q["category"], 0) + 1
        if q.get("image") and not (bank.dir / "images" / q["image"]).exists():
            errors.append(f"{q['id']}: image '{q['image']}' not found in questions/images/")
    return cats, errors


def results_summary(cfg: dict, wrong_limit: int = 20) -> tuple[list, list]:
    f = Path(cfg["data_dir"]) / "results.jsonl"
    rows = []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    by: dict = {}
    for r in rows:
        c = by.setdefault(r["category"], [0, 0])
        c[0] += 1
        c[1] += bool(r["ok"])
    cats = [(c, n, ok * 100 // n) for c, (n, ok) in sorted(by.items())]
    wrong = [r for r in rows if not r["ok"]][-wrong_limit:][::-1]
    return cats, wrong


def pair_loop(ctl: "Controller"):
    """Keep offering pairing until the TV accepts (needs the TV on)."""
    tv = ctl.tv
    while not tv.paired and not tv.dry_run:
        if tv.is_on():
            log.info("Pairing: press ALLOW on the TV screen (KidLock)")
            tv.pair()
        if tv.paired:
            log.info("Paired with TV")
            break
        time.sleep(30)
    if not tv.dry_run:
        apps = tv._do("app_list", lambda t: t.app_list()) or []
        if apps:
            log.info("Installed TV apps (use these IDs in block_apps):")
            for a in sorted(apps, key=lambda a: a.get("name", "")):
                log.info("  %-24s %s", a.get("appId"), a.get("name"))


# ------------------------------------------------------------------ web -----
def build_app(ctl: Controller) -> Flask:
    app = Flask(__name__, static_folder=None)

    @app.get("/")
    def index():
        ctl.page_loaded(request.remote_addr or "?")
        resp = send_from_directory(BASE, "quiz.html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/img/<path:name>")
    def img(name):
        return send_from_directory(ctl.bank.dir / "images", name)

    @app.get("/status")
    def status():
        resp = jsonify(ctl.status(request.remote_addr or ""))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/answer")
    def answer():
        data = request.get_json(silent=True) or request.form
        return jsonify(ctl.answer(data.get("answer", "")))

    def pin_ok() -> bool:
        pin = str(ctl.cfg.get("parent_pin", ""))
        given = request.headers.get("X-Pin") or request.values.get("pin", "")
        return bool(pin) and pin != "change-me" and given == pin

    @app.route("/parent", methods=["GET", "POST"])
    def parent():
        from html import escape as e
        if not pin_ok():
            return ("<meta name=viewport content='width=device-width'>"
                    "<form style='font:18px sans-serif;margin:2em'>Parent PIN "
                    "<input name=pin type=password inputmode=numeric autofocus> "
                    "<button>Open</button></form>"), 200 if "pin" not in request.values else 403
        act = request.form.get("do")
        if act == "unlock":
            threading.Thread(target=ctl.unlock, args=("parent PIN",), daemon=True).start()
        elif act == "lock":
            threading.Thread(target=ctl.lock, daemon=True).start()
        if act:
            time.sleep(1)
        cats, wrong = results_summary(ctl.cfg)
        _, errs = check_bank(ctl.cfg)
        s = ctl.s
        pin = e(request.values.get("pin", ""))
        rows = "".join(f"<tr><td>{e(c)}</td><td>{n}</td><td>{p}%</td></tr>" for c, n, p in cats) \
            or "<tr><td colspan=3>No answers yet</td></tr>"
        wr = "".join(f"<li><small>{e(r['t'][:16])} · {e(r['category'])}</small><br>"
                     f"{e(r['question'])}<br><b>gave {e(r['given'])}</b>, answer {e(r['correct'])}</li>"
                     for r in wrong) or "<li>None</li>"
        er = "".join(f"<li>{e(x)}</li>" for x in errs)
        return f"""<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>
<title>TV Kid Lock</title><style>body{{font:16px system-ui,sans-serif;margin:1em;max-width:40em}}
table{{border-collapse:collapse}}td,th{{padding:.3em .8em;border-bottom:1px solid #ccc;text-align:left}}
button{{font-size:1em;padding:.5em 1em;margin-right:.5em}}li{{margin:.6em 0}}.err{{color:#b00}}</style>
<h2>TV Kid Lock</h2>
<p><b>{'LOCKED - quiz on screen' if s.locked else 'Unlocked'}</b> ·
watched {s.watched_s / 60:.0f} of {ctl.limit_s / 60:.0f} min ·
TV {'paired' if ctl.tv.paired else '<span class=err>not paired yet</span>'}</p>
<p><small>Quiz address: <a href="{e(ctl.cfg['quiz_url'])}">{e(ctl.cfg['quiz_url'])}</a> ·
TV IP {e(ctl.tv.host)} · last quiz check-in:
{e(ctl.last_status_from[0]) + ', ' + str(int(time.time() - ctl.last_status_from[1])) + ' s ago' if ctl.last_status_from[1] else 'never'}</small></p>
<form method=post><input type=hidden name=pin value="{pin}">
<button name=do value=unlock>Unlock now</button><button name=do value=lock>Quiz now</button></form>
{f'<h3 class=err>Question file problems</h3><ul class=err>{er}</ul>' if er else ''}
<h3>Results by category</h3><table><tr><th>Category</th><th>Answered</th><th>Correct</th></tr>{rows}</table>
<h3>Recent wrong answers</h3><ul>{wr}</ul>"""

    @app.post("/parent/unlock")
    def parent_unlock():
        if not pin_ok():
            return jsonify({"ok": False}), 403
        threading.Thread(target=ctl.unlock, args=("parent PIN",), daemon=True).start()
        return jsonify({"ok": True})

    @app.post("/parent/lock")
    def parent_lock():
        if not pin_ok():
            return jsonify({"ok": False}), 403
        threading.Thread(target=ctl.lock, daemon=True).start()
        return jsonify({"ok": True})

    return app


# ------------------------------------------------------------------ main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--pair", action="store_true")
    ap.add_argument("--list-apps", action="store_true")
    ap.add_argument("--test-block", action="store_true")
    ap.add_argument("--check-questions", action="store_true")
    ap.add_argument("--results", action="store_true", help="summary of answers so far")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(Path(args.config))
    tv = TV(cfg)

    if args.pair:
        print("Connecting - press ALLOW on the TV if prompted...")
        tv.pair()
        print("Token saved." if Path(tv.token_file).exists() else
              "No token file - port 8001 models don't need one; check the TV prompt.")
        return
    if args.list_apps:
        apps = tv._do("app_list", lambda t: t.app_list()) or []
        for a in sorted(apps, key=lambda a: a.get("name", "")):
            print(f"{a.get('appId'):<40} {a.get('name')}")
        if not apps:
            print("app_list not supported on this model - see README for known IDs.")
        return
    if args.check_questions:
        cats, errors = check_bank(cfg)
        print(f"{sum(cats.values())} questions")
        for c, n in sorted(cats.items()):
            print(f"  {c:<20} {n}")
        for e in errors:
            print("ERROR", e)
        raise SystemExit(1 if errors else 0)
    if args.results:
        cats, wrong = results_summary(cfg, 15)
        if not cats:
            print("No answers recorded yet."); return
        print("Category             answered  correct")
        for c, n, p in cats:
            print(f"{c:<20} {n:>8}  {p:>6}%")
        print("\nRecent wrong answers:")
        for r in wrong:
            print(f"  {r['t'][:16]}  [{r['category']}] {r['question'][:60]}  "
                  f"gave {r['given']}, answer {r['correct']}")
        return
    if args.test_block:
        ha = HA(cfg)
        ha.block_internet(); print("Blocked - check the TV. Unblocking in 20 s...")
        time.sleep(20); ha.allow_internet(); print("Unblocked.")
        return

    cats, errors = check_bank(cfg)
    log.info("Questions: %s", ", ".join(f"{c} {n}" for c, n in sorted(cats.items())) or "none")
    for e in errors:
        log.warning("Question file problem: %s", e)
    ha = HA(cfg)
    log.info("Home Assistant: %s", ha.url or "not configured (no internet blocking)")
    log.info("Quiz page for the TV: %s", cfg["quiz_url"])

    ctl = Controller(cfg)
    threading.Thread(target=pair_loop, args=(ctl,), daemon=True).start()
    threading.Thread(target=ctl.run, daemon=True).start()
    port = int(cfg.get("web_port", 5000))
    try:
        from waitress import serve
        log.info("Web server on port %d", port)
        serve(build_app(ctl), host="0.0.0.0", port=port, threads=8, _quiet=True)
    except ImportError:
        build_app(ctl).run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
