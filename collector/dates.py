"""Datums- und Uhrzeit-Erkennung fuer deutsche und englische Veranstaltungstexte."""
import re
from datetime import date, datetime, timedelta

MONTHS = {
    "jan": 1, "januar": 1, "january": 1, "jänner": 1,
    "feb": 2, "februar": 2, "february": 2,
    "mär": 3, "mar": 3, "märz": 3, "maerz": 3, "march": 3, "mrz": 3,
    "apr": 4, "april": 4,
    "mai": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oct": 10, "oktober": 10, "october": 10,
    "nov": 11, "november": 11,
    "dez": 12, "dec": 12, "dezember": 12, "december": 12,
}
_MON = r"(Januar|Jänner|Jan|Februar|Feb|März|Maerz|Mär|Mrz|Mar|March|April|Apr|Mai|May|Juni|June|Jun|Juli|July|Jul|August|Aug|September|Sept|Sep|Oktober|October|Okt|Oct|November|Nov|Dezember|December|Dez|Dec|January|February)"

# Jede Regel liefert (tag, monat, jahr|None) ueber benannte Gruppen
_PATTERNS = [
    # 2026-11-04
    re.compile(r"(?<!\d)(?P<y>20\d\d)-(?P<m>\d\d)-(?P<d>\d\d)(?!\d)"),
    # 04.11.2026 / 4.11.26 / 4.11. / DI•03•11•26
    re.compile(r"(?<![\d.])(?P<d>[0-3]?\d)\s?[.•/]\s?(?P<m>[01]?\d)\s?(?:[.•/]\s?(?P<y>(?:20)?\d\d)(?![\d:])|\.)(?!\d)"),
    # 4. November 2026 / 4 Nov / 04. Nov.
    re.compile(r"(?<!\d)(?P<d>[0-3]?\d)(?:\.|st|nd|rd|th)?\s*(?:de\s)?" + _MON + r"\b\.?(?:,?\s*(?P<y>20\d\d))?", re.I),
    # November 18-19, 2025 (nur Starttag)
    re.compile(_MON + r"\.?\s+(?P<d>[0-3]?\d)\s*[-–]\s*[0-3]?\d,?\s*(?P<y>20\d\d)", re.I),
    # November 4, 2026 / Nov 4th
    re.compile(_MON + r"\.?\s+(?P<d>[0-3]?\d)(?:st|nd|rd|th)?\b(?:,?\s*(?P<y>20\d\d))?", re.I),
]
_RANGE_SEP = re.compile(r"^[\s,|]*(?:Uhr)?[\s,|]*(?:[-–—]|bis|to|until)\s*(?:[A-Za-zÄÖÜäöü]{2,10}\.?,?\s*)?$", re.I)
_DAYONLY_RANGE = re.compile(r"(?<![\d.])(?P<d1>[0-3]?\d)\.?\s*(?:[-–—&]|bis|und|and)\s*$")

_TIME = re.compile(
    r"(?<![\d.:])(?P<h>[0-2]?\d)(?:(?:[:.]|••)(?P<mi>[0-5]\d))?\s*"
    r"(?P<suf>Uhr|h\b|p\.\s?m\.|pm\b|a\.\s?m\.|am\b)?"
    r"(?:\s*(?:[-–—]|bis)\s*(?P<h2>[0-2]?\d)(?:(?:[:.])(?P<mi2>[0-5]\d))?\s*(?:Uhr|h\b)?)?", re.I)


def _mk(d, m, y, ref):
    try:
        d, m = int(d), int(m)
        if y is None:
            y = ref.year
            cand = date(y, m, d)
            if cand < ref - timedelta(days=60):
                nxt = date(y + 1, m, d)
                if (nxt - ref).days < 240:
                    cand = nxt
            return cand
        y = int(y)
        if y < 100:
            y += 2000
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def pi_of(hit):
    return len(hit) > 5 and hit[5] in (2, 4)


def find_dates(text, ref=None, year=None):
    """Alle Datumsangaben im Text: Liste von (start, ende, hatte_jahr, pos_start, pos_ende)."""
    ref = ref or date.today()
    hits = []
    for pi, pat in enumerate(_PATTERNS):
        for m in pat.finditer(text):
            g = m.groupdict()
            mon = g.get("m")
            if mon is None:
                word = next((x for x in m.groups() if x and x.lower().rstrip(".") in MONTHS), None)
                if not word:
                    continue
                mon = MONTHS[word.lower().rstrip(".")]
            if pi == 1:  # numerisch
                # "19.30" (Uhrzeit) ausschliessen: ohne Jahr nur mit Punkt danach
                if int(mon) > 12:
                    continue
            hits.append([m.start(), m.end(), g["d"], mon, g.get("y"), pi])
    # Ueberlappungen entfernen (laengster Treffer gewinnt)
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    clean = []
    for h in hits:
        if clean and h[0] < clean[-1][1]:
            continue
        clean.append(h)
    # Jahr von nachfolgendem Datum uebernehmen ("30. Okt – 1. Nov 2026")
    for i in range(len(clean) - 1, -1, -1):
        if clean[i][4] is None and i + 1 < len(clean) and clean[i + 1][4] and clean[i + 1][0] - clean[i][1] < 6:
            clean[i][4] = clean[i + 1][4]
    out = []
    i = 0
    while i < len(clean):
        s, e, d, mo, y, _pi = clean[i]
        had_year = y is not None
        if y is None and year:
            y = year
        start = _mk(d, mo, y, ref)
        end = None
        # Bereich "4. - 6. Nov 2026"
        dm = _DAYONLY_RANGE.search(text[max(0, s - 10):s]) if pi_of(clean[i]) else None
        if dm and start:
            st = _mk(dm.group("d1"), start.month, start.year, ref)
            if st and st <= start:
                start, end = st, start
                s = s - (len(text[max(0, s - 10):s]) - dm.start())
        if i + 1 < len(clean) and _RANGE_SEP.match(text[e:clean[i + 1][0]]):
            s2, e2, d2, mo2, y2, _ = clean[i + 1]
            end = _mk(d2, mo2, y2 or y, ref)
            if start and end and end < start and y is None:
                start = _mk(d, mo, (end.year - 1), ref)
            e = e2
            i += 1
        if start:
            out.append((start, end, had_year, s, e))
        i += 1
    # "bis 31.12.2026" / "until ..." ohne Startdatum
    return out


def find_time(text):
    """Erste plausible Uhrzeit (hh:mm) und ggf. Endzeit im Text."""
    for m in _TIME.finditer(text):
        h = int(m.group("h"))
        mi = m.group("mi")
        suf = (m.group("suf") or "").lower().replace(" ", "")
        if mi is None and not suf:
            continue
        if suf in ("h",) and mi is None:
            continue
        # Datumsfragmente wie "04.11" ausschliessen: danach direkt ein Punkt + Ziffer
        tail = text[m.end():m.end() + 3]
        if mi and not suf and re.match(r"\.\s?\d", text[m.end() - 3 + 3:m.end() + 2] if False else tail):
            continue
        if suf.startswith("p") and h < 12:
            h += 12
        if h > 23:
            continue
        mi = int(mi or 0)
        # "04.11" ohne Uhr: unsicher, nur akzeptieren wenn Doppelpunkt
        raw = m.group(0)
        if "." in raw and not suf and ":" not in raw:
            continue
        t = f"{h:02d}:{mi:02d}"
        t2 = None
        if m.group("h2"):
            h2 = int(m.group("h2"))
            if h2 <= 23:
                t2 = f"{h2:02d}:{int(m.group('mi2') or 0):02d}"
        return t, t2
    return None, None


def parse_iso(s):
    """ISO-Zeitstempel -> (datetime naiv in Europe/Berlin oder date, hat_zeit)."""
    from zoneinfo import ZoneInfo
    if not s:
        return None, False
    s = s.strip()
    try:
        if len(s) <= 10:
            return date.fromisoformat(s[:10]), False
        if re.search(r"[T ]00:00(:00)?(\.0+)?(Z|[+-]\d\d:?\d\d)?$", s):
            return date.fromisoformat(s[:10]), False
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
        return dt, True
    except ValueError:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S"), True
        except ValueError:
            return None, False
