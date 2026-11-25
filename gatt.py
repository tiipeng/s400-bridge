"""Aktive Verbindung zur S400, wie es die Xiaomi-App macht (Mi-Home-v2-Login mit dem Token).

Nach dem Login bekommt die Waage die Benutzerprofile (MIoT-Aktion siid 13 / aiid 1, aus einem
iPhone-Bluetooth-Mitschnitt der Xiaomi-App nachgebaut). Damit erkennt die Waage die Person,
rechnet Körperfett usw. selbst, zeigt es auf dem Display und schickt am Ende einen Datensatz
(Ereignis siid 8 / eiid 2) mit ihren eigenen Werten.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import struct
import time
from datetime import date

from bleak import BleakClient, BleakScanner

from s400.offline import parse_record
from s400.session import S400Session

log = logging.getLogger("waage.gatt")

SIID_USER, AIID_SET_USERS = 13, 1
SIID_WEIGH, EIID_LIVE, EIID_RESULT = 8, 1, 2


def _arg(piid: int, typ: int, raw: bytes) -> bytes:
    return struct.pack("<HH", piid, typ << 12 | len(raw)) + raw


def build_users_action(tid: int, users_json: str, now: int) -> bytes:
    """Genau das Format der Xiaomi-App: op 05, siid 13, aiid 1, 5 Argumente."""
    body = bytes([0x05, SIID_USER, AIID_SET_USERS, 5])
    body += _arg(1, 0x3, struct.pack("<H", 167))   # Konstante aus dem Mitschnitt
    body += _arg(2, 0x1, b"\x01")
    body += _arg(4, 0x5, struct.pack("<I", now))   # Uhrzeit für die Waage
    body += _arg(3, 0x0, b"\x01")
    body += _arg(5, 0xA, users_json.encode())
    total = 4 + len(body)
    return struct.pack("<HH", 0x2 << 12 | total, tid) + body


def users_json(cfg: dict, current_uid: int, last_weights: dict[str, float | None]) -> str:
    ud = []
    for p in cfg["persons"]:
        w = last_weights.get(p["id"]) or p["ref_kg"]
        ud.append({
            "uid": p["scale_uid"], "ut": p["scale_ut"],
            "age": date.today().year - p["birth_year"],
            "sex": 1 if p["sex"] == "m" else 2,
            "hi": p["height_cm"], "wt": round(w * 10),
            "time": p["scale_time"],
        })
    # Die Waage nutzt "hash" offenbar nur als Versionskennung der Profilliste.
    h = hashlib.md5(json.dumps([{k: u[k] for k in ("uid", "ut", "age", "sex", "hi")} for u in ud],
                               separators=(",", ":")).encode()).hexdigest().upper()
    d = {"mid": cfg["scale_mid"], "uid": current_uid, "uc": len(ud), "ow": 0, "hash": h, "ud": ud}
    return json.dumps(d, separators=(",", ":"))


def result_from_fields(fields: list[str]) -> dict | None:
    """Endergebnis der Waage (32 Felder = gespeicherter Datensatz ohne Index)."""
    rec = parse_record("0," + ",".join(f.strip() for f in fields))
    if rec is None:
        return None
    return {"uid": rec.user, "weight": rec.weight_kg, "impedance": rec.impedance_ohm,
            "impedance_low": rec.impedance_low_ohm, "hr": rec.heart_rate, "body": rec.body}


async def weigh_session(address: str, token: bytes, make_users, pick_uid, start_uid: int,
                        timeout: float = 45.0, find_timeout: float = 10.0,
                        idle_timeout: float = 25.0) -> dict | None:
    """Verbindet, meldet sich an, schickt die Profile und wartet auf das Endergebnis.

    Die Waage wertet nur aus, wenn der gemeldete aktuelle Benutzer zum Gewicht passt. Darum
    zuerst start_uid melden und, sobald das Gewicht stabil ist, den per pick_uid(kg) erkannten."""
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: d.address == address or (d.name or "").startswith("Mijia Scale S400"), timeout=find_timeout)
    if dev is None:
        log.info("Waage nicht verbindbar (schläft schon?)")
        return None
    async with BleakClient(dev, timeout=10) as client:
        sess = S400Session(client, token)
        await sess.login()
        log.info("eingeloggt")

        async def send_users(uid: int) -> None:
            await sess.send(build_users_action(sess._tid, make_users(uid), int(time.time())))
            sess._tid += 1
            log.info("Profile gesendet, aktuell uid=%s", uid)

        await send_users(start_uid)
        sent_uid = start_uid
        deadline = asyncio.get_running_loop().time() + timeout
        loop = asyncio.get_running_loop()
        idle_until = loop.time() + idle_timeout  # ohne Gewicht bis dahin: niemand steht drauf
        got_weight = False
        while (left := min(deadline, deadline if got_weight else idle_until) - loop.time()) > 0:
            ev = await sess.next_event(left)
            if ev is None:
                break
            if ev.type == "live" and ev.weight_kg:
                got_weight = True
            if ev.type == "live":
                log.info("live %.1f kg stabil=%s", ev.weight_kg or 0, ev.stable)
                if ev.stable and ev.weight_kg:
                    uid = pick_uid(ev.weight_kg)
                    if uid != sent_uid:
                        await send_users(uid)
                        sent_uid = uid
            elif ev.type == "final":
                res = result_from_fields(ev.fields)
                log.info("Ergebnis der Waage: %s", res)
                return res
            elif ev.type == "action_reply":
                log.info("Antwort der Waage auf Profile: code=%s", ev.miot.code if ev.miot else None)
            else:
                log.info("Ereignis %s %s", ev.type, ev.plaintext.hex())
        log.info("kein Ergebnis (%s), trenne", "Wägung unvollständig" if got_weight else "niemand auf der Waage")
    return None
