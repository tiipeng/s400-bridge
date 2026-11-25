"""Baut einen künstlichen pklg-Mitschnitt mit Mi-Home-v2-Login und prüft decode_capture."""
import io, os, struct, sys, contextlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import decode_capture as dc
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

TOKEN = bytes.fromhex("0123456789abcdef01234567")  # erfundener Test-Token
recs = []
def acl(direction, handle, value, op=0x52):
    att = bytes([op]) + struct.pack("<H", handle) + value
    l2 = struct.pack("<HH", len(att), 4) + att
    p = struct.pack("<HH", 0x0040 | (0x2 << 12), len(l2)) + l2
    typ = 0x02 if direction == "a" else 0x03
    body = struct.pack(">II", 1000, len(recs)) + bytes([typ]) + p
    recs.append(struct.pack(">I", len(body)) + body)
def parcel(direction, handle, data, kind):
    n = (len(data) + 17) // 18
    acl(direction, handle, b"\x00\x00\x00" + bytes([kind]) + struct.pack("<H", n))
    for i in range(n):
        acl(direction, handle, struct.pack("<H", i + 1) + data[i*18:(i+1)*18], 0x1B if direction == "w" else 0x52)

UPNP, AVDTP, V1A, V1B = 0x10, 0x19, 0x1A, 0x1B
app_rand, dev_rand = os.urandom(16), os.urandom(16)
acl("a", UPNP, bytes.fromhex("24000000"))
parcel("a", AVDTP, app_rand, 0x0b)
parcel("w", AVDTP, dev_rand, 0x0d)
k = dc.hkdf_keys(TOKEN, app_rand, dev_rand)
parcel("w", AVDTP, dc.hmac256(k["dev_key"], dev_rand + app_rand), 0x0c)
pt = bytes.fromhex("1220010005") + b"profil-test"
enc = b"\x01\x00" + AESCCM(k["app_key"], tag_length=4).encrypt(k["app_iv"] + bytes(4) + b"\x01\x00" + bytes(2), pt, None)
parcel("a", V1A, enc, 0x00)
f = Path(os.environ.get("TMPDIR", "/tmp")) / "test.pklg"
f.write_bytes(b"".join(recs))
sys.argv = ["x", str(f), TOKEN.hex()]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    dc.main()
out = buf.getvalue()
assert "Token PASST" in out, out
assert "profil-test" in out, out
print("OK")
