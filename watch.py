#!/usr/bin/env python3
"""
PortaSplit 12000 - surveillance de stock Castorama + alerte push (ntfy).

Contexte (v3) : ClimRadar a ferme l'acces gratuit a ses donnees (passe payant).
On lit donc directement l'API de disponibilite de Castorama (la seule enseigne
qui reponde a un serveur), qui donne le VRAI stock -- pas le JSON-LD trompeur :

    GET https://www.castorama.fr/casto-browse-mfe/api/fulfilment-options
        ?compositeOfferId=<EAN>&postalCode=<CP>[&storeId=<id>]

Signaux FIABLES :
  - homeDelivery : livraison a domicile (national), avec quantite ;
  - inStore AVEC un storeId : vrai stock du magasin.
ATTENTION : SANS storeId, clickAndCollect/inStore valent "Available"/"Stockable"
PARTOUT (drapeaux de capacite, verifie sur 5 codes postaux) -> on ne s'en sert
PAS pour alerter (sinon faux positifs).

On n'alerte qu'au passage indispo -> dispo. Ping quotidien "tout va bien" a 18h.

Tourne sur GitHub Actions (.github/workflows/watch.yml). En local :
    NTFY_TOPIC=mon-topic python watch.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:           # Python < 3.9 (ne devrait pas arriver)
    ZoneInfo = None

# ----------------------------------------------------------------- reglages
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

# Produit surveille : EAN du Midea PortaSplit 12000 chez Castorama.
CASTO_EAN = os.environ.get("CASTO_EAN", "8431312260509").strip()
WATCH_POSTAL = os.environ.get("WATCH_POSTAL", "94100").strip()

# Magasins a surveiller EN PLUS de la livraison (stock reel par magasin).
# storeId Castorama separes par des virgules. Vide = livraison a domicile seule.
# (Les storeId se capturent dans le navigateur via le selecteur de magasin.)
STORE_IDS = [s.strip() for s in os.environ.get("CASTO_STORE_IDS", "").split(",") if s.strip()]

# Ping quotidien "tout va bien" : heure locale (FR). -1 = desactive.
DAILY_OK_HOUR = int(os.environ.get("DAILY_OK_HOUR", "18"))
try:
    PARIS = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris")) if ZoneInfo else None
except Exception:
    PARIS = None

STATE_FILE = pathlib.Path(__file__).with_name("state.json")
API = "https://www.castorama.fr/casto-browse-mfe/api/fulfilment-options"
PRODUCT_URL = (
    "https://www.castorama.fr/climatiseur-portasplit-midea-reversible-3500w/"
    f"{CASTO_EAN}_CAFR.prd"
)

# availability qui signifient INDISPONIBLE (comparaison en minuscules).
# "stockable" = "vendu en magasin" mais PAS forcement en stock -> indispo tant
# qu'un storeId ne donne pas une quantite reelle > 0.
UNAVAILABLE = {"", "none", "outofstock", "notavailable", "unavailable",
               "soldout", "stockable", "discontinued"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


# ----------------------------------------------------------------- API
def fetch_fulfilment(store_id: str | None = None):
    """Renvoie les attributs de dispo, {} si produit vide, None si erreur."""
    params = {"compositeOfferId": CASTO_EAN, "postalCode": WATCH_POSTAL}
    if store_id:
        params["storeId"] = store_id
    try:
        r = requests.get(API, params=params, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            print(f"  ! API HTTP {r.status_code}" + (f" (store {store_id})" if store_id else ""))
            return None
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  ! API erreur : {e}")
        return None
    items = data.get("data") or []
    if not items:
        return {}
    return items[0].get("attributes") or {}


def is_available(obj) -> tuple[bool, int]:
    """(disponible ?, quantite). Dispo si quantite>0, ou availability hors liste
    'indispo' (ce qui couvre InStock/Available/LimitedStock cote livraison)."""
    if not isinstance(obj, dict):
        return False, 0
    qty = obj.get("quantity")
    qty = int(qty) if isinstance(qty, (int, float)) else 0
    if qty > 0:
        return True, qty
    avail = str(obj.get("availability", "")).lower()
    return (avail not in UNAVAILABLE), qty


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
        "last_alert_date": last_alert_date,
        "daily_ok_date": daily_ok_date,
    }
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


# ----------------------------------------------------------------- notif
def _post_ntfy(title, body, priority, tags):
    print(f"  >>> {title}\n" + body)
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC non defini : pas d'envoi reel)")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags, "Click": PRODUCT_URL},
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"  ! envoi ntfy echoue : {e}")


def notify(current, new_keys):
    lines = "\n".join(f"- {current[k]}" for k in new_keys)
    body = f"Midea PortaSplit dispo chez Castorama !\n{lines}\n{PRODUCT_URL}"
    _post_ntfy("PortaSplit dispo chez Castorama !", body, "urgent", "snowflake")


def daily_ok_notify(attrs):
    hd = attrs.get("homeDelivery", {})
    body = (
        "Surveillance Castorama OK : le PortaSplit est toujours indisponible.\n"
        f"(livraison : {hd.get('availability', '?')})\n{PRODUCT_URL}"
    )
    _post_ntfy("PortaSplit watch - tout va bien", body, "low", "white_check_mark")


# ----------------------------------------------------------------- main
def main():
    prev = load_state()
    known = set(prev.get("available", []))
    last_alert_date = prev.get("last_alert_date", "")
    daily_ok_date = prev.get("daily_ok_date", "")
    now = dt.datetime.now(PARIS) if PARIS else dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()

    print(f"- Castorama PortaSplit (EAN {CASTO_EAN}, zone {WATCH_POSTAL}) - {now:%Y-%m-%d %H:%M %Z}")
    attrs = fetch_fulfilment()
    if not attrs or "homeDelivery" not in attrs:
        print("  ! reponse API inexploitable (format change ou blocage ?), etat conserve.")
        save_state(today, known, last_alert_date, daily_ok_date)
        return 0

    current = {}   # cle -> description lisible

    hd = attrs.get("homeDelivery", {})
    ok, qty = is_available(hd)
    print(f"  livraison domicile : {hd.get('availability')} (q{hd.get('quantity')}) -> {'DISPO' if ok else 'non'}")
    if ok:
        current["livraison"] = "Livraison a domicile disponible" + (f" ({qty} en stock)" if qty else "")

    for sid in STORE_IDS:
        sattrs = fetch_fulfilment(store_id=sid)
        if not sattrs:
            continue
        ins = sattrs.get("inStore", {})
        ok_s, qty_s = is_available(ins)
        print(f"  magasin {sid} : inStore={ins.get('availability')} (q{ins.get('quantity')}) -> {'DISPO' if ok_s else 'non'}")
        if ok_s:
            current[f"store:{sid}"] = f"Retrait magasin {sid} : {qty_s} piece(s)"

    new_keys = [k for k in current if k not in known]
    if new_keys:
        notify(current, new_keys)
        last_alert_date = today
    else:
        print("  aucune nouveaute (pas de spam).")

    if (DAILY_OK_HOUR >= 0 and now.hour == DAILY_OK_HOUR
            and daily_ok_date != today and last_alert_date != today):
        daily_ok_notify(attrs)
        daily_ok_date = today

    save_state(today, set(current.keys()), last_alert_date, daily_ok_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
