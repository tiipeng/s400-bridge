# s400-bridge

Use the **Xiaomi Mijia Body Composition Scale S400** (`MJTZC01YM`) without the Xiaomi Home app,
for a household of several people, with the data going to **Apple Health** (and optionally Home Assistant).

A Mac near the scale listens to its encrypted Bluetooth broadcast (weight + impedance, decoded with
[`xiaomi-ble`](https://github.com/Bluetooth-Devices/xiaomi-ble)), assigns each weigh-in to a person by weight,
estimates body fat / lean mass / BMI with the well-known Mi Fit formula, and

- stores it in SQLite,
- writes a small JSON file per weigh-in to iCloud Drive (`Waage/<Name>/neu/`),
- optionally updates Home Assistant sensors and sends a push notification.

An iOS Shortcut on each person's iPhone picks up the JSON files and logs weight, body fat %,
lean body mass and BMI into Apple Health (with the original timestamp).

### Optional: body fat on the scale display ("weigh twice")

`gatt.py` can talk to the scale like the Xiaomi app does: Mi Home v2 login with the device token,
send the user profiles, tell the scale who is standing on it. The scale then computes body composition
itself, **shows body fat on its display** and returns the full result (fat, muscle, water, BMR, …).

Catch: from sleep, the S400 only starts advertising when the measurement is already finished, so you can't be
connected for the first weigh-in, and staying connected permanently would drain the batteries. With
`"gatt_second_weighing": true` the bridge connects right after a weigh-in (the scale is awake for ~2 s) and waits
up to 90 s: **step off and on again**, and the second weigh-in shows body fat on the display. The scale's result
then replaces the first (formula-based) measurement. If nobody steps on again, the first measurement is kept.

> Code comments and log messages are in German. Tested with one S400 (firmware `2.1.1_0057`) on macOS 26.
> The profile message was rebuilt from a Bluetooth capture of the Xiaomi app. It works for us, but it is
> reverse-engineered and could break with a firmware update.

## How it is built

| Part | What it does |
|---|---|
| `blescan/` | Tiny Swift program with an embedded `Info.plist`. macOS only grants Bluetooth to a program that declares it, so this binary owns the permission, scans for `0xFE95` adverts and starts `bridge.py` as a child (which inherits the permission). |
| `bridge.py` | Main loop: broadcast decoding, person matching, body-fat estimate, storage, iCloud/HA export. |
| `gatt.py` | Experimental Bluetooth session with the scale: login, user profiles, live weight, scale-computed result. |
| `s400/` | Mi Home v2 login and MIoT framing, vendored from [snelstim/ha-xiaomi-s400](https://github.com/snelstim/ha-xiaomi-s400) (Apache-2.0). |
| `tools/decode_capture.py` | Decrypts an iPhone Bluetooth capture (`.pklg`) between the Xiaomi app and the scale, given the token. |

## Setup

You need a Mac within Bluetooth range of the scale, Python 3.12, the Xcode command line tools, and the scale
paired once in Xiaomi Home (to get its keys). Don't reset or re-pair the scale afterwards, or the keys change.

1. **Get the keys** with [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)
   (QR login works). For the scale you need `TOKEN` (24 hex), `BLE KEY` (32 hex) and `MAC`.
2. **Store them in the macOS keychain** (service `waage-bridge`):
   ```bash
   security add-generic-password -U -s waage-bridge -a token   -w "<TOKEN>"
   security add-generic-password -U -s waage-bridge -a bindkey -w "<BLE KEY>"
   # optional, Home Assistant long-lived token (paste via clipboard; the interactive prompt cuts at 128 chars):
   security add-generic-password -U -s waage-bridge -a ha_token -w "$(pbpaste)"
   ```
3. **Install**:
   ```bash
   python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
   cd blescan && swiftc -O main.swift -o blescan -Xlinker -sectcreate -Xlinker __TEXT \
       -Xlinker __info_plist -Xlinker Info.plist && codesign -s - -f blescan && cd ..
   cp config.example.json config.json   # then edit it
   ```
4. **Configure** `config.json`: `scale_mac`; one entry per person with `sex` (`m`/`f`), `height_cm`,
   `birth_year` and a rough `ref_kg` for the first match. `scale_mid` is your Xiaomi account ID.
   `scale_uid`/`scale_ut`/`scale_time` are the user slots the scale knows. Ours were `3`/`1` and `4`/`2`,
   as the Xiaomi app had assigned them; other values are untested. If unsure, capture what your app sends (below).
   `scale_ble_id` (the macOS Bluetooth UUID of the scale) is optional; the device name is used as fallback.
5. **Run as a launch agent**: copy `launchd.plist.example` to `~/Library/LaunchAgents/local.s400-bridge.plist`,
   fix the paths, then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.s400-bridge.plist`.
   Allow Bluetooth when macOS asks (System Settings → Privacy & Security → Bluetooth).
6. **iPhone Shortcut "Waage Health"** (one per person): *Get Contents of Folder* (iCloud Drive/Waage/&lt;Name&gt;/neu) →
   *Repeat with Each* → *Get Dictionary from Input* → get values `datum`, `weight`, `fat`, `lean`, `bmi` →
   *Log Health Sample* (Weight, Body Fat Percentage, Lean Body Mass, Body Mass Index) with date = `datum` →
   *Delete Files* (confirmation off). Trigger it with personal automations (time of day, charger disconnected).
   Share each person's folder via iCloud Drive (collaborate, can make changes) so everyone sees only their own data.

## Capturing what your Xiaomi app sends

Install Apple's Bluetooth logging profile on the iPhone (developer.apple.com → Bug Reporting → Profiles and Logs),
weigh yourself with the Xiaomi app open, create a sysdiagnose, AirDrop it to the Mac and run:

```bash
.venv/bin/python tools/decode_capture.py logs/Bluetooth/bluetoothd-hci-latest.pklg <TOKEN>
```

It prints the decrypted login, the user-profile message (`siid=13 aiid=1`) and the scale's result (`siid=8 eiid=2`).

## Tests

```bash
for t in tests/test_*.py; do .venv/bin/python "$t"; done
```

## Credits and license

Apache-2.0, see `LICENSE` and `NOTICE`. The Mi Home v2 login code comes from
[snelstim/ha-xiaomi-s400](https://github.com/snelstim/ha-xiaomi-s400), which credits nokistin/xiaomi-s400-live and
[dnandha/miauth](https://github.com/dnandha/miauth). Result field map from the
[Home Assistant community thread](https://community.home-assistant.io/t/662778).
Not affiliated with Xiaomi.
