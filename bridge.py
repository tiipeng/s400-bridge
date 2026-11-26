"""Xiaomi S400 -> Home Assistant -> iPhone (Apple Health).

Liest JSON-Zeilen von blescan (stdin). Wacht die Waage auf, verbindet sich gatt.py mit ihr
(wie die Xiaomi-App), meldet die passende Person, die Waage rechnet Körperfett usw. selbst und
zeigt es an. Klappt das nicht, gilt der alte Weg: S400-Funkpakete mit xiaomi-ble entschlüsseln,
per Gewicht zuordnen und Körperfett/Magermasse/BMI selbst berechnen.
speichert alles in waage.db, setzt Sensoren in Home Assistant und schickt eine
Push-Nachricht, deren Tipp den iPhone-Kurzbefehl "Waage Health" startet.

Geheimnisse liegen im macOS-Schlüsselbund (Dienst "waage-bridge", Konten "bindkey", "token", "ha_token").
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

from habluetooth.models import BluetoothServiceInfo
from xiaomi_ble import XiaomiBluetoothDeviceData

HERE = Path(__file__).resolve().parent
FE95 = "0000fe95-0000-1000-8000-00805f9b34fb"
S400_PIDS = {0x30D9, 0x3BD5, 0x48CF}

log = logging.getLogger("waage")


# ---------------------------------------------------------------- Konfiguration

def keychain(account: str) -> str | None:
    r = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", "waage-bridge", "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() or None


def load_config() -> dict:
    return json.loads((HERE / "config.json").read_text())


# ---------------------------------------------------------------- Körperwerte
# Formeln der Xiaomi-Waagen, wie sie openScale und bodymiscale verwenden.

def body_fat_pct(sex: str, age: int, weight: float, height: float, impedance: float) -> float:
    lbm = (height * 9.058 / 100) * (height / 100) + weight * 0.32 + 12.226
    lbm -= impedance * 0.0068 + age * 0.0542
    if sex == "f":
        const = 9.25 if age <= 49 else 7.25
    else:
        const = 0.8
    coef = 1.0
    if sex == "m" and weight < 61:
        coef = 0.98
    elif sex == "f" and weight > 60:
        coef = 0.96 * (1.03 if height > 160 else 1.0)
    elif sex == "f" and weight < 50:
        coef = 1.02 * (1.03 if height > 160 else 1.0)
    fat = (1.0 - ((lbm - const) * coef) / weight) * 100
    if fat > 63:
        fat = 75
    return round(min(max(fat, 5.0), 75.0), 1)


def metrics(person: dict, weight: float, impedance: float | None, hr: int | None) -> dict:
    h = person["height_cm"]
    age = date.today().year - person["birth_year"]
    m = {"weight": weight}
    if impedance:
        m["impedance"] = impedance  # immer speichern, damit Fett später nachberechnet werden kann
    if hr:
        m["hr"] = hr
    if not h or not person["birth_year"]:  # Angaben fehlen noch -> kein BMI/Fett
        return m
    m["bmi"] = round(weight / (h / 100) ** 2, 1)
    if impedance:
        fat = body_fat_pct(person["sex"], age, weight, h, impedance)
        m["fat"] = fat
        m["lean"] = round(weight * (1 - fat / 100), 1)
    return m


# ---------------------------------------------------------------- Speicher

class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute(
            """create table if not exists messung (
                ts text not null, person text not null, gewicht real not null,
                fett real, mager real, bmi real, puls integer, impedanz real)"""
        )
        cols = [r[1] for r in self.db.execute("pragma table_info(messung)")]
        if "waage" not in cols:  # alle Werte, die die Waage selbst berechnet hat (JSON)
            self.db.execute("alter table messung add column waage text")
        self.db.commit()

    def add(self, person: str, m: dict) -> None:
        with self.lock:
            self.db.execute(
                "insert into messung values (?,?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), person, m["weight"],
                 m.get("fat"), m.get("lean"), m.get("bmi"), m.get("hr"), m.get("impedance"),
                 json.dumps(m["body"]) if m.get("body") else None),
            )
            self.db.commit()

    def last_weight(self, person: str) -> float | None:
        with self.lock:
            r = self.db.execute(
                "select gewicht from messung where person=? order by ts desc limit 1", (person,)
            ).fetchone()
        return r[0] if r else None

    def last(self, person: str) -> tuple | None:
        with self.lock:
            return self.db.execute(
                "select ts,gewicht,fett,mager,bmi,puls from messung where person=? order by ts desc limit 1",
                (person,),
            ).fetchone()


# ---------------------------------------------------------------- Home Assistant

class HA:
    def __init__(self, url: str, token: str | None):
        self.url = url.rstrip("/")
        self.token = token

    def _post(self, path: str, body: dict) -> None:
        if not self.token:
            log.warning("kein HA-Token im Schlüsselbund, überspringe %s", path)
            return
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()

    def set_state(self, entity: str, value, unit: str | None, name: str, icon: str, ts: str) -> None:
        attrs = {"friendly_name": name, "icon": icon, "state_class": "measurement", "gemessen": ts}
        if unit:
            attrs["unit_of_measurement"] = unit
        if unit == "kg":
            attrs["device_class"] = "weight"
        self._post(f"/api/states/{entity}", {"state": value, "attributes": attrs})

    def notify(self, service: str, title: str, message: str, url: str) -> None:
        self._post(f"/api/services/notify/{service}",
                   {"title": title, "message": message, "data": {"url": url}})


SENSORS = [  # (Spalte in Store.last, Schlüssel, Einheit, Name, Icon)
    (1, "gewicht", "kg", "Gewicht", "mdi:scale-bathroom"),
    (2, "koerperfett", "%", "Körperfett", "mdi:water-percent"),
    (3, "magermasse", "kg", "Magermasse", "mdi:arm-flex"),
    (4, "bmi", None, "BMI", "mdi:human"),
    (5, "puls", "bpm", "Puls", "mdi:heart-pulse"),
]


def publish_states(ha: HA, store: Store, person: dict) -> None:
    row = store.last(person["id"])
    if not row:
        return
    for col, key, unit, name, icon in SENSORS:
        if row[col] is not None:
            ha.set_state(f"sensor.waage_{person['id']}_{key}", row[col], unit,
                         f"{name} {person['name']}", icon, row[0])


# ---------------------------------------------------------------- Messlogik

class Measurement:
    """Sammelt die Pakete einer Wägung aus den rohen S400-Werten (Gewicht, Puls, Impedanz).
    Ablauf laut xiaomi-ble: Pakete mit Gewicht+Puls+Impedanz 50 kHz, dann Abschlusspaket
    nur mit Impedanz 250 kHz, beim Absteigen ein Paket mit lauter Nullen.
    Mit Socken kommt nur Gewicht -> nach QUIET Sekunden ohne Abschluss wird nur das Gewicht gemeldet."""

    QUIET = 10

    def __init__(self, emit):
        self.emit = emit
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.weight = self.imp = self.hr = None
        self.done = False
        self.last_ts = 0.0

    def _fire(self):
        if self.weight and not self.done:
            self.done = True
            self.emit(self.weight, self.imp, self.hr)

    def raw(self, mass: int, hr: int, imp: int) -> None:
        with self.lock:
            if mass == 0x7FF:
                mass = 0
            if mass == 0 and hr == 0 and imp == 0:  # abgestiegen
                self._fire()
                self.reset()
                return
            self.last_ts = time.monotonic()
            if mass:
                if self.done and self.weight and abs(mass / 10 - self.weight) > 1:
                    self.reset()  # neue Wägung ohne Absteige-Paket
                    self.last_ts = time.monotonic()
                self.weight = mass / 10
                if 0 < hr < 127:
                    self.hr = hr + 50
                if imp:
                    self.imp = imp / 10
            elif imp and hr == 0:  # Abschlusspaket (250 kHz)
                self._fire()

    def tick(self) -> None:
        with self.lock:
            if self.weight and not self.done and time.monotonic() - self.last_ts > self.QUIET:
                self._fire()


def hook_s400(meas: Measurement) -> None:
    """Rohe S400-Werte direkt aus dem Parser von xiaomi-ble abgreifen."""
    import struct
    from xiaomi_ble import parser

    orig = parser.xiaomi_dataobject_dict[0x6E16]

    def wrapped(xobj, device, device_type):
        _, data, _ = struct.unpack("<BII", xobj)
        log.info("S400 roh: kg=%s puls=%s imp=%s", (data & 0x7FF) / 10, (data >> 11) & 0x7F, (data >> 18) / 10)
        meas.raw(data & 0x7FF, (data >> 11) & 0x7F, data >> 18)
        return orig(xobj, device, device_type)

    parser.xiaomi_dataobject_dict[0x6E16] = wrapped


def pick_person(cfg: dict, store: Store, weight: float) -> dict:
    """Wer am nächsten am eigenen letzten Gewicht liegt (Startwert: ref_kg aus config.json)."""
    def dist(p):
        return abs(weight - (store.last_weight(p["id"]) or p["ref_kg"]))
    return min(cfg["persons"], key=dist)


def export_icloud(cfg: dict, person: dict, m: dict, t: datetime) -> None:
    """Legt iCloud Drive/Waage/<Name>/neu/<zeit>.json an. Der iPhone-Kurzbefehl "Waage Health"
    trägt jede Datei in Apple Health ein und löscht sie danach."""
    folder = Path(cfg["icloud_dir"]).expanduser() / person["name"] / "neu"
    folder.mkdir(parents=True, exist_ok=True)
    item = {"datum": t.strftime("%Y-%m-%d %H:%M:%S"), **{k: m[k] for k in ("weight", "fat", "lean", "bmi") if k in m}}
    name = t.strftime("%Y-%m-%d_%H-%M-%S") + ".json"
    tmp = folder / ("." + name + ".tmp")
    tmp.write_text(json.dumps(item))
    tmp.replace(folder / name)


def from_scale(res: dict) -> dict:
    """Ergebnis der Waage (gatt.result_from_fields) -> unser Messwert-Format."""
    b = res["body"]
    m = {"weight": res["weight"]}
    for key, src in (("fat", "fat_percent"), ("lean", "fat_free_mass_kg"), ("bmi", "bmi")):
        if b.get(src) is not None:
            m[key] = b[src]
    if res.get("hr"):
        m["hr"] = res["hr"]
    if res.get("impedance_low"):
        m["impedance"] = res["impedance_low"]
    if len(b) > 1:
        m["body"] = b
    return m


def handle(cfg: dict, store: Store, ha: HA, weight: float, imp, hr, scale: dict | None = None) -> None:
    if scale:
        by_uid = {p["scale_uid"]: p for p in cfg["persons"]}
        person = by_uid.get(scale["uid"]) or pick_person(cfg, store, weight)
        m = from_scale(scale)
        if "fat" not in m:  # Waage hat niemanden erkannt (Profil kam zu spät) -> Formel
            for k, v in metrics(person, weight, imp, hr).items():
                m.setdefault(k, v)
    else:
        person = pick_person(cfg, store, weight)
        m = metrics(person, weight, imp, hr)
    store.add(person["id"], m)
    log.info("Messung %s: %s", person["name"], m)
    try:
        export_icloud(cfg, person, m, datetime.now())
    except Exception:
        log.exception("iCloud-Export fehlgeschlagen")
    try:
        publish_states(ha, store, person)
        msg = f"{m['weight']:.1f} kg"
        if "fat" in m:
            msg += f" · {m['fat']:.1f} % Fett"
        msg += " — tippen für Apple Health"
        # Tipp startet den Kurzbefehl, der die neuen Messungen aus iCloud holt
        url = "shortcuts://run-shortcut?name=" + urllib.parse.quote(cfg["shortcut_name"])
        if person.get("notify"):
            ha.notify(person["notify"], f"Waage · {person['name']}", msg, url)
    except Exception:
        log.exception("Home Assistant nicht erreichbar (Messung ist in waage.db gespeichert)")


def to_service_info(line: str, scale_mac: str | None = None) -> BluetoothServiceInfo | None:
    o = json.loads(line)
    data = bytes.fromhex(o["fe95"])
    if len(data) < 5 or int.from_bytes(data[2:4], "little") not in S400_PIDS:
        return None
    if data[0] & 0x10:  # MAC im Frame (nur Leerlauf-Pakete)
        mac = ":".join(f"{b:02X}" for b in data[5:11][::-1])
    elif scale_mac:  # Messpakete enthalten keine MAC; macOS verrät sie nicht -> aus config.json
        mac = scale_mac
    else:
        return None
    return BluetoothServiceInfo(o.get("name") or "", mac, o.get("rssi", 0), {}, {FE95: data}, [], "local")


class GattWorker:
    """Verbindet sich bei jedem Aufwachen der Waage (erstes Funkpaket nach einer Pause) mit ihr.

    Solange eine Sitzung läuft oder kurz nach einem Ergebnis werden Messungen aus den Funkpaketen
    zurückgehalten; liefert die Sitzung nichts, werden sie normal verarbeitet (alter Weg)."""

    COOLDOWN = 45  # s nach einer Sitzung: die Waage ist noch wach, nicht gleich wieder verbinden

    def __init__(self, cfg: dict, store: Store, ha: HA, token: bytes):
        import asyncio
        self.cfg, self.store, self.ha, self.token = cfg, store, ha, token
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.lock = threading.Lock()
        self.busy = False
        self.ended = -1e9
        self.result_at = -1e9
        self.pending: list[tuple] = []

    WAKE_SILENCE = 30  # s Funkstille davor = die Waage wacht gerade auf

    def start_scanner(self) -> None:
        """Eigener Dauer-Scan in diesem Prozess: beim ersten Signal nach einer Pause sofort mit
        genau diesem Gerät verbinden (ohne neue Suche) – die Waage misst schon, es zählt jede Sekunde."""
        import asyncio
        from bleak import BleakScanner
        scale_id = self.cfg.get("scale_ble_id")
        state = {"last": -1e9}

        def cb(device, adv):
            if not (device.address == scale_id or (adv.local_name or device.name or "").startswith("Mijia Scale")):
                return
            now = time.monotonic()
            woke = now - state["last"] >= self.WAKE_SILENCE
            state["last"] = now
            if woke:
                log.info("Waage wacht auf (rssi %s)", adv.rssi)
                self.poke(device.address, device)

        async def run():
            while True:
                try:
                    async with BleakScanner(cb):
                        while True:
                            await asyncio.sleep(3600)
                except Exception as e:
                    log.warning("Scanner neu gestartet: %r", e)
                    await asyncio.sleep(5)

        asyncio.run_coroutine_threadsafe(run(), self.loop)

    def poke(self, ble_id: str, device=None) -> None:
        import asyncio
        with self.lock:
            if self.busy or time.monotonic() - self.ended < self.COOLDOWN:
                return
            self.busy = True
        asyncio.run_coroutine_threadsafe(self._session(ble_id, device), self.loop)

    def adv_measurement(self, w, i, h) -> None:
        with self.lock:
            if self.busy:
                self.pending.append((w, i, h))
                return
            if time.monotonic() - self.result_at < 120:
                log.info("Funk-Messung %.1f kg ignoriert (Waage hat schon geliefert)", w)
                return
        handle(self.cfg, self.store, self.ha, w, i, h)

    async def _session(self, ble_id: str, device=None) -> None:
        import gatt
        cfg, store = self.cfg, self.store
        res = None
        try:
            last = max(cfg["persons"], key=lambda p: (store.last(p["id"]) or ("",))[0])
            res = await gatt.weigh_session(
                ble_id, self.token,
                make_users=lambda uid: gatt.users_json(cfg, uid, {p["id"]: store.last_weight(p["id"]) for p in cfg["persons"]}),
                pick_uid=lambda kg: pick_person(cfg, store, kg)["scale_uid"],
                start_uid=last["scale_uid"], timeout=60, find_timeout=10,
                idle_timeout=4, device=device)
        except Exception as e:
            log.warning("Verbindung zur Waage fehlgeschlagen: %r", e)
        ok = bool(res and res.get("weight"))
        if ok:
            try:
                handle(cfg, store, self.ha, res["weight"], res.get("impedance_low"), res.get("hr"), scale=res)
            except Exception:
                log.exception("Ergebnis der Waage nicht verarbeitet")
        with self.lock:
            self.busy = False
            self.ended = time.monotonic()
            if ok:
                self.result_at = time.monotonic()
            pending, self.pending = ([] if ok else self.pending), []
        for w, i, h in pending:
            handle(cfg, store, self.ha, w, i, h)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    key = keychain("bindkey")
    if not key:
        log.error("kein Bindkey im Schlüsselbund (security add-generic-password -s waage-bridge -a bindkey -w)")
    dev = XiaomiBluetoothDeviceData(bytes.fromhex(key) if key else None)
    store = Store(HERE / "waage.db")
    ha = HA(cfg["ha_url"], keychain("ha_token"))
    token = keychain("token")
    worker = GattWorker(cfg, store, ha, bytes.fromhex(token)) if token else None
    if worker and cfg.get("gatt_fast", False):  # aus: Waage funkt erst nach der Messung, kein Zeitfenster
        worker.start_scanner()
    else:
        log.warning("kein Waagen-Token im Schlüsselbund – nur Funk-Modus, Display zeigt kein Fett")
    meas = Measurement(worker.adv_measurement if worker else (lambda w, i, h: handle(cfg, store, ha, w, i, h)))
    hook_s400(meas)

    def ticker():
        while True:
            time.sleep(2)
            meas.tick()

    threading.Thread(target=ticker, daemon=True).start()

    def republish():  # nach HA-Neustart sind REST-Zustände weg -> regelmäßig erneuern
        while True:
            for p in cfg["persons"]:
                try:
                    publish_states(ha, store, p)
                except Exception as e:
                    log.warning("republish: %s", e)
            time.sleep(600)

    threading.Thread(target=republish, daemon=True).start()
    log.info("bereit")

    warned = False
    last_seen = -1e9  # letztes Funkpaket der Waage (monotonic)
    for line in sys.stdin:
        try:
            si = to_service_info(line, cfg["scale_mac"])
            if not si:
                continue
            now = time.monotonic()
            # Wach und ruhig funkt die Waage alle ~11 s. Erst nach >= 30 s Stille ist ein Paket ein
            # echtes Aufwachen (jemand steigt drauf) – sonst würden wir die Waage wachhalten.
            if worker and cfg.get("gatt_on_beacon", False) and now - last_seen >= 30:
                worker.poke(json.loads(line)["id"])
            last_seen = now
            if si.service_data[FE95][0] & 0x40:
                log.info("S400 Paket mit Daten (verschlüsselt=%s)", bool(si.service_data[FE95][0] & 0x08))
            dev.update(si)  # entschlüsselt; Werte kommen über hook_s400 an
            if si.service_data[FE95][0] & 0x08 and not dev.bindkey_verified and not warned:
                log.error("verschlüsseltes Paket nicht lesbar – Bindkey falsch oder fehlt")
                warned = True
        except Exception:
            log.exception("Fehler bei Paket: %s", line.strip())


if __name__ == "__main__":
    main()
