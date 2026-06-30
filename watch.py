#!/usr/bin/env python3
"""
PortaSplit 12000 - surveillance de stock LOCAL + alerte push (ntfy).

Principe (v2) :
  - on lit la page geolocalisee de ClimRadar (https://climradar.fr), qui agrege
    le stock *en direct* d'une douzaine d'enseignes, magasin par magasin ;
  - ces donnees sont rendues cote serveur et integrees dans la page : un serveur
    (GitHub Actions) peut donc les lire sans navigateur ni JavaScript ;
  - on n'alerte QUE si le PortaSplit est reellement disponible
        * dans un magasin physique a <= RADIUS_KM de chez toi, OU
        * en ligne (livraison) en France -- desactivable via INCLUDE_ONLINE,
    et au prix normal (prix <= PRICE_MAX) ;
  - on n'alerte qu'au passage rupture -> dispo (pas de spam tant que ca reste
    dispo), magasin par magasin.

Tourne sur GitHub Actions toutes les 30 min (.github/workflows/watch.yml),
mais marche aussi en local :
    NTFY_TOPIC=mon-topic python watch.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import re
import sys

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:           # Python < 3.9 (ne devrait pas arriver)
    ZoneInfo = None

# ----------------------------------------------------------------- reglages
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
PRICE_MAX = float(os.environ.get("PRICE_MAX", "999"))      # seuil "prix normal"

# Ta zone : centre (lat, lon) + rayon en km. Defaut = secteur 94100 / 40 km.
CENTER_LAT = float(os.environ.get("WATCH_LAT", "48.7997"))
CENTER_LON = float(os.environ.get("WATCH_LON", "2.4937"))
RADIUS_KM = float(os.environ.get("WATCH_RADIUS_KM", "40"))

# Alerter aussi quand c'est dispo EN LIGNE (livraison) ? 1 = oui, 0 = non.
INCLUDE_ONLINE = os.environ.get("INCLUDE_ONLINE", "1").strip().lower() not in (
    "0", "", "false", "no", "non",
)

# Ping quotidien "tout va bien" : heure locale (FR) du run le plus proche.
# Envoye 1x/jour SI aucune alerte stock n'est partie ce jour-la. -1 = desactive.
DAILY_OK_HOUR = int(os.environ.get("DAILY_OK_HOUR", "18"))
try:
    PARIS = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris")) if ZoneInfo else None
except Exception:
    PARIS = None

POSTAL = os.environ.get("WATCH_POSTAL", "94100")           # juste pour le lien
PRODUCT = "portasplit"
IN_STOCK = {"en_stock", "stock_faible"}                    # statuts "dispo"

STATE_FILE = pathlib.Path(__file__).with_name("state.json")

# Page geolocalisee ClimRadar : a la fois source (donnees integrees dans la page)
# et lien ouvert quand tu tapes la notif (ta vue locale).
PAGE_URL = f"https://climradar.fr/?cp={POSTAL}&r={int(RADIUS_KM)}&pays=FR&p={PRODUCT}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ----------------------------------------------------------------- helpers
def fetch(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            print(f"  ! {url} -> HTTP {r.status_code}")
            return None
        return r.text
    except requests.RequestException as e:
        print(f"  ! {url} -> {e}")
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def extract_objects(html: str):
    """ClimRadar (Next.js) integre ses donnees JSON dans la page, avec les
    guillemets echappes (\\"). On de-echappe, puis on recupere chaque objet plat
    {...} et on le decode independamment (les objets non-JSON sont ignores)."""
    txt = html.replace('\\"', '"')
    out = []
    for chunk in re.findall(r"\{[^{}]*\}", txt):
        try:
            out.append(json.loads(chunk))
        except (json.JSONDecodeError, ValueError):
            pass
    return out


def parse_climradar(html: str):
    """Renvoie (stores_by_id, stock_entries) extraits de la page."""
    stores, stock = {}, []
    for o in extract_objects(html):
        if "retailerId" in o and "channel" in o and "id" in o and "lat" in o:
            stores[o["id"]] = o
        elif "storeId" in o and "productId" in o and "status" in o:
            stock.append(o)
    return stores, stock


def available_points(stores: dict, stock: list):
    """Points de vente ou le PortaSplit est dispo ET alertable (proche ou en
    ligne FR, sous le prix max). Chaque element : dict pret pour la notif."""
    hits = []
    for s in stock:
        if s.get("productId") != PRODUCT or s.get("status") not in IN_STOCK:
            continue
        price = s.get("price")
        if price is not None and price > PRICE_MAX:
            continue
        st = stores.get(s.get("storeId"))
        if not st:
            continue

        online = st.get("channel") == "online"
        dist = None
        if online:
            if not INCLUDE_ONLINE or st.get("country") != "FR":
                continue
        else:
            lat, lon = st.get("lat"), st.get("lon")
            if lat is None or lon is None:
                continue
            dist = haversine_km(CENTER_LAT, CENTER_LON, lat, lon)
            if dist > RADIUS_KM:
                continue

        hits.append({
            "id": s["storeId"],
            "name": st.get("name", s["storeId"]),
            "city": st.get("city", ""),
            "status": s["status"],
            "price": price,
            "online": online,
            "dist_km": dist,
        })
    return hits


# ----------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(today, available, last_alert_date, daily_ok_date):
    state = {
        "_heartbeat": today,                 # >=1 commit/jour -> garde le cron actif
        "available": sorted(available),
        "last_alert_date": last_alert_date,  # dernier jour ou une alerte stock est partie
        "daily_ok_date": daily_ok_date,      # dernier jour ou le ping "tout va bien" est parti
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ----------------------------------------------------------------- notif
def notify(new_hits):
    def sort_key(h):
        return (h["online"], h["dist_km"] if h["dist_km"] is not None else 0.0)

    lines = []
    for h in sorted(new_hits, key=sort_key):
        price_txt = f"{h['price']:.0f} EUR" if h["price"] is not None else "prix a verifier"
        where = "en ligne (livraison)" if h["online"] else f"{h['city']} - {h['dist_km']:.0f} km"
        etat = "stock faible" if h["status"] == "stock_faible" else "en stock"
        lines.append(f"- {h['name']} ({where}) : {etat}, {price_txt}")

    body = "Midea PortaSplit 12000 dispo !\n" + "\n".join(lines) + f"\n{PAGE_URL}"
    print("  >>> ALERTE :\n" + body)
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC non defini : pas d'envoi reel)")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": "PortaSplit 12000 dispo pres de chez toi !",
                "Priority": "urgent",
                "Tags": "snowflake",
                "Click": PAGE_URL,
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! envoi ntfy echoue : {e}")


def daily_ok_notify(nat_count: int, store_count: int):
    """Ping quotidien rassurant (priorite basse) : la surveillance tourne bien."""
    body = (
        "Surveillance OK : aucun PortaSplit dispo pres de chez toi aujourd'hui.\n"
        f"({store_count} magasins suivis, {nat_count} en stock en France)\n{PAGE_URL}"
    )
    print("  (ping 'tout va bien') " + body.replace("\n", " | "))
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC non defini : pas d'envoi reel)")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": "PortaSplit watch - tout va bien",
                "Priority": "low",
                "Tags": "white_check_mark",
                "Click": PAGE_URL,
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! envoi ntfy (ping) echoue : {e}")


# ----------------------------------------------------------------- main
def main():
    prev = load_state()
    known = set(prev.get("available", []))
    last_alert_date = prev.get("last_alert_date", "")
    daily_ok_date = prev.get("daily_ok_date", "")

    now = dt.datetime.now(PARIS) if PARIS else dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()

    online_txt = "oui" if INCLUDE_ONLINE else "non"
    print(f"- ClimRadar (zone {POSTAL}, rayon {RADIUS_KM:.0f} km, en ligne={online_txt}) - {now:%Y-%m-%d %H:%M %Z}")
    html = fetch(PAGE_URL)
    if html is None:
        print("  ! page indisponible, on garde l'etat precedent.")
        save_state(today, known, last_alert_date, daily_ok_date)
        return 0

    stores, stock = parse_climradar(html)
    print(f"  donnees : {len(stores)} magasins, {len(stock)} entrees de stock")
    if len(stores) < 20 or len(stock) < 20:
        # Page vide / format change / blocage : on n'efface pas l'etat connu.
        print("  ! donnees insuffisantes (format change ou blocage ?), etat conserve.")
        save_state(today, known, last_alert_date, daily_ok_date)
        return 0

    nat = sum(
        1 for s in stock
        if s.get("productId") == PRODUCT and s.get("status") in IN_STOCK
    )
    hits = available_points(stores, stock)
    print(f"  en stock (national) : {nat} | alertable (<= {RADIUS_KM:.0f} km ou en ligne FR) : {len(hits)}")
    for h in hits:
        where = "en ligne" if h["online"] else f"{h['city']} {h['dist_km']:.0f}km"
        print(f"    * {h['name']} [{where}] {h['status']} {h['price']}EUR")

    current = {h["id"] for h in hits}
    new_hits = [h for h in hits if h["id"] not in known]
    if new_hits:
        notify(new_hits)
        last_alert_date = today
    else:
        print("  aucune nouveaute (pas de spam).")

    # Ping quotidien "tout va bien" : sur le run le plus proche de DAILY_OK_HOUR,
    # une seule fois/jour, et seulement si aucune alerte stock n'est partie aujourd'hui.
    if (DAILY_OK_HOUR >= 0 and now.hour == DAILY_OK_HOUR
            and daily_ok_date != today and last_alert_date != today):
        daily_ok_notify(nat, len(stores))
        daily_ok_date = today

    save_state(today, current, last_alert_date, daily_ok_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
