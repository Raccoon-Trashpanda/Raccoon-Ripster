"""Verify tools/widevine/device.wvd against SoundCloud's Widevine license server.

Does a real challenge/response against a known DRM (ctr/CENC) SoundCloud track
and prints the CONTENT key on success. A pass means the .wvd is valid and not
revoked. Reuses the production handshake from sc_widevine_runner.py.

  python verify_wvd.py [soundcloud_url]
"""
import asyncio, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import sc_widevine_runner as R  # noqa: E402
import httpx  # noqa: E402

DEFAULT_URL = "https://soundcloud.com/proff/sets/missing-places-ive-never-been"
DEV = ROOT / "tools" / "widevine" / "device.wvd"


def _oauth() -> str:
    for line in (ROOT / "config.yaml").open(encoding="utf-8"):
        if line.strip().startswith("soundcloud-oauth-token:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


async def main(url: str) -> int:
    if not DEV.is_file():
        print("FAIL: tools/widevine/device.wvd not found — extract one first.")
        return 1
    oauth = _oauth()
    label, tracks = await R._resolve_input_url(url, oauth)
    t = tracks[0]
    print(f"track: {t['title']} ({label})")
    timeout = httpx.Timeout(connect=5, read=15, write=15, pool=15)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        cid = await R._scrape_client_id(c)
        stream = await R._resolve_track_stream(c, t["id"], cid, oauth)
        if not stream or not stream.get("license_token"):
            print("FAIL: track is not DRM / no license_token (pick a Go+ ctr track).")
            return 1
        m = await c.get(stream["url"])
    parsed = R._parse_m3u8(m.text)
    kid, key = await R._widevine_key(oauth, parsed["pssh_b64"],
                                     stream["license_token"], DEV)
    print(f"\n  OK — CONTENT key obtained: {kid}:{key}")
    print("  device.wvd is VALID against SoundCloud (not revoked).")
    return 0


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    try:
        sys.exit(asyncio.run(main(u)))
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        sys.exit(1)
