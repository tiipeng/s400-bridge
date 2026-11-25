"""Einmal-Test: verbindet sich mit der Waage, meldet zuerst die erste Person, korrigiert bei stabilem Gewicht
auf die passende Person und wartet auf das Ergebnis. Start nur über blescan (Bluetooth-Freigabe)."""
import asyncio, logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge, gatt
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
cfg = bridge.load_config()
store = bridge.Store(bridge.HERE / "waage.db")
token = bytes.fromhex(bridge.keychain("token"))
weights = {p["id"]: store.last_weight(p["id"]) for p in cfg["persons"]}
mk = lambda uid: gatt.users_json(cfg, uid, weights)
pick = lambda kg: bridge.pick_person(cfg, store, kg)["scale_uid"]
res = asyncio.run(gatt.weigh_session(cfg["scale_ble_id"], token, mk, pick, cfg["persons"][0]["scale_uid"], timeout=90, find_timeout=150))
print("ERGEBNIS:", res, flush=True)
