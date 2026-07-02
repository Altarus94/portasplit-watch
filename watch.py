#!/usr/bin/env python3
"""
PortaSplit 12000 - surveillance de stock EN LIGNE + alerte push (ntfy).

Contexte (v4) : ClimRadar est passe payant. Sur les grandes enseignes, seules
DEUX repondent a un serveur (les autres -- Boulanger, Fnac, Darty, ManoMano,
Leroy Merlin, Cdiscount -- sont derriere des pare-feux anti-robots type
Akamai / Cloudflare / DataDome qui bloquent les IP de datacenter) :

  * Castorama : API interne JSON (fulfilment-options) -> vrai stock livraison
                (+ retrait magasin si des storeId sont fournis).
  * Amazon.fr : la fiche /dp/ sert la vraie dispo dans le HTML (buybox).
                Pas de JSON-LD trompeur ici : le HTML EST la verite.

Regles anti-faux-positif :
  - on ignore le JSON-LD schema.org (souvent "InStock" code en dur = mensonge) ;
  - une source illisible (erreur, 403, captcha, page ambigue) = INDETERMINE :
    on conserve son etat, on n'alerte pas (jamais de fausse alerte).

On alerte au passage indispo -> dispo, par source. Ping quotidien "tout va bien"
a 18h. Tourne sur GitHub Actions. En local : NTFY_TOPIC=mon-topic python watch.py
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

# --- Castorama ---
CASTO_EAN = os.environ.get("CASTO_EAN", "8431312260509").strip()
WATCH_POSTAL = os.environ.get("WATCH_POSTAL", "94100").strip()
# storeId Castorama a surveiller (retrait magasin). Vide = livraison seule.
STORE_IDS = [s.strip() for s in os.environ.get("CASTO_STORE_IDS", "").split(",") if s.strip()]

# --- Amazon.fr --- (vide pour desactiver)
AMAZON_ASIN = os.environ.get("AMAZON_ASIN", "B0CY2YW8BT").strip()

# --- Ping quotidien "tout va bien" ---
DAILY_OK_HOUR = int(os.environ.get("DAILY_OK_HOUR", "18"))
try:
    PARIS = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Paris")) if ZoneInfo else None
except Exception:
    PARIS = None

STATE_FILE = pathlib.Path(__file__).with_name("state.json")

CASTO_API = "https://www.castorama.fr/casto-browse-mfe/api/fulfilment-options"
CASTO_URL = ("https://www.castorama.fr/climatiseur-portasplit-midea-reversible-3500w/"
             f"{CASTO_EAN}_CAFR.prd")
AMAZON_URL = f"https://www.amazon.fr/dp/{AMAZON_ASIN}"

# availability Castorama qui signifient INDISPONIBLE (minuscules).
# "stockable" = "vendu en magasin" mais pas forcement en stock -> indispo tant
# qu'un storeId ne donne pas une quantite reelle > 0.
UNAVAILABLE = {"", "none", "outofstock", "notavailable", "unavailable",
               "soldout", "stockable", "discontinued"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


# ----------------------------------------------------------------- Castorama
def _casto_fetch(store_id=None):
    """Attributs de dispo Castorama, {} si vide, None si erreur/illisible."""
    params = {"compositeOfferId": CASTO_EAN, "postalCode": WATCH_POSTAL}
    if store_id:
        params["storeId"] = store_id
    try:
        r = requests.get(CASTO_API, params=params,
                         headers={"User-Agent": UA, "Accept": "application/json",
                                  "Accept-Language": "fr-FR,fr;q=0.9"}, timeout=25)
        if r.status_code != 200:
            print(f"  [casto] HTTP {r.status_code}" + (f" (store {store_id})" if store_id else ""))
            return None
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"  [casto] erreur : {e}")
        return None
    items = data.get("data") or []
    return items[0].get("attributes") or {} if items else {}


def _is_available(obj):
    """(dispo ?, quantite). Dispo si quantite>0, ou availability hors liste indispo."""
    if not isinstance(obj, dict):
        return False, 0
    qty = obj.get("quantity")
    qty = int(qty) if isinstance(qty, (int, float)) else 0
    if qty > 0:
        return True, qty
    return (str(obj.get("availability", "")).lower() not in UNAVAILABLE), qty


def check_castorama():
    """Renvoie {cle: {desc, url}} des offres dispo, ou None si illisible."""
    attrs = _casto_fetch()
    if not attrs or "homeDelivery" not in attrs:
        print("  [casto] reponse inexploitable -> indetermine")
        return None
    out = {}
    hd = attrs.get("homeDelivery", {})
    ok, qty = _is_available(hd)
    print(f"  [casto] livraison : {hd.get('availability')} (q{hd.get('quantity')}) -> {'DISPO' if ok else 'non'}")
    if ok:
        desc = "Castorama - livraison a domicile" + (f" ({qty} en stock)" if qty else "")
        out["casto:livraison"] = {"desc": desc, "url": CASTO_URL}
    for sid in STORE_IDS:
        s = _casto_fetch(store_id=sid)
        if s is None:
            continue
        ins = s.get("inStore", {})
        ok_s, qty_s = _is_available(ins)
        print(f"  [casto] magasin {sid} : {ins.get('availability')} (q{ins.get('quantity')}) -> {'DISPO' if ok_s else 'non'}")
        if ok_s:
            out[f"casto:store:{sid}"] = {"desc": f"Castorama {sid} - retrait ({qty_s} piece(s))", "url": CASTO_URL}
    return out


# ----------------------------------------------------------------- Amazon
def check_amazon():
    """Renvoie {cle: {desc, url}} si dispo, {} si indispo, None si indetermine
    (captcha / erreur / page ambigue -> jamais de fausse alerte)."""
    if not AMAZON_ASIN:
        return {}
    try:
        r = requests.get(AMAZON_URL, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }, timeout=25)
    except requests.RequestException as e:
        print(f"  [amazon] erreur : {e} -> indetermine")
        return None
    if r.status_code != 200:
        print(f"  [amazon] HTTP {r.status_code} -> indetermine")
        return None
    low = r.text.lower()
    if any(m in low for m in ("saisissez les caract", "type the characters",
                              "/errors/validatecaptcha", "api-services-support.amazon")):
        print("  [amazon] captcha/robot-check -> indetermine")
        return None
    oos = ("actuellement indisponible" in low) or ("outofstockbuybox" in low)
    buyable = ("submit.add-to-cart" in low) or ('id="add-to-cart-button"' in low)
    if buyable and not oos:
        print("  [amazon] EN STOCK")
        return {"amazon:livraison": {"desc": "Amazon.fr - en stock (livraison)", "url": AMAZON_URL}}
    if oos and not buyable:
        print("  [amazon] indisponible")
        return {}
    print(f"  [amazon] page ambigue (oos={oos} buyable={buyable}) -> indetermine")
    return None


# ----------------------------------------------------------------- state
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(today, available, last_alert_date, daily_ok_date):
    STATE_FILE.write_text(json.dumps({
        "_heartbeat": today,                 # >=1 commit/jour -> garde le cron actif
        "available": sorted(available),
        "last_alert_date": last_alert_date,
        "daily_ok_date": daily_ok_date,
    }, ensure_ascii=False, indent=2), "utf-8")


# ----------------------------------------------------------------- notif
def _post_ntfy(title, body, priority, tags, click):
    print(f"  >>> {title}\n{body}")
    if not NTFY_TOPIC:
        print("  (NTFY_TOPIC non defini : pas d'envoi reel)")
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                      headers={"Title": title, "Priority": priority, "Tags": tags, "Click": click},
                      timeout=20)
    except requests.RequestException as e:
        print(f"  ! envoi ntfy echoue : {e}")


def notify(new_keys, info):
    lines = "\n".join(f"- {info[k]['desc']}\n  {info[k]['url']}" for k in new_keys)
    body = f"Midea PortaSplit dispo !\n{lines}"
    _post_ntfy("PortaSplit dispo !", body, "urgent", "snowflake", info[new_keys[0]]["url"])


def daily_ok_notify():
    body = ("Surveillance OK : le PortaSplit est toujours indisponible "
            "(Castorama + Amazon).\n" + CASTO_URL)
    _post_ntfy("PortaSplit watch - tout va bien", body, "low", "white_check_mark", CASTO_URL)


# ----------------------------------------------------------------- main
SOURCES = [("casto", check_castorama), ("amazon", check_amazon)]


def main():
    prev = load_state()
    known = set(prev.get("available", []))
    last_alert_date = prev.get("last_alert_date", "")
    daily_ok_date = prev.get("daily_ok_date", "")
    now = dt.datetime.now(PARIS) if PARIS else dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()

    print(f"- PortaSplit watch - {now:%Y-%m-%d %H:%M %Z}")
    current = set()
    info = {}
    for src, checker in SOURCES:
        res = checker()
        if res is None:
            # Source illisible : on conserve son etat precedent (pas de faux mouvement).
            current |= {k for k in known if k.split(":", 1)[0] == src}
        else:
            for k, meta in res.items():
                current.add(k)
                info[k] = meta

    new_keys = [k for k in current if k not in known]
    if new_keys:
        notify(new_keys, info)
        last_alert_date = today
    else:
        print("  aucune nouveaute (pas de spam).")

    if (DAILY_OK_HOUR >= 0 and now.hour == DAILY_OK_HOUR
            and daily_ok_date != today and last_alert_date != today):
        daily_ok_notify()
        daily_ok_date = today

    save_state(today, current, last_alert_date, daily_ok_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
