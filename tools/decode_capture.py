"""Entschlüsselt einen iPhone-Bluetooth-Mitschnitt (PacketLogger .pklg) zwischen Xiaomi Home und der S400.

Aufruf: .venv/bin/python tools/decode_capture.py <datei.pklg> [token-hex]
(ohne token-hex wird er aus dem Schlüsselbund gelesen: Dienst waage-bridge, Konto token)

Ablauf: ATT-Pakete aus dem HCI-Mitschnitt holen, Mi-Home-v2-Login finden (app_rand/dev_rand),
Sitzungsschlüssel per HKDF aus dem Token ableiten (wie snelstim/ha-xiaomi-s400, Apache-2.0),
dann alle verschlüsselten Pakete in beide Richtungen entschlüsseln und als MIoT-Nachricht ausgeben.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from collections import defaultdict

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.hmac import HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ATT_NAMES = {0x12: "WriteReq", 0x52: "WriteCmd", 0x1B: "Notify", 0x1D: "Indicate", 0x0B: "ReadRsp", 0x0A: "ReadReq"}


# ------------------------------------------------------------------ pklg -> ATT

def read_pklg(path):
    """PacketLogger: [len u32 BE][ts u64][type u8][payload]; len zählt ts+type+payload."""
    data = open(path, "rb").read()
    pos = 0
    while pos + 13 <= len(data):
        (ln,) = struct.unpack_from(">I", data, pos)
        if ln < 9 or pos + 4 + ln > len(data):
            break
        ts_hi, ts_lo = struct.unpack_from(">II", data, pos + 4)
        typ = data[pos + 12]
        yield ts_hi + ts_lo / 1e6, typ, data[pos + 13 : pos + 4 + ln]
        pos += 4 + ln


def att_packets(path):
    """Gibt (zeit, richtung, conn_handle, att_bytes) zurück. Setzt L2CAP-Fragmente zusammen."""
    partial = {}
    for ts, typ, p in read_pklg(path):
        if typ not in (0x02, 0x03) or len(p) < 4:  # ACL gesendet / empfangen
            continue
        hdr, _ = struct.unpack_from("<HH", p)
        conn, pb = hdr & 0x0FFF, (hdr >> 12) & 0x3
        body = p[4:]
        key = (typ, conn)
        if pb == 0x1 and key in partial:  # Fortsetzung
            partial[key][1] += body
        else:
            partial[key] = [ts, bytearray(body)]
        buf = partial[key][1]
        if len(buf) >= 4:
            l2len, cid = struct.unpack_from("<HH", buf)
            if len(buf) >= 4 + l2len:
                if cid == 0x0004:
                    yield partial[key][0], "app→waage" if typ == 0x02 else "waage→app", conn, bytes(buf[4 : 4 + l2len])
                del partial[key]


# ------------------------------------------------------------------ Mi-Home-v2

def hkdf_keys(token, app_rand, dev_rand):
    d = HKDF(algorithm=hashes.SHA256(), length=64, salt=app_rand + dev_rand, info=b"mible-login-info").derive(token)
    return {"dev_key": d[0:16], "app_key": d[16:32], "dev_iv": d[32:36], "app_iv": d[36:40]}


def hmac256(k, m):
    h = HMAC(k, hashes.SHA256()); h.update(m); return h.finalize()


def decrypt(keys, direction, raw):
    if len(raw) < 6:
        return None
    k, iv = (keys["dev_key"], keys["dev_iv"]) if direction == "waage→app" else (keys["app_key"], keys["app_iv"])
    try:
        return AESCCM(k, tag_length=4).decrypt(iv + bytes(4) + raw[:2] + bytes(2), raw[2:], None)
    except Exception:
        return None


def miot(pt: bytes) -> str:
    if len(pt) < 5 or pt[1] >> 4 != 0x2:
        return ""
    _, tid, op = struct.unpack_from("<HHB", pt)
    names = {2: "get_props", 3: "get_props_reply", 4: "set_props", 5: "action", 6: "action_reply", 7: "event", 8: "set_props_reply"}
    out = f"  MIoT tid={tid} op={names.get(op, hex(op))}"
    try:
        if op in (5, 7):
            siid, iid, argc = struct.unpack_from("<BHB", pt, 5)
            out += f" siid={siid} {'aiid' if op == 5 else 'eiid'}={iid}"
            pos = 9
            for _ in range(argc):
                piid, tl = struct.unpack_from("<HH", pt, pos); pos += 4
                v = pt[pos : pos + (tl & 0xFFF)]; pos += tl & 0xFFF
                out += f"\n      piid={piid} typ={tl >> 12} wert={v.decode('ascii', 'replace') if tl >> 12 == 0xA else v.hex()}"
        elif op in (2, 4):
            out += f" body={pt[5:].hex()}"
    except struct.error:
        pass
    return out


def main():
    path = sys.argv[1]
    if len(sys.argv) > 2:
        token = bytes.fromhex(sys.argv[2])
    else:
        token = bytes.fromhex(subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "waage-bridge",
                                              "-a", "token", "-w"], capture_output=True, text=True).stdout.strip())
    pkts = [(ts, d, c, a) for ts, d, c, a in att_packets(path) if a[0] in ATT_NAMES and len(a) >= 3]
    print(f"{len(pkts)} ATT-Pakete")

    # Login-Verbindungen und Handles am Inhalt erkennen: App schreibt 24000000 auf UPNP
    for i, (ts, d, conn, a) in enumerate(pkts):
        if d == "app→waage" and a[0] in (0x12, 0x52) and a[3:] == bytes.fromhex("24000000"):
            print(f"\n=== Login auf Verbindung {conn:#x} um {ts:.3f} ===")
            decode_session(pkts[i:], conn, token)


def decode_session(pkts, conn, token):
    # Rahmen auf den Handles sammeln: Parcel = [frame u16 LE][daten]; Kopf "000000xx yy nn nn" kündigt an
    parcels = defaultdict(bytearray)
    expect = {}
    msgs = []  # (ts, richtung, handle, bytes) komplette Nachrichten
    for ts, d, c, a in pkts:
        if c != conn:
            continue
        h = struct.unpack_from("<H", a, 1)[0]
        v = a[3:]
        key = (d, h)
        if len(v) == 6 and v[:3] == b"\x00\x00\x00":  # Ankündigung: typ + anzahl rahmen
            expect[key] = struct.unpack_from("<H", v, 4)[0]
            parcels[key] = bytearray()
            msgs.append((ts, d, h, "ankündigung", v))
            continue
        if key in expect and len(v) >= 2:
            frame = struct.unpack_from("<H", v)[0]
            parcels[key] += v[2:]
            if frame >= expect[key]:
                msgs.append((ts, d, h, "parcel", bytes(parcels[key])))
                del expect[key]
            continue
        msgs.append((ts, d, h, "roh", v))

    keys = None
    app_rand = dev_rand = None
    for ts, d, h, kind, v in msgs:
        if kind == "parcel" and len(v) == 16 and d == "app→waage" and app_rand is None:
            app_rand = v
        elif kind == "parcel" and len(v) == 16 and d == "waage→app" and dev_rand is None:
            dev_rand = v
        elif kind == "parcel" and len(v) == 32 and d == "waage→app" and app_rand and dev_rand and keys is None:
            k = hkdf_keys(token, app_rand, dev_rand)
            ok = v == hmac256(k["dev_key"], dev_rand + app_rand)
            print(f"  Sitzungsschlüssel abgeleitet, Token {'PASST' if ok else 'passt NICHT'}")
            keys = k
        line = f"{ts:12.3f} {d} h={h:#06x} {kind:11s} {v.hex()}"
        if keys and kind in ("parcel", "roh") and len(v) > 6:
            pt = decrypt(keys, d, v)
            if pt is not None:
                line += f"\n      klartext: {pt.hex()}  {pt.decode('ascii', 'replace')!r}" + miot(pt)
        print(line)


if __name__ == "__main__":
    main()
