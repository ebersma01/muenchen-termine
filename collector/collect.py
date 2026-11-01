#!/usr/bin/env python3
"""Sammler fuer die Muenchner Veranstaltungs-App.

Liest die Quellen aus app/data/sources.json, holt deren Termine und schreibt
  app/data/events.json   (alle Termine fuer die App)
  app/data/status.json   (welche Quelle wie viele Termine geliefert hat, Fehler)

Aufruf:
  python collector/collect.py                 alle aktiven Quellen
  python collector/collect.py --only ifo,badw einzelne Quellen testen (schreibt nichts)
  python collector/collect.py --no-render     Quellen, die einen Browser brauchen, ueberspringen
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import extract  # noqa: E402
from dates import find_dates  # noqa: E402
from geo import Geocoder, MAX_KM, MARIENPLATZ, km, mentions_mvv, mentions_outside, region_of  # noqa: E402

APP = os.path.normpath(os.path.join(HERE, "..", "app"))
DATA = os.environ.get("EVENTS_DATA") or os.path.join(APP, "data")
TZ = ZoneInfo("Europe/Berlin")
DAYS_AHEAD = 180

_FREE = re.compile(r"\b(eintritt frei|freier eintritt|kostenlos|kostenfrei|gebührenfrei|free admission|free of charge|gratis)\b", re.I)
_PRICE = re.compile(r"(\d{1,3}(?:[.,]\d{2})?)\s?(?:€|EUR|Euro)\b|€\s?(\d{1,3}(?:[.,]\d{2})?)", re.I)
_MEMBERS = re.compile(r"(nur für mitglieder|exklusiv für mitglieder|members only|mitgliederversammlung|für mitglieder des)", re.I)
_REGISTER = re.compile(r"\b(anmeldung|anmelden|registrierung|registration|register|voranmeldung)\b", re.I)
_TICKETS = re.compile(r"\b(tickets?|karten|vorverkauf|abendkasse)\b", re.I)
_EXHIB = re.compile(r"(ausstellung|exhibition|schau\b|sammlung|installation|präsentation|kunstverein|museum)", re.I)
_CANCELLED = re.compile(r"(abgesagt|entfällt|cancelled|canceled|ausverkauft\W*abgesagt)", re.I)


def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(name, data):
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- Quellen abrufen
def raw_events(src, allow_render=True):
    f = src.get("fetch") or {}
    kind = f.get("type", "cards")
    urls = f.get("urls") or [f.get("url") or src["url"]]
    render = bool(f.get("render"))
    if render and not allow_render:
        raise RuntimeError("braucht Browser (übersprungen)")
    out = []
    for url in urls:
        if kind == "tribe":
            out += extract.fetch_tribe(url)
            continue
        text = extract.fetch(url, render=render)
        if kind == "ics":
            out += extract.parse_ics(text)
        elif kind == "embedded":
            out += extract.parse_embedded(text, url, f)
        elif kind == "jsonld":
            out += extract.parse_jsonld(text, url)
        elif kind == "auto":
            evs = extract.parse_jsonld(text, url)
            out += evs or extract.parse_cards(text, url, f)
        else:
            out += extract.parse_cards(text, url, f)
    if f.get("detail"):
        add_details(out, f)
    return out


def add_details(evs, f):
    """Fuer Karten ohne Uhrzeit/Ort die Detailseite ansehen (JSON-LD oder Text)."""
    n = 0
    for ev in evs:
        if n >= f.get("detailMax", 25):
            break
        if not ev.get("url") or (not ev["allDay"] and ev["venue"].get("name")):
            continue
        try:
            n += 1
            html = extract.fetch(ev["url"], timeout=25)
        except Exception:
            continue
        ld = extract.parse_jsonld(html, ev["url"])
        if ld:
            d = ld[0]
            if d["start"][:10] == ev["start"][:10] and not d["allDay"]:
                ev["start"], ev["end"], ev["allDay"] = d["start"], d["end"], False
            if not ev["venue"].get("name") and d["venue"].get("name"):
                ev["venue"] = d["venue"]
            if not ev.get("description") and d.get("description"):
                ev["description"] = d["description"]
            continue
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style", "nav", "footer", "header"]):
            t.decompose()
        main = soup.select_one(f.get("detailRoot", "main")) or soup.body
        if not main:
            continue
        txt = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
        if ev["allDay"]:
            i = txt.find(ev["start"][8:10].lstrip("0") + ".")
            t1, t2 = extract.find_time(txt[max(0, i):i + 300] if i >= 0 else txt[:600])
            if t1:
                ev["start"] = f"{ev['start'][:10]}T{t1}"
                ev["end"] = f"{ev['start'][:10]}T{t2}" if t2 else None
                ev["allDay"] = False
        if not ev["venue"].get("name"):
            m = re.search(r"\b(?:Ort|Veranstaltungsort|Location|Wo)\s*:?\s+([^|]{3,90}?)(?:\s{2}|\s(?:Datum|Zeit|Beginn|Uhrzeit|Eintritt|Kosten|Anmeldung)\b|$)", txt)
            if m:
                ev["venue"]["name"] = m.group(1).strip(" ,.")
        if not ev.get("description"):
            ev["description"] = txt[:300]


# --------------------------------------------------------------------------- Aufbereiten
def _kw_hits(text, kws):
    n = 0
    for kw in kws:
        if not kw:
            continue
        if len(kw) <= 3:
            ok = re.search(r"(?<![A-Za-zÄÖÜäöüß-])" + re.escape(kw) + r"(?![A-Za-zÄÖÜäöüß-])", text)
        elif len(kw) >= 7:
            ok = re.search(re.escape(kw), text, re.I)  # auch in Wortzusammensetzungen ("Universitätsgottesdienst")
        else:
            ok = re.search(r"(?<![A-Za-zÄÖÜäöüß])" + re.escape(kw), text, re.I)
        if ok:
            n += 1
    return n


def best_category(title, desc, cats, defaults, keywords_only=False, fallback=None, broad=False):
    """Genau eine Kategorie: Stichwoerter im Titel zaehlen dreifach, in der Beschreibung einfach.
    Die Standardkategorie der Quelle gilt, solange keine andere klar (mind. 3 Punkte) vorne liegt."""
    tscore = {c["id"]: 3 * _kw_hits(title, c.get("keywords", [])) for c in cats}
    scores = {c["id"]: tscore[c["id"]] + _kw_hits(desc, c.get("keywords", [])) for c in cats}
    order = [c["id"] for c in cats]
    best = max(order, key=lambda c: (scores[c], -order.index(c)))
    if keywords_only:
        if scores[best] > 0:
            return best
        return fallback[0] if fallback else None
    defaults = [c for c in defaults if c in scores]
    if not defaults:
        return best if scores[best] > 0 else None
    d = max(defaults, key=lambda c: (scores[c], -defaults.index(c)))
    if best not in defaults and tscore[best] >= 3 and scores[best] >= scores[d] + 3:
        return best
    # breit aufgestellte Haeuser: schon ein Titeltreffer einer anderen Kategorie genuegt
    tbest = max(order, key=lambda c: (tscore[c], scores[c], -order.index(c)))
    if broad and tscore[tbest] >= 3 and tscore[tbest] > tscore[d]:
        return tbest
        return best
    return d


def keyword_cats(text, cats):
    hits = []
    for c in cats:
        for kw in c.get("keywords", []):
            if not kw:
                continue
            if len(kw) <= 3:
                ok = re.search(r"(?<![A-Za-zÄÖÜäöüß])" + re.escape(kw) + r"(?![A-Za-zÄÖÜäöüß])", text)
            else:
                ok = re.search(r"(?<![A-Za-zÄÖÜäöüß])" + re.escape(kw), text, re.I)
            if ok:
                hits.append(c["id"])
                break
    return hits


def guess_cost(text):
    import detail
    return detail._cost_from_text(text, loose=True) or "unklar"


def guess_access(text):
    if _MEMBERS.search(text):
        return "nur Mitglieder"
    if _REGISTER.search(text):
        return "öffentlich, Anmeldung"
    if _TICKETS.search(text):
        return "öffentlich, Tickets"
    return "unklar"


_GENERIC_VENUE = re.compile(r"^(München|Muenchen|Munich|Online|Hybrid|digital|vor ort|tbd|tba|n\.?\s?n\.?|bayern|deutschland)\W*$", re.I)


def finish(ev, src, cats, geocoder, today):
    f = src.get("fetch") or {}
    title = (ev.get("title") or "").strip()
    # Datumsangaben am Titelanfang entfernen ("02.11.2026 Einstiegsseminar ...")
    for _ in range(2):
        ds = find_dates(title[:30])
        if ds and ds[0][3] == 0:
            title = title[ds[0][4]:].strip(" |-–,:")
    title = re.sub(r"^(Mo|Di|Mi|Do|Fr|Sa|So)\.?,?\s+", "", title)
    if len(title) < 3:
        return None
    ev["title"] = title
    blob = " ".join(x for x in [title, ev.get("description") or "", ev["venue"].get("name") or "",
                                ev["venue"].get("address") or "", ev.get("_text") or ""] if x)
    if f.get("include") and not re.search(f["include"], blob, re.I):
        return None
    if f.get("exclude") and re.search(f["exclude"], blob, re.I):
        return None
    if f.get("urlInclude") and not re.search(f["urlInclude"], ev.get("url") or ""):
        return None
    if _CANCELLED.search(title):
        return None
    title = re.sub(r"\s+-\s+\d{1,2}\s+[A-Za-zä]{3}$", "", title)  # "Stück - 1 Nov"
    if f.get("titleStrip"):
        title = re.sub(f["titleStrip"], "", title).strip()
    ev["title"] = title
    # Zeitraum
    s = ev.get("start")
    if not s:
        return None
    try:
        sd = date.fromisoformat(s[:10])
        ed = date.fromisoformat(ev["end"][:10]) if ev.get("end") else sd
    except ValueError:
        return None
    if ev.get("end") and len(ev["end"]) == 10 and len(s) > 10 and ev["end"] == s[:10]:
        ev["end"] = None
    if ev.get("end") and ev["end"] < s[:len(ev["end"])]:
        ev["end"] = None
    # Lange Zeitraeume (> 3 Wochen) nur fuer Ausstellungen; sonst ist es meist eine Terminserie
    if (ed - sd).days > 21 and not f.get("longOk") and not _EXHIB.search(blob):
        return None
    if ed < today or sd > today + timedelta(days=DAYS_AHEAD) or (ed - sd).days > 400:
        return None
    # Ort
    v = ev["venue"]
    dv = f.get("venue") or {}
    if v.get("name") and _GENERIC_VENUE.match(v["name"]):
        if re.match(r"online", v["name"], re.I):
            ev["online"] = True
        v["name"] = None
    if not v.get("name") and dv:
        for k in ("name", "address", "lat", "lng"):
            v[k] = v.get(k) or dv.get(k)
    if dv and v.get("name") and not v.get("address") and dv.get("name") and \
            (dv["name"].lower() in v["name"].lower() or v["name"].lower() in dv["name"].lower()):
        for k in ("address", "lat", "lng"):
            v[k] = v.get(k) or dv.get(k)
    if not v.get("address"):
        # Adresse steht oft nur in Beschreibung oder Kartentext ("Rosenstraße 9, 80331 München")
        import detail
        for txt in (v.get("name") or "", ev.get("description") or "", ev.get("_text") or ""):
            m = detail._ADDR_RE.search(txt)
            if m:
                v["address"] = f"{m.group(1).strip()}, {m.group(2)} {m.group(3)}"
                if v.get("name") and m.group(1) in v["name"]:
                    v["name"] = v["name"].split(m.group(1))[0].strip(" ,|-–") or None
                if not v.get("name"):
                    pre = txt[:m.start()].rstrip(" ,|-–:")
                    cand = re.split(r"[|.:]\s*|\s{2,}", pre)[-1].strip() if pre else ""
                    if 3 <= len(cand) <= 60 and not find_dates(cand):
                        v["name"] = cand
                break
    if v.get("name"):
        v["name"] = re.sub(r"(,\s*(München|Munich))+$", "", v["name"]).strip(" ,") or v["name"]
    loc_text = " ".join(x for x in [v.get("name"), v.get("address")] if x)
    scope = f.get("scope", "local")
    online = bool(ev.get("online")) and not loc_text
    if scope == "filter":
        if not (mentions_mvv(loc_text or blob) or (v.get("lat") and km((v["lat"], v["lng"]), MARIENPLATZ) < MAX_KM)):
            return None
    else:
        out = mentions_outside(loc_text) or (mentions_outside(ev.get("_text", "")) if not loc_text else None)
        if out and not mentions_mvv(loc_text or ev.get("_text", "")):
            return None
    if not v.get("lat") and (v.get("name") or v.get("address")) and not online:
        q = v.get("address") or v["name"]
        if not mentions_mvv(q):
            q += ", München"
        g = geocoder.lookup(q)
        if g:
            v["lat"], v["lng"] = g["lat"], g["lng"]
            if not v.get("address"):
                v["address"] = shorten_addr(g["address"])
    if v.get("lat") and km((v["lat"], v["lng"]), MARIENPLATZ) > MAX_KM:
        if scope == "filter" or not dv:
            return None
        v["lat"] = v["lng"] = None  # vermutlich falsch gefunden
    # Kategorie: genau eine
    fb = list(f.get("fallbackCats", []))
    if f.get("fallbackType") and f["fallbackType"] not in (ev.get("_type") or ""):
        fb = []
    one = best_category(title, ev.get("description") or "", cats, src.get("categories", []),
                        keywords_only=bool(f.get("keywordsOnly")), fallback=fb, broad=bool(f.get("broad")))
    catlist = [one] if one else []
    if not catlist:
        return None
    text_all = blob
    eid = src["id"] + "-" + hashlib.sha1(f"{title.lower()}|{s}".encode()).hexdigest()[:10]
    return {
        "id": eid,
        "title": title,
        "start": s,
        "end": ev.get("end"),
        "allDay": bool(ev.get("allDay")),
        "venue": {"name": v.get("name"), "address": v.get("address"), "lat": v.get("lat"), "lng": v.get("lng")},
        "region": region_of(loc_text, v.get("lat"), v.get("lng")),
        "categories": catlist,
        "description": (ev.get("description") or "").strip()[:300],
        "cost": ev.get("cost") or guess_cost(text_all),
        "access": guess_access(text_all) if not f.get("access") else f["access"],
        "online": bool(ev.get("online")),
        "url": ev.get("url") or src["url"],
        "source": src["id"],
        "checked": datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"),
    }


def shorten_addr(a):
    parts = [p.strip() for p in a.split(",")]
    plz = next((p for p in parts if re.fullmatch(r"\d{5}", p)), None)
    city = "München" if "München" in a else (parts[-4] if len(parts) > 4 else "")
    street = ""
    if len(parts) > 2 and re.match(r"^\d", parts[0]):
        street = f"{parts[1]} {parts[0]}"
    elif len(parts) > 2 and re.match(r"^\d", parts[1]):
        street = f"{parts[2]} {parts[1]}"
    return ", ".join(x for x in [street, f"{plz or ''} {city}".strip()] if x) or None


def norm_key(e):
    t = re.sub(r"[^a-z0-9äöüß]", "", e["title"].lower())[:40]
    return f"{t}|{e['start'][:10]}"


# --------------------------------------------------------------------------- Hauptprogramm
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="nur diese Quellen (Komma-getrennt), nichts schreiben")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--no-detail", action="store_true", help="Detailseiten nicht öffnen")
    ap.add_argument("--show", type=int, default=0, help="bei --only: so viele Termine anzeigen")
    args = ap.parse_args()

    sources = load("sources.json", [])
    cats = load("categories.json", [])
    today = datetime.now(TZ).date()
    geocoder = Geocoder(os.path.join(DATA, "geocache.json"), max_new=int(os.environ.get("GEOCODE_MAX", "300")))
    only = set(args.only.split(",")) if args.only else None
    todo = [s for s in sources if (only and s["id"] in only) or (not only and s.get("active", True) and s.get("fetch") is not False)]

    status = []
    results = {}

    def run(src):
        t0 = time.time()
        try:
            evs = raw_events(src, allow_render=False)
            return src, evs, None, time.time() - t0
        except Exception as e:
            return src, [], e, time.time() - t0

    plain = [s for s in todo if not (s.get("fetch") or {}).get("render")]
    rendered = [s for s in todo if (s.get("fetch") or {}).get("render")]
    with cf.ThreadPoolExecutor(8) as ex:
        for src, evs, err, dt in ex.map(run, plain):
            results[src["id"]] = (src, evs, err)
    for src in rendered:  # Browser-Quellen nacheinander
        if args.no_render:
            results[src["id"]] = (src, [], RuntimeError("braucht Browser (übersprungen)"))
            continue
        try:
            results[src["id"]] = (src, raw_events(src), None)
        except Exception as e:
            results[src["id"]] = (src, [], e)
    extract.close_browser()

    all_events = {}
    for src in todo:
        src_, evs, err = results[src["id"]]
        kept = []
        for ev in evs:
            try:
                fe = finish(ev, src, cats, geocoder, today)
            except Exception:
                traceback.print_exc()
                fe = None
            if fe:
                kept.append(fe)
        # doppelte innerhalb der Quelle
        seen = {}
        for e in kept:
            seen.setdefault(norm_key(e), e)
        kept = list(seen.values())[: (src.get("fetch") or {}).get("maxEvents", 150)]
        st = {"id": src["id"], "name": src["name"], "found": len(evs), "events": len(kept),
              "ok": err is None and len(kept) > 0, "error": None}
        if err is not None:
            st["error"] = f"{type(err).__name__}: {str(err)[:160]}"
        elif not evs:
            st["error"] = "keine Termine erkannt"
        elif not kept:
            st["error"] = "Termine gefunden, aber keiner passt (Zeitraum/Ort/Kategorie)"
        status.append(st)
        print(f"{'OK ' if st['ok'] else '-- '} {src['id']:<22} gefunden {len(evs):>4}  übernommen {len(kept):>4}  {st['error'] or ''}", flush=True)
        if only and args.show:
            for e in kept[: args.show]:
                print(f"      {e['start']:<16} {e['title'][:70]} | {e['venue']['name'] or '-'} | {','.join(e['categories'])}")
        for e in kept:
            k = norm_key(e)
            if k in all_events:
                old = all_events[k]
                if not old["venue"].get("lat") and e["venue"].get("lat"):
                    old["venue"] = e["venue"]
            else:
                all_events[k] = e
    if not args.no_detail:
        import detail
        cache = detail.DetailCache(os.path.join(DATA, "detailcache.json"))
        t0 = time.time()
        n_new, n_cached = detail.enrich(list(all_events.values()), sources, cache, geocoder,
                                        max_fetch=int(os.environ.get("DETAIL_MAX", "900")))
        all_events = {k: e for k, e in all_events.items() if not e.pop("_drop", False)}
        cache.save()
        print(f"Detailseiten: {n_new} neu geladen, {n_cached} aus dem Zwischenspeicher ({time.time() - t0:.0f} s)", flush=True)
        if only and args.show:
            for e in list(all_events.values())[: args.show * 3]:
                print(f"      {e['title'][:45]:<45} | {(e['venue']['name'] or '-')[:30]} | {(e['venue']['address'] or '-')[:35]} | {e['cost']}")
    for e in all_events.values():
        v = e["venue"]
        if v.get("address"):
            v["address"] = re.sub(r"((?:München|Munich))(?:,\s*(?:München|Munich|Deutschland|Germany|Bayern))+$", r"\1", v["address"])
        if not v.get("name") and not v.get("address") and e.get("online"):
            v["name"] = "Online"
    geocoder.save()
    if only:
        return

    manual = load("manual-events.json", {"events": []}).get("events", [])
    events = [e for e in manual if (e.get("end") or e["start"])[:10] >= today.isoformat()] + list(all_events.values())
    events.sort(key=lambda e: (e["start"], e["title"]))
    now = datetime.now(TZ).isoformat(timespec="seconds")
    write_json("events.json", {"generated": now, "note": "Automatisch gesammelt", "events": events})
    write_json("status.json", {"generated": now, "total": len(events),
                               "sources": sorted(status, key=lambda s: (s["ok"], s["id"]))})
    print(f"\n{len(events)} Termine aus {sum(1 for s in status if s['ok'])} von {len(status)} Quellen geschrieben.")


if __name__ == "__main__":
    main()
