"""Leser fuer die verschiedenen Quellformate: iCal, JSON-LD, WordPress-Kalender (Tribe) und
eine allgemeine HTML-Erkennung, die wiederkehrende "Termin-Karten" auf einer Seite findet."""
import html as htmllib
import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from dates import find_dates, find_time, parse_iso

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 MuenchenEventsBot/1.0",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

_browser = None


def fetch(url, render=False, timeout=40):
    """Seite laden. render=True nutzt einen unsichtbaren Browser (fuer Seiten, die erst per
    JavaScript ihre Termine nachladen)."""
    if render:
        return _render(url)
    for attempt in range(2):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            return r.text
        except requests.RequestException:
            if attempt:
                raise
    return ""


def _render(url):
    global _browser
    from playwright.sync_api import sync_playwright
    if _browser is None:
        pw = sync_playwright().start()
        try:
            _browser = pw.chromium.launch()
        except Exception:
            _browser = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium")
    page = _browser.new_page(locale="de-DE", user_agent=UA["User-Agent"])
    try:
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        return page.content()
    finally:
        page.close()


def close_browser():
    global _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None


def clean(s):
    s = htmllib.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _dt_fields(start, end, has_time):
    """Werte fuer start/end/allDay im App-Format."""
    def fmt(x):
        if x is None:
            return None
        if isinstance(x, datetime):
            return x.strftime("%Y-%m-%dT%H:%M") if has_time else x.strftime("%Y-%m-%d")
        return x.isoformat()
    return fmt(start), fmt(end), not has_time


# --------------------------------------------------------------------------- iCal
def parse_ics(text):
    from zoneinfo import ZoneInfo
    text = re.sub(r"\r?\n[ \t]", "", text)
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S):
        props = {}
        for line in block.strip().splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            name, *params = k.split(";")
            props.setdefault(name.upper(), (params, v))

        def val(n):
            return htmllib.unescape(props.get(n, ([], ""))[1].replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip())

        def dt(n):
            if n not in props:
                return None, False
            params, v = props[n]
            v = v.strip()
            if "VALUE=DATE" in params or len(v) == 8:
                return datetime.strptime(v[:8], "%Y%m%d").date(), False
            d = datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
            tz = next((p.split("=", 1)[1] for p in params if p.startswith("TZID=")), None)
            if v.endswith("Z"):
                d = d.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
            elif tz:
                try:
                    d = d.replace(tzinfo=ZoneInfo(tz.strip('"'))).astimezone(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
                except Exception:
                    pass
            return d, True

        st, has_t = dt("DTSTART")
        en, _ = dt("DTEND")
        if st is None:
            continue
        if not has_t and en and isinstance(en, date) and not isinstance(en, datetime):
            en = en - timedelta(days=1)  # iCal-Ende ist exklusiv
            if en <= st:
                en = None
        s, e, allday = _dt_fields(st, en, has_t)
        loc = val("LOCATION")
        geo = val("GEO")
        lat = lng = None
        if geo and ";" in geo:
            try:
                lat, lng = map(float, geo.split(";"))
            except ValueError:
                pass
        out.append({
            "title": val("SUMMARY"), "start": s, "end": e, "allDay": allday,
            "venue": {"name": loc.split(",")[0].strip() if loc else None,
                      "address": loc or None, "lat": lat, "lng": lng},
            "description": clean(val("DESCRIPTION"))[:400], "url": val("URL") or None,
        })
    return out


# --------------------------------------------------------------------------- JSON-LD
def _ld_items(soup):
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                t = x.get("@type")
                t = " ".join(t) if isinstance(t, list) else str(t or "")
                if t.endswith("Event") or "Event" in t.split():
                    yield x
                for k in ("@graph", "itemListElement", "item", "subEvent", "event", "events"):
                    if k in x:
                        stack.append(x[k])


def parse_jsonld(html_text, base_url):
    soup = BeautifulSoup(html_text, "lxml")
    out = []
    for it in _ld_items(soup):
        st, has_t = parse_iso(it.get("startDate"))
        if not st:
            continue
        en, _ = parse_iso(it.get("endDate"))
        if en and isinstance(st, datetime) and isinstance(en, datetime) and en.date() == st.date() and not has_t:
            en = None
        s, e, allday = _dt_fields(st, en, has_t)
        if e == s:
            e = None
        loc = it.get("location") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        name = addr = None
        lat = lng = None
        online = "Online" in str(it.get("eventAttendanceMode", ""))
        if isinstance(loc, dict):
            if loc.get("@type") == "VirtualLocation":
                online = True
            name = clean(loc.get("name") or "") or None
            a = loc.get("address")
            if isinstance(a, dict):
                addr = ", ".join(x for x in [a.get("streetAddress"), " ".join(
                    x for x in [a.get("postalCode"), a.get("addressLocality")] if x)] if x) or None
            elif isinstance(a, str):
                addr = clean(a) or None
            g = loc.get("geo") or {}
            try:
                lat, lng = float(g.get("latitude")), float(g.get("longitude"))
            except (TypeError, ValueError):
                pass
        elif isinstance(loc, str):
            name = clean(loc)
        offers = it.get("offers")
        cost = None
        if isinstance(offers, list) and offers:
            offers = offers[0]
        if isinstance(offers, dict) and offers.get("price") not in (None, ""):
            p = str(offers.get("price")).strip()
            if re.fullmatch(r"0+(?:[.,]0+)?", p) or re.search(r"frei|free|kostenlos|gratis", p, re.I):
                cost = "kostenlos"
            elif re.search(r"\d", p):
                cost = f"{p} {offers.get('priceCurrency', '€')}".replace("EUR", "€")
        url = it.get("url")
        if isinstance(url, list):
            url = url[0] if url else None
        out.append({
            "title": clean(it.get("name")), "start": s, "end": e, "allDay": allday,
            "venue": {"name": name, "address": addr, "lat": lat, "lng": lng},
            "description": clean(it.get("description"))[:400],
            "url": urljoin(base_url, url) if url else None, "cost": cost, "online": online,
            "_type": str(it.get("@type")),
        })
    return out


# --------------------------------------------------------------------------- WordPress "The Events Calendar"
def fetch_tribe(base, pages=3):
    out = []
    url = base.rstrip("/") + "/wp-json/tribe/events/v1/events?per_page=50&start_date=" + date.today().isoformat()
    for _ in range(pages):
        txt = fetch(url)
        data = json.loads(txt)
        for ev in data.get("events", []):
            allday = bool(ev.get("all_day"))
            st = ev.get("start_date", "")
            en = ev.get("end_date", "")
            s = st[:10] if allday else st[:16].replace(" ", "T")
            e = en[:10] if allday else en[:16].replace(" ", "T")
            if e and s and e[:10] == s[:10] and not allday:
                e = e  # Endzeit am selben Tag behalten
            v = ev.get("venue") or {}
            if isinstance(v, list):
                v = v[0] if v else {}
            addr = ", ".join(x for x in [v.get("address"), " ".join(x for x in [v.get("zip"), v.get("city")] if x)] if x)
            lat = v.get("geo_lat")
            lng = v.get("geo_lng")
            out.append({
                "title": clean(ev.get("title")), "start": s, "end": e if e != s else None, "allDay": allday,
                "venue": {"name": clean(v.get("venue")) or None, "address": addr or None,
                          "lat": float(lat) if lat else None, "lng": float(lng) if lng else None},
                "description": clean(ev.get("excerpt") or ev.get("description"))[:400],
                "url": ev.get("url"), "cost": clean(ev.get("cost")) or None,
            })
        nxt = data.get("next_rest_url")
        if not nxt:
            break
        url = nxt
    return out


# --------------------------------------------------------------------------- allgemeine HTML-Karten
_STOP_TITLES = re.compile(
    r"^(mehr|mehr infos?|mehr erfahren|details|tickets?|info|infos|weiterlesen|read more|learn more|"
    r"zum termin|jetzt anmelden|anmelden|anmeldung|informationen und tickets|erzähl mir mehr|"
    r"zur veranstaltung|veranstaltung|event|termin|online event|get your ticket|karten|buchen|"
    r"merken|teilen|kalender|ics|ical|outlook|google|zum kalender hinzufügen|tagestipp|highlight|neu)\b\W*$", re.I)
_DROP_TAGS = ["script", "style", "noscript", "svg", "nav", "select", "iframe", "template"]
_LOC_CLASS = re.compile(r"(location|venue|place|ort\b|-ort|ort-|where|address|adresse|spielort|room|raum)", re.I)
_TITLE_CLASS = re.compile(r"(?<!sub)(?<!sub-)(?<!sub_)(title|titel|headline|heading|subject|ueberschrift)", re.I)
_ONLINE = re.compile(r"\b(online|webinar|livestream|virtuell|digital via|zoom|hybrid)\b", re.I)
_DATEISH = re.compile(r"^[\W\d]*(mo|di|mi|do|fr|sa|so|mon|tue|wed|thu|fri|sat|sun|montag|dienstag|mittwoch|donnerstag|"
                      r"freitag|samstag|sonntag|heute|morgen|ab|bis|uhr|\d|[.:/–\-|•]|\s|"
                      r"jan\w*|feb\w*|mär\w*|mar\w*|apr\w*|mai|may|jun\w*|jul\w*|aug\w*|sep\w*|okt\w*|oct\w*|nov\w*|dez\w*|dec\w*)*$", re.I)


def _text(el):
    return re.sub(r"\s+", " ", el.get_text(" | ", strip=True)).replace(" | | ", " | ")


_PLACE_ONLY = re.compile(r"^(München|Muenchen|Munich|Online|Hybrid|Garching|Freising|Tutzing|Berlin|vor Ort|Präsenz|Webinar)$", re.I)


_STATUS_ONLY = re.compile(r"^(verlegt|ausverkauft|sold out|abgesagt|neuer termin|zusatz(?:show|konzert|termin)|restkarten|"
                          r"wenige tickets|hochverlegt|upgrade|new date|postponed)\b", re.I)


def _is_title(t):
    t = t.strip(" |·-–")
    return not _STATUS_ONLY.match(t) and not _PLACE_ONLY.match(t) and len(t) >= 4 and sum(c.isalpha() for c in t) >= 4 and not _STOP_TITLES.match(t) and not _DATEISH.match(t)


def _page_year(soup):
    head = " ".join(x.get_text(" ") for x in soup.find_all(["title", "h1"])[:3])
    ys = set(re.findall(r"\b(20\d\d)\b", head))
    return int(ys.pop()) if len(ys) == 1 else None


def _sel_match(el, sel):
    try:
        return el.css.match(sel)
    except AttributeError:
        import soupsieve
        return soupsieve.match(sel, el)


def parse_cards(html_text, base_url, cfg=None):
    """Findet Termine auf einer normalen Webseite. cfg kann CSS-Selektoren vorgeben:
    card (ein Element pro Termin), title, date, location, link."""
    cfg = cfg or {}
    soup = BeautifulSoup(html_text, "lxml")
    year = cfg.get("year") or _page_year(soup)
    for t in soup(_DROP_TAGS):
        t.decompose()
    # Seitenkopf/-fuss entfernen, aber <header> innerhalb von Termin-Karten behalten
    for t in soup.find_all(["header", "footer"]):
        if t.find("nav") or len(list(t.parents)) <= 4 or t.get("role") in ("banner", "contentinfo") \
                or "site" in " ".join(t.get("class", [])) or t.get("id") in ("header", "footer", "site-header"):
            t.decompose()
    root = soup.select_one(cfg["root"]) if cfg.get("root") else (soup.find("main") or soup.body or soup)
    if root is None:
        return []
    if cfg.get("card") and cfg.get("monthHeader"):
        # Spielplaene mit Monatsueberschrift ("November 2026") und nur Tageszahl im Termin
        cards, month, day = [], "", ""
        sel = cfg["monthHeader"] + ", " + cfg["card"] + (", " + cfg["dayHeader"] if cfg.get("dayHeader") else "")
        for el in root.select(sel):
            if _sel_match(el, cfg["monthHeader"]):
                month = _text(el)
            elif cfg.get("dayHeader") and _sel_match(el, cfg["dayHeader"]):
                day = re.sub(r"\D", "", _text(el))[:2]
            elif _sel_match(el, cfg["card"]):
                el["data-month"] = month
                if day:
                    el["data-day"] = day
                cards.append(el)
    elif cfg.get("card"):
        cards = root.select(cfg["card"])
    else:
        cards = _auto_cards(root, year)
    out = []
    for c in cards:
        ev = _card_to_event(c, base_url, cfg, year)
        if ev:
            out.append(ev)
    return out


def _count_dates(el, year):
    return len(find_dates(el.get_text(" ", strip=True)[:4000], year=year))


def _auto_cards(root, year):
    """Kleinste wiederkehrende Bloecke, die genau eine Datumsangabe (oder einen Zeitraum) enthalten."""
    seen = set()
    cards = []
    for s in root.find_all(string=True):
        if not s.strip() or len(s) > 400:
            continue
        if not find_dates(s, year=year):
            continue
        el = s.parent
        n0 = _count_dates(el, year)
        if n0 == 0:
            # Datum ueber mehrere Elemente verteilt ("01. | November | 2026")
            pass
        card = el
        for _ in range(8):
            p = card.parent
            if p is None or p is root or p.name in ("body", "html", "main"):
                break
            if _count_dates(p, year) > max(1, _count_dates(card, year)) and card.find("a", href=True) \
                    and len(card.get_text(" ", strip=True)) > 25:
                break
            if _count_dates(p, year) > 2:
                break
            card = p
        if id(card) in seen:
            continue
        seen.add(id(card))
        cards.append(card)
    # Verschachtelte Karten: aeussere entfernen, wenn innere Karte schon alles abdeckt
    ids = {id(c) for c in cards}
    result = []
    for c in cards:
        if any(id(p) in ids for p in c.parents):
            continue
        result.append(c)
    return result


def _card_to_event(card, base_url, cfg, year):
    if cfg.get("drop"):
        for x in card.select(cfg["drop"]):
            x.decompose()
    text = _text(card)
    if len(text) < 12:
        return None
    dsrc = text
    if cfg.get("date"):
        d_el = card.select_one(cfg["date"])
        if d_el:
            dsrc = _text(d_el)

    if card.get("data-month"):
        dd = card.get("data-day") or re.sub(r"\D", "", dsrc)[:2]
        dsrc = dd + ". " + card["data-month"]
    ds = find_dates(dsrc, year=year)
    if not ds:
        return None
    start, end, _, ds_s, ds_e = ds[0]
    if end is None and len(ds) > 1 and cfg.get("secondDateIsEnd", True):
        # "Sonntag, 01. November 2026 - Freitag, 06. November 2026" mit Text dazwischen
        between = dsrc[ds_e:ds[1][3]]
        if len(between) < 25 and re.search(r"[-–—]|bis", between):
            end = ds[1][0]
    t1, t2 = find_time(dsrc[ds_s:] if cfg.get("date") else text[ds_s:ds_s + 200]) if True else (None, None)
    if cfg.get("time"):
        tm = card.select_one(cfg["time"])
        if tm:
            t1, t2 = find_time(re.sub(r"\b(\d{1,2})\.(\d{2})\b", r"\1:\2", _text(tm)) + " Uhr")
    if not t1:
        t1, t2 = find_time(text)
    # Titel
    title = None
    if cfg.get("title"):
        el = card.select_one(cfg["title"])
        if el:
            title = _text(el) or el.get("alt") or el.get("title")
            if title and cfg.get("titleFirstLine"):
                title = next((t.strip() for t in el.stripped_strings if _is_title(t.strip())), title)
    if not title:
        heads = [(int(el.name[1]), i, _text(el)) for i, el in enumerate(card.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]))]
        heads = [h for h in heads if _is_title(h[2])]
        if heads:
            title = min(heads)[2]
    if not title:
        for el in card.find_all(class_=_TITLE_CLASS):
            t = _text(el)
            if _is_title(t) and len(t) < 200:
                title = t
                break
    if not title:
        links = [(_text(a), a) for a in card.find_all("a")]
        links = [t for t, a in links if _is_title(t)]
        if links:
            title = max(links, key=len) if len(links) < 4 else links[0]
    if not title:
        for el in card.find_all(["strong", "b"]):
            t = _text(el)
            if _is_title(t):
                title = t
                break
    if not title:
        parts = [p for p in text.split(" | ") if _is_title(p)]
        if parts:
            title = parts[0]
    if not title:
        return None
    if cfg.get("titleRegex"):
        m = re.search(cfg["titleRegex"], text)
        if m:
            title = m.group(1) if m.groups() else m.group(0)
    if cfg.get("titlePrefix"):
        title = cfg["titlePrefix"] + title
    title = re.sub(r"\s*\|\s*", " ", title).strip(" -–|")
    if len(title) > 180:
        title = title[:177] + "…"
    # Link
    link = None
    if cfg.get("link"):
        a = card.select_one(cfg["link"])
        link = a.get("href") if a else None
    if not link:
        if card.name == "a" and card.get("href"):
            link = card["href"]
    if not link:
        best = None
        for a in card.find_all("a", href=True):
            h = a["href"]
            if h.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            if any(x in h for x in ("readspeaker", "facebook.com", "twitter.com", "instagram.com", "linkedin.com", "/share")):
                continue
            if title and title[:20].lower() in _text(a).lower():
                best = h
                break
            best = best or h
        link = best
    if not link and card.get("data-href"):
        link = card["data-href"]
    url = urljoin(base_url, link) if link else base_url
    # Ort
    venue = None
    if cfg.get("location"):
        el = card.select_one(cfg["location"])
        if el:
            venue = _text(el)
    if not venue and cfg.get("locationRegex"):
        m = re.search(cfg["locationRegex"], text)
        if m:
            venue = m.group(1)
    if not venue:
        m = re.search(r"\b(?:Ort|Location|Veranstaltungsort|Wo)\s*:\s*\|?\s*([^|]{3,80})", text)
        if m:
            venue = m.group(1)
    if not venue:
        for el in card.find_all(class_=_LOC_CLASS):
            t = _text(el)
            if 2 < len(t) < 120 and not find_dates(t):
                venue = t
                break
    if venue:
        venue = re.split(r",\s*(?:ab\s)?\d{1,2}[.:]\d{2}|,\s*\d{1,2}\s?Uhr|\s\|\s", venue)[0]
        venue = re.sub(r"^(Ort|Location|Wo)\s*:\s*", "", venue).strip(" ,|") or None
    # Beschreibung = restlicher Text
    desc = text
    for x in (title, venue or ""):
        if x:
            desc = desc.replace(x, " ")
    desc = " ".join(p for p in desc.split(" | ") if len(p) > 25 and not _STOP_TITLES.match(p.strip()))
    desc = re.sub(r"\s+", " ", desc).strip()[:300]
    has_time = bool(t1) and (end is None or end == start)
    if has_time:
        s = f"{start.isoformat()}T{t1}"
        e = f"{start.isoformat()}T{t2}" if t2 else None
    else:
        s = start.isoformat()
        e = end.isoformat() if end and end != start else None
    return {
        "title": title, "start": s, "end": e, "allDay": not has_time,
        "venue": {"name": venue, "address": None, "lat": None, "lng": None},
        "description": desc, "url": url, "online": bool(_ONLINE.search(text)),
        "_text": text,
    }


# --------------------------------------------------------------------------- eingebettete Daten (z. B. Next.js-Seiten)
def parse_embedded(html_text, base_url, cfg):
    """Termine, die als JSON-Objekte im Seitenquelltext stehen (z. B. {"slug":..,"title":..,"startDay":..}).
    cfg: urlTemplate ("https://…/events/{slug}"), Feldnamen optional."""
    t = html_text.replace('\\"', '"')
    k_title = cfg.get("titleKey", "title")
    k_start = cfg.get("startKey", "startDay")
    out, seen = [], set()
    dec = json.JSONDecoder()
    for m in re.finditer(r'\{"slug":"', t):
        try:
            obj, _ = dec.raw_decode(t, m.start())
        except ValueError:
            continue
        if not isinstance(obj, dict) or not obj.get(k_title) or not obj.get(k_start):
            continue
        key = (obj.get("slug"), obj.get(k_start))
        if key in seen:
            continue
        seen.add(key)
        sd = str(obj[k_start])[:10]
        ed = obj.get(cfg.get("endKey", "endDay"))
        ed = str(ed)[:10] if ed and not str(ed).startswith("$") else None
        t1, t2 = find_time(str(obj.get("time") or ""))
        if t1 and not ed:
            s_, e_, allday = f"{sd}T{t1}", (f"{sd}T{t2}" if t2 else None), False
        else:
            s_, e_, allday = sd, (ed if ed and ed != sd else None), True
        loc = obj.get("location")
        loc = clean(loc) if isinstance(loc, str) and not loc.startswith("$") else None
        url = cfg.get("urlTemplate", "").format(**{k: v for k, v in obj.items() if isinstance(v, str)}) or base_url
        out.append({
            "title": clean(obj[k_title]), "start": s_, "end": e_, "allDay": allday,
            "venue": {"name": loc, "address": None, "lat": None, "lng": None},
            "description": clean(obj.get("description"))[:300], "url": url,
            "online": bool(loc and re.search(r"online|webinar", loc, re.I)),
            "_text": " ".join(str(v) for v in obj.values() if isinstance(v, str)),
        })
    return out
