"""Prüft gatt.py gegen das Nachrichtenformat aus einem iPhone-Bluetooth-Mitschnitt der Xiaomi-App.
Alle Werte (Konto-ID, Profile, Messwerte) sind erfunden; nur der Aufbau stammt aus dem Mitschnitt."""
import json, struct, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge, gatt

# --- Profil-Aktion: Byte-Aufbau wie von der App gesendet (op 05, siid 13, aiid 1, 5 Argumente)
js = '{"mid":"1234567890","uid":3,"uc":1,"ow":0,"hash":"00","ud":[{"uid":3,"ut":1,"age":30,"sex":1,"hi":180,"wt":800,"time":1700000000}]}'
ts = 0x6B066751
body = (bytes.fromhex("050d0105" "01000230a700" "0200011001" "04000450") + struct.pack("<I", ts)
        + bytes.fromhex("0300010001" "0500") + struct.pack("<H", 0xA000 | len(js)) + js.encode())
expected = struct.pack("<HH", 0x2000 | (4 + len(body)), 11) + body
assert gatt.build_users_action(11, js, ts) == expected

# --- Endergebnis der Waage (32 Felder): erfundene Werte
RESULT = "1234567890,3,1,800,60,0,1700000100,200,600,550,33,8,190,280,250,700,-6,-40,-34,12,1700,0,30,82,750,41,160,640,440,152,5000,4800"
r = gatt.result_from_fields(RESULT.split(","))
assert r["uid"] == 3 and r["weight"] == 80.0 and r["impedance"] == 500.0 and r["impedance_low"] == 480.0 and r["hr"] == 60, r
b = r["body"]
assert b["fat_percent"] == 20.0 and b["bmi"] == 25.0 and b["fat_free_mass_kg"] == 64.0 and b["bmr_kcal_day"] == 1700, b

# --- Profil-JSON aus config
cfg = {"scale_mid": "1234567890", "persons": [
    {"id": "a", "scale_uid": 3, "scale_ut": 1, "birth_year": 1990, "sex": "m", "height_cm": 180, "ref_kg": 80, "scale_time": 1700000000},
    {"id": "b", "scale_uid": 4, "scale_ut": 2, "birth_year": 1995, "sex": "f", "height_cm": 165, "ref_kg": 60, "scale_time": 1700000001}]}
j = json.loads(gatt.users_json(cfg, 4, {"a": 80.4, "b": None}))
assert j["uid"] == 4 and j["uc"] == 2 and j["mid"] == "1234567890"
assert j["ud"][0] == {"uid": 3, "ut": 1, "age": j["ud"][0]["age"], "sex": 1, "hi": 180, "wt": 804, "time": 1700000000}
assert j["ud"][1]["sex"] == 2 and j["ud"][1]["wt"] == 600  # ohne Messung: ref_kg
print("OK")

# --- Umwandlung in unser Format
m = bridge.from_scale(r)
assert m["weight"] == 80.0 and m["fat"] == 20.0 and m["lean"] == 64.0 and m["bmi"] == 25.0 and m["impedance"] == 480.0 and m["hr"] == 60, m
assert m["body"]["bmr_kcal_day"] == 1700
socks = bridge.from_scale({"uid": 3, "weight": 73.0, "impedance": None, "impedance_low": None, "hr": None, "body": {"bmi": 23.0, "source": "scale"}})
assert socks == {"weight": 73.0, "bmi": 23.0, "body": {"bmi": 23.0, "source": "scale"}}, socks
print("OK from_scale")
