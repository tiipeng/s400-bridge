"""Simuliert verschlüsselte S400-Pakete (MiBeacon v5) mit einem Test-Schlüssel."""
import json, os, struct, sys
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
from xiaomi_ble import XiaomiBluetoothDeviceData

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
MAC_REV = bytes.fromhex("665544332211")  # erfundene MAC 11:22:33:44:55:66
PID = struct.pack("<H", 0x30D9)

def packet(counter, mass_dkg, hr, imp_dohm, profile=1, with_mac=False):
    data = mass_dkg | (hr << 11) | (imp_dohm << 18)
    obj = b"\x16\x6e\x09" + struct.pack("<BII", profile, data, 0)
    ext = os.urandom(3)
    nonce = MAC_REV + PID + bytes([counter]) + ext
    enc = AESCCM(KEY, tag_length=4).encrypt(nonce, obj, b"\x11")
    # echte S400-Messpakete tragen keine MAC (Frame-Flag 0x10 fehlt)
    if with_mac:
        frame = b"\x58\x59" + PID + bytes([counter]) + MAC_REV + enc[:-4] + ext + enc[-4:]
    else:
        frame = b"\x48\x59" + PID + bytes([counter]) + enc[:-4] + ext + enc[-4:]
    return json.dumps({"id": "x", "rssi": -60, "name": "Mijia Scale S400", "fe95": frame.hex()})

out = []
meas = bridge.Measurement(lambda *a: out.append(a))
bridge.hook_s400(meas)

def run(seq, key=KEY):
    out.clear(); meas.reset()
    dev = XiaomiBluetoothDeviceData(key)
    for line in seq:
        dev.update(bridge.to_service_info(line, "11:22:33:44:55:66"))
    return list(out)

seq = [packet(1, 731, 0, 0), packet(2, 732, 20, 5123), packet(3, 0, 0, 4801), packet(4, 0, 0, 0)]
assert run(seq) == [(73.2, 512.3, 70)], out
assert run(seq[:3] + [seq[2], seq[3]]) == [(73.2, 512.3, 70)], out          # Abschluss doppelt
seq2 = [packet(5, 531, 30, 6001), packet(6, 0, 0, 5500), packet(7, 0, 0, 0)]
assert run(seq + seq2) == [(73.2, 512.3, 70), (53.1, 600.1, 80)], out      # zwei Personen
assert run(seq[:3] + seq2) == [(73.2, 512.3, 70), (53.1, 600.1, 80)], out  # ohne Absteige-Paket
assert run([packet(8, 530, 0, 0), packet(9, 0, 0, 0)]) == [(53.0, None, None)], out  # Socken, absteigen
run([packet(10, 530, 0, 0)]); meas.last_ts -= 11; meas.tick()               # Socken, Timeout
assert out == [(53.0, None, None)], out
assert run(seq, key=bytes(16)) == [], out                                   # falscher Schlüssel
assert run([packet(11, 732, 20, 5123, with_mac=True), packet(12, 0, 0, 4801, with_mac=True)]) == [(73.2, 512.3, 70)], out
assert bridge.to_service_info(packet(13, 700, 0, 0)) is None                # ohne MAC und ohne config                                   # falscher Schlüssel

print("m:", bridge.metrics({"height_cm": 175, "birth_year": 1990, "sex": "m"}, 73.2, 512.3, 70))
print("f:", bridge.metrics({"height_cm": 162, "birth_year": 1995, "sex": "f"}, 53.1, 600.1, 80))
print("OK")
