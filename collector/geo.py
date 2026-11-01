"""Orte: Koordinaten suchen (OpenStreetMap/Nominatim, mit Zwischenspeicher) und pruefen, ob ein
Termin im MVV-Gebiet (etwa 45 km um den Marienplatz) liegt."""
import json
import math
import os
import re
import time

import requests

MARIENPLATZ = (48.1374, 11.5755)
MAX_KM = 48

# Orte im MVV-Gebiet (Auswahl). Treffer im Text => Termin gilt als "in der Gegend".
MVV_PLACES = [
    "München", "Muenchen", "Munich", "Garching", "Freising", "Erding", "Dachau", "Starnberg", "Tutzing",
    "Fürstenfeldbruck", "Fuerstenfeldbruck", "Ebersberg", "Unterschleißheim", "Oberschleißheim", "Martinsried",
    "Planegg", "Oberpfaffenhofen", "Weßling", "Gräfelfing", "Ismaning", "Unterföhring", "Grünwald", "Pullach",
    "Haar", "Vaterstetten", "Neubiberg", "Ottobrunn", "Taufkirchen", "Unterhaching", "Oberhaching", "Germering",
    "Puchheim", "Olching", "Karlsfeld", "Holzkirchen", "Wolfratshausen", "Geretsried", "Herrsching", "Seefeld",
    "Gauting", "Feldafing", "Pöcking", "Berg am Starnberger See", "Andechs", "Moosburg", "Hallbergmoos",
    "Eching", "Neufahrn", "Poing", "Markt Schwaben", "Grafing", "Kirchheim", "Feldkirchen", "Aschheim",
    "Hohenkammer", "Bernried", "Seeshaupt", "Kloster Seeon", "Maisach", "Gilching", "Krailling", "Stockdorf",
    "Neuried", "Sauerlach", "Höhenkirchen", "Brunnthal", "Hohenbrunn", "Zorneding", "Kirchseeon", "Erdweg",
    "Petershausen", "Haimhausen", "Bergkirchen", "Schwabhausen", "Inning", "Wörthsee", "Utting", "Dießen",
    "Fürstenfeld", "Freimann", "Schwabing", "Pasing", "Bogenhausen", "Giesing", "Sendling", "Neuhausen",
    "Haidhausen", "Laim", "Moosach", "Trudering", "Riem", "Perlach", "Garching-Hochbrück", "Großhadern",
]
# Orte klar ausserhalb: ohne MVV-Ort im Text wird so ein Termin aussortiert
OUTSIDE = [
    "Berlin", "Hamburg", "Köln", "Frankfurt", "Nürnberg", "Erlangen", "Fürth", "Augsburg", "Regensburg",
    "Stuttgart", "Hannover", "Düsseldorf", "Leipzig", "Dresden", "Bonn", "Wien", "Zürich", "Bad Staffelstein",
    "Kloster Banz", "Würzburg", "Ingolstadt", "Passau", "Bamberg", "Bayreuth", "Wildbad Kreuth", "Kreuth",
    "Pfaffenhofen", "Rosenheim", "Landshut", "Kempten", "Brüssel", "Brussels", "Paris", "London", "Kairo",
    "Hof", "Coburg", "Aschaffenburg", "Schweinfurt", "Straubing", "Deggendorf", "Neu-Ulm", "Ulm", "Mannheim",
    "Karlsruhe", "Essen", "Dortmund", "Bremen", "Mainz", "Wiesbaden", "Salzburg", "Innsbruck", "Singapur",
    "Indien", "Bad Kissingen", "Bad Tölz", "Garmisch", "Weiden", "Amberg", "Ansbach", "Memmingen", "Kaufbeuren",
    "Traunstein", "Mittelfranken", "Oberfranken", "Unterfranken", "Oberpfalz", "Niederbayern", "Amsterdam",
    "Shanghai", "Mumbai", "Delhi", "Bangkok", "Kuala Lumpur", "Malaysia", "Dubai", "New York", "Istanbul", "Altötting", "Mühldorf", "Neumarkt", "Eichstätt", "Dillingen", "Lindau", "Hannover", "Bielefeld",
]
_MVV_RE = re.compile(r"\b(" + "|".join(map(re.escape, MVV_PLACES)) + r")\b", re.I)
_OUT_RE = re.compile(r"\b(" + "|".join(map(re.escape, OUTSIDE)) + r")\b", re.I)
_CITY_RE = re.compile(r"\b(München|Muenchen|Munich|Schwabing|Pasing|Bogenhausen|Giesing|Sendling|Neuhausen|"
                      r"Haidhausen|Laim|Moosach|Trudering|Riem|Perlach|Freimann|Großhadern)\b", re.I)


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def mentions_mvv(text):
    return bool(_MVV_RE.search(text or ""))


def mentions_outside(text):
    m = _OUT_RE.search(text or "")
    return m.group(1) if m else None


def region_of(text, lat=None, lng=None):
    """'München' oder 'Umland'."""
    if lat and lng:
        return "München" if km((lat, lng), MARIENPLATZ) < 11 else "Umland"
    if not text or _CITY_RE.search(text):
        return "München"
    m = _MVV_RE.search(text)
    return "Umland" if m else "München"


class Geocoder:
    """Nominatim (OpenStreetMap). Hoechstens eine Anfrage pro Sekunde, Ergebnisse werden in
    data/geocache.json gespeichert, damit jede Adresse nur einmal gesucht wird."""

    def __init__(self, path, max_new=150):
        self.path = path
        self.max_new = max_new
        self.new = 0
        self.last = 0
        try:
            with open(path, encoding="utf-8") as f:
                self.cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            self.cache = {}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, self.path)

    def lookup(self, query):
        if not query:
            return None
        key = query.strip().lower()
        if key in self.cache:
            return self.cache[key]
        if self.new >= self.max_new or os.environ.get("NO_GEOCODE"):
            return None
        wait = 1.1 - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.new += 1
        res = None
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search", params={
                "q": query, "format": "json", "limit": 1, "countrycodes": "de",
                "viewbox": "10.95,48.55,12.25,47.75", "bounded": 0,
            }, headers={"User-Agent": "MuenchenEventsCollector/1.0 (private, non-commercial)"}, timeout=20)
            data = r.json()
            if data:
                res = {"lat": round(float(data[0]["lat"]), 5), "lng": round(float(data[0]["lon"]), 5),
                       "address": data[0].get("display_name", "")}
        except Exception:
            return None
        self.cache[key] = res
        return res
