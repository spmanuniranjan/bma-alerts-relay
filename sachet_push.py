#!/usr/bin/env python3
"""
BMA LIFE CARE - official alert relay (Kerala).

Every INTERVAL seconds (default 60):
  1. reads the official NDMA SACHET Kerala RSS feed,
  2. downloads each NEW CAP message,
  3. works out which Kerala districts it names (from the official area text),
  4. sends it, UNCHANGED, as an FCM data message to the topic  kl_<district>.
The app decides how to show it. This script never edits severity, text or area.

Usage:
  python sachet_push.py --once --dry-run          # look at the feed, send nothing
  python sachet_push.py --test-push idukki        # send a clearly-marked TEST alarm to topic kl_idukki
  python sachet_push.py                           # run forever (use systemd, see README)

Needs:  pip install firebase-admin      and a Firebase service-account key:
  export GOOGLE_APPLICATION_CREDENTIALS=/path/serviceAccountKey.json
"""
import argparse, json, logging, os, re, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

FEED = os.environ.get("BMA_FEED", "https://sachet.ndma.gov.in/cap_public_website/rss/rss_kerala.xml")
CAP = "https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier="
STATE_FILE = os.environ.get("BMA_STATE", "seen.json")
INTERVAL = int(os.environ.get("BMA_INTERVAL", "60"))
UA = "BMALifeCare-relay/0.6 (disaster alert relay; contact: set-your-email)"
MAX_AGE_MIN = 180          # do not push alerts older than 3 hours

DISTRICTS = {
    "thiruvananthapuram": ["thiruvananthapuram", "trivandrum"],
    "kollam": ["kollam", "quilon"],
    "pathanamthitta": ["pathanamthitta"],
    "alappuzha": ["alappuzha", "alleppey"],
    "kottayam": ["kottayam"],
    "idukki": ["idukki"],
    "ernakulam": ["ernakulam"],
    "thrissur": ["thrissur", "trichur"],
    "palakkad": ["palakkad", "palghat"],
    "malappuram": ["malappuram"],
    "kozhikode": ["kozhikode", "calicut"],
    "wayanad": ["wayanad"],
    "kannur": ["kannur", "cannanore"],
    "kasaragod": ["kasaragod", "kasargod", "kasaragode"],
}

log = logging.getLogger("bma")


class NotModified(Exception):
    pass


def http_get(url, timeout=15, etag=None):
    """Returns (body, etag). NDMA asks integrators to use ETag caching, so the feed is fetched conditionally."""
    headers = {"User-Agent": UA}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), r.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            raise NotModified()
        raise


def local(tag):
    return tag.split("}")[-1].split(":")[-1]


def kids(el, name):
    return [c for c in el if local(c.tag) == name]


def text_of(el, name):
    for c in kids(el, name):
        return (c.text or "").strip()
    return ""


def parse_rss(data):
    root = ET.fromstring(data)
    items = []
    for it in root.iter():
        if local(it.tag) == "item":
            items.append({n: text_of(it, n) for n in ("title", "link", "guid", "description")})
    return items


ID_RES = [re.compile(r"identifier=(\d+)"), re.compile(r"IN-(\d+)_")]


def cap_id(item):
    for field in ("link", "guid", "description"):
        for rx in ID_RES:
            m = rx.search(item.get(field, ""))
            if m:
                return m.group(1)
    return None


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_cap(data):
    root = ET.fromstring(data)
    a = {k: text_of(root, k) for k in ("identifier", "sender", "sent", "status", "msgType", "references")}
    infos = kids(root, "info")
    info = None
    for i in infos:
        if text_of(i, "language").lower().startswith("en"):
            info = i
            break
    if info is None and infos:
        info = infos[0]
    f = {}
    if info is not None:
        for k in ("event", "urgency", "severity", "certainty", "effective", "expires", "headline", "description", "instruction"):
            f[k] = text_of(info, k)
        areas = [text_of(ar, "areaDesc") for ar in kids(info, "area")]
        f["area"] = ", ".join(x for x in areas if x)
    a.update({k: f.get(k, "") for k in ("event", "urgency", "severity", "certainty", "effective", "expires", "headline", "description", "instruction", "area")})
    return a


def districts_for(area):
    low = (area or "").lower()
    return [slug for slug, names in DISTRICTS.items() if any(n in low for n in names)]


def ref_ids(references):
    out = []
    for tok in (references or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            out.append(parts[1])
    return out


def cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def payload(a, cap_digits):
    """FCM data limit is 4 KB: long free-text is shortened; the app links to the full official CAP message."""
    return {
        "type": "alert",
        "id": a["identifier"], "sender": a["sender"], "sent": a["sent"], "status": a["status"] or "Actual",
        "msgType": a["msgType"] or "Alert", "references": a["references"],
        "event": cut(a["event"], 80), "urgency": a["urgency"], "severity": a["severity"], "certainty": a["certainty"],
        "effective": a["effective"], "expires": a["expires"],
        "headline": cut(a["headline"], 300), "description": cut(a["description"], 1200),
        "instruction": cut(a["instruction"], 600), "area": cut(a["area"], 400),
        "link": (CAP + cap_digits) if cap_digits else "",
    }


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    s.setdefault("seen", [])
    s.setdefault("routes", {})
    return s


def save_state(s):
    s["seen"] = s["seen"][-1500:]
    if len(s["routes"]) > 500:
        for k in list(s["routes"])[:-500]:
            s["routes"].pop(k, None)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


_fb = {"ok": False}


def send(topic, data, dry):
    if dry:
        log.info("DRY-RUN would send to %s: %s | %s | %s", topic, data.get("severity"), data.get("event"), data.get("area"))
        return
    if not _fb["ok"]:
        import firebase_admin
        from firebase_admin import credentials
        firebase_admin.initialize_app(credentials.ApplicationDefault())
        _fb["ok"] = True
    from firebase_admin import messaging
    msg = messaging.Message(
        topic=topic, data=data,
        android=messaging.AndroidConfig(priority="high", ttl=timedelta(minutes=30)),
    )
    log.info("sent to %s -> %s", topic, messaging.send(msg))


def process(a, digits, state, dry):
    now = datetime.now(timezone.utc)
    if a["status"] != "Actual":
        return 0
    refs = ref_ids(a["references"])
    mtype = (a["msgType"] or "").lower()
    if mtype == "cancel":
        slugs = sorted({s for r in refs for s in state["routes"].get(r, [])}) or list(DISTRICTS)
    else:
        sent = parse_iso(a["sent"])
        exp = parse_iso(a["expires"])
        if exp and exp < now:
            return 0
        if sent and now - sent > timedelta(minutes=MAX_AGE_MIN):
            return 0
        slugs = districts_for(a["area"])
        if not slugs:
            log.info("area not clear, not pushed: %s | %s", a["identifier"], a["area"])
            return 0
        state["routes"][a["identifier"]] = slugs
    data = payload(a, digits)
    for slug in slugs:
        send("kl_" + slug, data, dry)
    return len(slugs)


def cycle(state, dry):
    use_etag = None if state.get("retry") else state.get("etag")
    try:
        data, new_etag = http_get(FEED, etag=use_etag)
    except NotModified:
        log.info("feed unchanged (304)")
        return 0, 0
    items = parse_rss(data)
    new = 0
    failed = False
    for it in items[:80]:
        d = cap_id(it)
        if not d or d in state["seen"]:
            continue
        try:
            a = parse_cap(http_get(CAP + d)[0])
        except Exception as e:  # network / XML problem: try again next cycle
            failed = True
            log.warning("CAP %s failed: %s", d, e)
            continue
        state["seen"].append(d)
        new += process(a, d, state, dry)
    state["retry"] = failed
    if new_etag and not failed:
        state["etag"] = new_etag
    save_state(state)
    return len(items), new


def test_push(slug, dry):
    if slug not in DISTRICTS:
        sys.exit("unknown district slug: " + slug)
    now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    a = {
        "identifier": "TEST-%d" % int(time.time()), "sender": "BMA TEST", "sent": now.isoformat(timespec="seconds"),
        "status": "Actual", "msgType": "Alert", "references": "", "event": "TEST ALERT", "urgency": "Immediate",
        "severity": "Extreme", "certainty": "Observed", "effective": now.isoformat(timespec="seconds"),
        "expires": (now + timedelta(minutes=10)).isoformat(timespec="seconds"),
        "headline": "ഇത് പരീക്ഷണ അലർട്ട് മാത്രമാണ്. യഥാർഥ ദുരന്തമല്ല.", "description": "Server-to-phone push test.",
        "instruction": "", "area": slug.capitalize(),
    }
    send("kl_" + slug, payload(a, ""), dry)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-push", metavar="DISTRICT_SLUG")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.test_push:
        test_push(args.test_push, args.dry_run)
        return
    state = load_state()
    first = not state["seen"]
    if first:
        log.info("first run: existing feed items will be remembered; only alerts from the last %d min are pushed", MAX_AGE_MIN)
    while True:
        try:
            n_items, n_sent = cycle(state, args.dry_run)
            log.info("feed items: %d, pushes sent: %d", n_items, n_sent)
        except Exception as e:
            log.error("cycle failed: %s", e)
        if args.once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
