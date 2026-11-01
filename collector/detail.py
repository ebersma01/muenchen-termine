"""Detailseiten: fuer Termine ohne Ort oder ohne Preis die Seite des Termins oeffnen und
Ort, Adresse und Kosten auslesen. Ergebnisse werden in app/data/detailcache.json gespeichert
und 14 Tage lang wiederverwendet, damit der taegliche Lauf kurz bleibt."""
import concurrent.futures as cf
import json
import os
import re
import time
from urllib.parse import urldefrag

from bs4 import BeautifulSoup

import extract

MAX_AGE_DAYS = 14
_PRICE = r"(?:ab\s*)?\d{1,3}(?:[,.]\d{1,2})?(?:\s*(?:/|–|-|bis)\s*\d{1,3}(?:[,.]\d{1,2})?)*\s*(?:€|Euro\b|EUR\b)"
_PRICE2 = r"(?:€|EURO\b|EUR\b)\s?\d{1,3}(?:[,.](?:\d{1,2}|-))?(?:\s*(?:/|–|-)\s*(?:€|EURO|EUR)?\s?\d{1,3}(?:[,.](?:\d{1,2}|-))?)*"
_PRICE_RE = re.compile(f"({_PRICE}|{_PRICE2})", re.I)
_PRICE_KW = re.compile(r"\b(Eintritt|Preise?|Kosten|Tickets?|Karten|VVK|Vorverkauf|Abendkasse|Teilnahmegebühr|Gebühr|"
                       r"Teilnahmebeitrag|Beitrag|Kartenpreise?|Admission|Price|Entgelt)\b", re.I)
_FREE_RE = re.compile(r"\b(Eintritt\s*(?:ist\s+)?:?\s*(?:frei|kostenlos|kostenfrei|gratis)|freier Eintritt|ohne Eintritt|"
                      r"free admission|admission(?: is)? free|free entry|Teilnahme(?:\s+ist)?\s*:?\s*(?:kostenlos|kostenfrei|gebührenfrei)|"
                      r"kostenfreie Veranstaltung|kostenlose Veranstaltung|Veranstaltung ist kostenlos|entgeltfrei|"
                      r"(?:Kosten|Preis|Gebühr|Teilnahmegebühr)\s*:\s*(?:keine|kostenlos|kostenfrei|frei|0\s?€))\b", re.I)
_ADDR_RE = re.compile(
    r"([A-ZÄÖÜ][\wäöüß.\-]*(?:[ \-][\wäöüß.\-]+){0,4}?(?:straße|strasse|str\.|platz|weg|allee|ring|gasse|damm|ufer|"
    r"steig|berg|markt|hof|park|anger)\s*\d{1,4}\s?[a-z]?(?:\s*[-–+]\s*\d{1,4}[a-z]?)?)\s*[,|]?\s*(\d{5})\s+"
    r"([A-ZÄÖÜ][\wäöüß.\-]+(?:\s(?:am|an der|im|bei)\s[A-ZÄÖÜ][\wäöüß.\-]+)?)")
_ORT_RE = re.compile(r"(?:^|\s)(?:Ort|Veranstaltungsort|Spielort|Location|Wo|Venue|Treffpunkt|Adresse)\s*:\s*([^\n|]{3,120})", re.I)
_LOC_CLASS = re.compile(r"(location|venue|spielort|veranstaltungsort|event-place|place-name|address|adresse)", re.I)


def _main_text(soup):
    for t in soup(["script", "style", "noscript", "svg", "nav", "iframe", "form", "template"]):
        t.decompose()
    for t in soup.find_all(["header", "footer"]):
        if t.find("nav") or len(list(t.parents)) <= 4 or t.get("role") in ("banner", "contentinfo"):
            t.decompose()
    for t in soup.find_all(class_=re.compile(r"(cookie|newsletter|consent|footer|related|teaser-list|sidebar)", re.I)):
        t.decompose()
    root = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body or soup
    return root


def _cost_from_text(txt, loose=True):
    if _FREE_RE.search(txt):
        return "kostenlos"
    # Preis in der Naehe eines Stichworts ("Eintritt: 15/10 €")
    for m in _PRICE_KW.finditer(txt):
        win = txt[m.start(): m.end() + 140]
        p = _PRICE_RE.search(win)
        if p:
            return _fmt_price(p.group(1))
    if loose:
        # sonst: wenige, eindeutige Preisangaben auf der Seite ("ab 35 €")
        found = list(dict.fromkeys(_fmt_price(m.group(1)) for m in _PRICE_RE.finditer(txt)))
        if 1 <= len(found) <= 3:
            return found[0]
    return None


def _fmt_price(p):
    p = re.sub(r"\s+", " ", p).strip()
    if re.fullmatch(r"0+(?:[,.]0+)?\s*(?:€|EUR|Euro)|€\s?0+(?:[,.]0+)?", p, re.I):
        return "kostenlos"
    p = re.sub(r"\b(EURO|EUR)\b", "€", p, flags=re.I)
    p = re.sub(r"[,.]-", "", p)
    if "€" in p and not p.rstrip().endswith("€"):
        p = p.replace("€", "").strip() + " €"
    p = re.sub(r"\s*€", " €", p)
    p = re.sub(r"\s+", " ", p).strip()
    return p[:40]

def parse_detail(html):
    """-> {"venue": name|None, "address": str|None, "cost": str|None, "lat", "lng"}"""
    out = {"venue": None, "address": None, "cost": None, "lat": None, "lng": None}
    ld = extract.parse_jsonld(html, "")
    if ld:
        d = ld[0]
        v = d.get("venue") or {}
        out["venue"] = v.get("name")
        out["address"] = v.get("address")
        out["lat"], out["lng"] = v.get("lat"), v.get("lng")
        if d.get("cost"):
            out["cost"] = d["cost"]
    soup = BeautifulSoup(html, "lxml")
    whole = soup.body.get_text(" | ", strip=True) if soup.body else ""
    root = _main_text(soup)
    txt = re.sub(r"[ \t\r\f\v]+", " ", root.get_text("\n", strip=True))
    flat = txt.replace("\n", " | ")
    if not out["cost"]:
        out["cost"] = _cost_from_text(flat)
    if not out["cost"] and whole:
        out["cost"] = _cost_from_text(re.sub(r"\s+", " ", whole))
    if not out["venue"]:
        el = root.find(class_=_LOC_CLASS)
        if el:
            t = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            t = re.sub(r"^(Ort|Veranstaltungsort|Location|Spielort|Adresse)\s*:?\s*", "", t, flags=re.I)
            if 2 < len(t) < 140:
                out["venue"] = t
    if not out["venue"]:
        m = _ORT_RE.search(txt)
        if m:
            out["venue"] = m.group(1).strip(" ,.;")
    if not out["address"]:
        src = out["venue"] or ""
        m = _ADDR_RE.search(src) or _ADDR_RE.search(flat)
        if m:
            out["address"] = f"{m.group(1).strip()}, {m.group(2)} {m.group(3)}"
    if out["venue"]:
        v = out["venue"]
        # Adresse aus dem Ortsnamen herausloesen
        if out["address"] and out["address"].split(",")[0] in v:
            v = v.split(out["address"].split(",")[0])[0].strip(" ,|-–")
        v = re.split(r"\s\|\s|\s{2,}", v)[0].strip(" ,")
        out["venue"] = v[:100] or None
    return out


class DetailCache:
    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, url):
        d = self.data.get(url)
        if d and time.time() - d.get("ts", 0) < MAX_AGE_DAYS * 86400:
            return d
        return None

    def put(self, url, d):
        d = dict(d)
        d["ts"] = int(time.time())
        self.data[url] = d

    def save(self):
        cutoff = time.time() - 60 * 86400
        self.data = {k: v for k, v in self.data.items() if v.get("ts", 0) > cutoff}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, self.path)


def needs_detail(e, src_url):
    if not e.get("url"):
        return False
    u = urldefrag(e["url"])[0]
    if u.rstrip("/") == (src_url or "").rstrip("/"):
        return False  # Link zeigt nur auf die Uebersichtsseite
    if re.search(r"\.(pdf|ics|jpg|png)$|eventim\.de|ticketmaster|eventbrite", u, re.I):
        return False
    return e["cost"] in (None, "", "unklar") or not e["venue"].get("name") or not e["venue"].get("address")


def enrich(events, sources, cache, geocoder, max_fetch=900, workers=10, budget_s=900):
    """Fuellt Ort/Adresse/Kosten aus den Detailseiten. Gibt (abgerufen, aus_cache) zurueck."""
    by_src = {s["id"]: s for s in sources}
    todo = {}
    for e in events:
        src = by_src.get(e["source"], {})
        if (src.get("fetch") or {}).get("noDetail"):
            continue
        if needs_detail(e, src.get("url")):
            todo.setdefault(urldefrag(e["url"])[0], []).append(e)
    cached, fetch_urls = {}, []
    for u in todo:
        c = cache.get(u)
        if c is not None:
            cached[u] = c
        else:
            fetch_urls.append(u)
    fetch_urls = fetch_urls[:max_fetch]
    t0 = time.time()

    def work(u):
        if time.time() - t0 > budget_s:
            return u, None
        try:
            return u, parse_detail(extract.fetch(u, timeout=20))
        except Exception:
            return u, {"venue": None, "address": None, "cost": None, "lat": None, "lng": None, "failed": True}

    fetched = {}
    with cf.ThreadPoolExecutor(workers) as ex:
        for u, d in ex.map(work, fetch_urls):
            if d is not None:
                fetched[u] = d
                cache.put(u, d)
    for u, d in {**cached, **fetched}.items():
        for e in todo[u]:
            apply(e, d, geocoder, by_src.get(e["source"], {}))
    return len(fetched), len(cached)


def apply(e, d, geocoder, src):
    from geo import km, MARIENPLATZ, MAX_KM, mentions_mvv, mentions_outside, region_of
    a = d.get("address") or ""
    if a and mentions_outside(a) and not mentions_mvv(a):
        if not (src.get("fetch") or {}).get("venue") and not e["venue"].get("name"):
            e["_drop"] = True  # Termin findet doch ausserhalb statt
        return  # Detailseite gehoert vermutlich zu einem anderen Ort (z. B. Ticketshop einer Tour)
    if e["cost"] in (None, "", "unklar") and d.get("cost"):
        e["cost"] = d["cost"]
    v = e["venue"]
    changed = False
    if d.get("venue") and re.match(r"^[\d(]", d["venue"]):
        d = dict(d, venue=None)
    if not v.get("name") and d.get("venue"):
        v["name"] = d["venue"]
        changed = True
    if not v.get("address") and d.get("address"):
        v["address"] = a
        changed = True
    if d.get("lat") and not v.get("lat"):
        v["lat"], v["lng"] = d["lat"], d["lng"]
    elif changed and (not v.get("lat") or d.get("address")) and v.get("address"):
        q = v["address"] if mentions_mvv(v["address"]) else v["address"] + ", München"
        g = geocoder.lookup(q)
        if g and km((g["lat"], g["lng"]), MARIENPLATZ) < MAX_KM:
            v["lat"], v["lng"] = g["lat"], g["lng"]
    if v.get("lat"):
        e["region"] = region_of(" ".join(x for x in [v.get("name"), v.get("address")] if x), v["lat"], v["lng"])
