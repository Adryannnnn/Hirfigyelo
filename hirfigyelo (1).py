#!/usr/bin/env python3
"""Pénzügyi és Trump-hírfigyelő: hírfolyamokat néz, téma szerint szűri a fontos híreket,
push értesítést küld a telefonodra (ntfy.sh), és egy telefonbarát hírlistát is készít.

Csak a Python beépített moduljait használja, nincs mit telepíteni.
A témákat, szavakat és forrásokat a beallitasok.json fájlban tudod szerkeszteni.

Használat:
    python3 hirfigyelo.py                         # egyszeri ellenőrzés
    python3 hirfigyelo.py --loop                  # folyamatos figyelés
    python3 hirfigyelo.py --test                  # tesztértesítést küld
    python3 hirfigyelo.py --szaraz                # csak kiírja a híreket, nem küld értesítést
    python3 hirfigyelo.py --beallitasok-letrehoz  # kiírja az alap beallitasok.json fájlt
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

MAPPA = Path(__file__).resolve().parent
BEALLITAS_FAJL = Path(os.environ.get("HIRFIGYELO_BEALLITASOK", MAPPA / "beallitasok.json"))
ALLAPOT_FAJL = Path(os.environ.get("HIRFIGYELO_ALLAPOT", MAPPA / "lattott.json"))
HIREK_JSON = Path(os.environ.get("HIRFIGYELO_HIREK", MAPPA / "hirek.json"))
HIREK_HTML = HIREK_JSON.with_suffix(".html")
UA = {"User-Agent": "Mozilla/5.0 (hirfigyelo/3.0)"}

# ======================= ALAPBEÁLLÍTÁSOK =======================
# Ezt nem kell szerkesztened: a saját változtatásaidat a beallitasok.json fájlba írd.
# Szó végi * = bármilyen ragozás (kamat* találja: kamat, kamatdöntés, kamatot).
ALAP = {
    "_megjegyzes": "Szerkeszthető beállítások. Témát/szót/forrást törölhetsz vagy hozzáadhatsz. "
                   "Egy téma vagy forrás kikapcsolása: 'aktiv': false. Szó végi * = bármilyen ragozás.",
    "ntfy_topic": "CSERELD-LE-egyedi-nev-123",
    "percek": 10,
    "max_ertesites": 8,
    "max_kor_ora": 24,
    "max_hir_a_listaban": 80,
    # Fordítás: "hu" = az angol címeket magyarra fordítja (az eredeti zárójelben mellette marad).
    # Kikapcsolás: "fordit": "" . A már magyar címeket nem fordítja.
    "fordit": "hu",
    "max_fordit": 60,
    # Telegram: NYILVÁNOS csatornák neve @ nélkül, például ["valamicsatorna", "masikcsatorna"].
    # Privát csoportot és csoportbeszélgetést nem tud olvasni.
    "telegram_csatornak": [],
    "temak": {
        "Trump – háború": {
            "aktiv": True,
            "szavak": ["trump"],
            "es_szavak": [
                "war", "military", "troops", "strike", "strikes", "attack", "invasion", "invade", "missile",
                "nuclear", "ceasefire", "peace deal", "peace talks", "pentagon", "nato", "iran", "israel",
                "gaza", "russia", "ukraine", "putin", "zelensky", "china", "taiwan", "north korea",
                "háború*", "katon*", "támadás*", "rakéta*", "tűzszünet*", "béketárgyalás*", "orosz*", "ukrán*",
            ],
        },
        "Trump – döntések": {
            "aktiv": True,
            "szavak": ["trump"],
            "es_szavak": [
                "executive order", "signs", "signed", "orders", "decision", "decides", "announces", "announced",
                "veto", "pardon", "sanctions", "ban", "bans", "tariff", "tariffs", "nominates", "nominee",
                "fires", "appoints", "plan", "deal", "bill",
                "döntés*", "rendelet*", "bejelent*", "aláír*", "vám*", "szankció*", "kinevez*", "elnöki",
            ],
        },
        "Trump": {"aktiv": True, "szavak": ["trump", "white house", "fehér ház"]},
        "Kripto": {
            "aktiv": True,
            "szavak": [
                "bitcoin", "btc", "ethereum", "ether", "crypto", "cryptocurrency", "kripto*", "stablecoin",
                "tether", "solana", "xrp", "binance", "coinbase", "spot etf", "bitcoin etf", "crypto etf",
                "halving", "crypto regulation", "crypto hack", "exchange hack", "strategic bitcoin reserve",
                "microstrategy",
            ],
        },
        "Forex": {
            "aktiv": True,
            "szavak": [
                "forex", "fx market", "eur/usd", "usd/jpy", "gbp/usd", "usd/chf", "eur/huf", "usd/huf",
                "dollar index", "dxy", "us dollar", "yen", "sterling", "currency", "currencies",
                "forint*", "deviza*", "árfolyam*",
            ],
        },
        "Tőzsde": {
            "aktiv": True,
            "szavak": [
                "stock market", "wall street", "s&p 500", "nasdaq", "dow jones", "sell-off", "selloff",
                "rally", "crash", "bond yield", "treasury yield", "earnings", "bankruptcy", "merger",
                "acquisition", "ipo", "profit warning", "tőzsde*", "részvény*", "bux", "gyorsjelentés*",
                "csőd*", "felvásárl*",
            ],
        },
        "Jegybank/kamat": {
            "aktiv": True,
            "szavak": [
                "federal reserve", "the fed", "fomc", "ecb", "bank of england", "bank of japan", "rate cut",
                "rate hike", "interest rate", "inflation", "cpi", "powell", "lagarde", "mnb",
                "kamat*", "infláció*", "jegybank*",
            ],
        },
        "Világgazdaság": {
            "aktiv": True,
            "szavak": [
                "gdp", "jobs report", "nonfarm", "unemployment", "recession", "tariff", "trade war",
                "sanctions", "opec", "oil price", "crude", "gold price", "commodities",
                "recesszi*", "vám*", "munkanélküliség*", "olajár*", "aranyár*",
            ],
        },
    },
    # Ezekre a szavakra (a címben) hangos, sürgős értesítés jön.
    "surgos": [
        "breaking", "just in", "urgent", "plunge", "surge", "soar", "tumble", "crash", "record high",
        "all-time high", "emergency", "halts", "default", "collapse", "hack", "rate cut", "rate hike",
        "tariff", "sanction", "ban", "approves", "war", "attack", "strike", "invasion", "missile",
        "nuclear", "ceasefire", "executive order", "veto",
        "zuhan*", "szárnyal*", "rekord*", "háború*", "támadás*", "azonnali",
    ],
    # Ezeket tartalmazó híreket kihagyja.
    "kizart": ["horoscope", "celebrity", "recipe", "sweepstakes", "sponsored", "giveaway",
               "price prediction", "top 5 coins"],
    # Hírforrások. 'url' = RSS-cím, 'gnews' = Google News keresés (Reuters, WSJ stb. így érhető el).
    "forrasok": {
        "Reuters": {"aktiv": True, "gnews": "site:reuters.com (markets OR stocks OR economy OR bitcoin OR crypto OR forex OR Trump)"},
        "Wall Street Journal": {"aktiv": True, "gnews": "site:wsj.com (markets OR stocks OR economy OR bitcoin OR crypto OR dollar OR Trump)"},
        "CNN": {"aktiv": True, "gnews": "site:cnn.com (markets OR business OR economy OR stocks OR bitcoin OR crypto OR Trump OR war)"},
        "Bloomberg": {"aktiv": True, "url": "https://feeds.bloomberg.com/markets/news.rss"},
        "Bloomberg Politika": {"aktiv": True, "url": "https://feeds.bloomberg.com/politics/news.rss"},
        "Investing.com": {"aktiv": True, "url": "https://www.investing.com/rss/news.rss"},
        "Investing.com Forex": {"aktiv": True, "url": "https://www.investing.com/rss/news_1.rss"},
        "Investing.com Kripto": {"aktiv": True, "url": "https://www.investing.com/rss/news_301.rss"},
        "Portfolio.hu": {"aktiv": True, "url": "https://www.portfolio.hu/rss/all.xml"},
        "CoinDesk": {"aktiv": True, "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
        "Cointelegraph": {"aktiv": True, "url": "https://cointelegraph.com/rss"},
        "Trump – háború (Google News)": {"aktiv": True, "gnews": "Trump (war OR strike OR military OR troops OR Iran OR Russia OR Ukraine OR Israel OR Gaza OR China OR Taiwan OR NATO OR ceasefire)"},
        "Trump – döntések (Google News)": {"aktiv": True, "gnews": "Trump (\"executive order\" OR signs OR decision OR announces OR veto OR pardon OR sanctions OR tariff)"},
        "Trump (magyarul)": {"aktiv": True, "gnews": "Trump háború OR döntés OR vám OR rendelet", "nyelv": "hu-HU", "orszag": "HU"},
        "BBC World": {"aktiv": True, "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
        "BBC Business": {"aktiv": True, "url": "https://feeds.bbci.co.uk/news/business/rss.xml"},
        "CNBC": {"aktiv": True, "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664"},
        "MarketWatch": {"aktiv": True, "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
        "Yahoo Finance": {"aktiv": True, "url": "https://finance.yahoo.com/news/rssindex"},
        # X (Twitter): nincs ingyenes, megbízható RSS-e. Ha van saját RSS-hidad vagy Nitter-szervered:
        # "X: @realDonaldTrump": {"aktiv": true, "url": "https://SZERVERED/realDonaldTrump/rss"},
    },
}
# ===============================================================

CFG = {}
FEEDEK = {}
TEMA_RE = {}
SURGOS_RE = None
KIZART_RE = None
NTFY_TOPIC = ""
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")


def gnews(kereses, nyelv="en-US", orszag="US"):
    q = urllib.parse.quote_plus(kereses + " when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl={nyelv}&gl={orszag}&ceid={orszag}:{nyelv.split('-')[0]}"


def kulcs_regex(szavak):
    """Szóhatáros, kis-nagybetű független keresés. Szó végi * = bármilyen ragozás."""
    reszek = []
    for sz in szavak:
        sz = (sz or "").strip()
        if not sz:
            continue
        if sz.endswith("*"):
            reszek.append(r"(?<!\w)" + re.escape(sz[:-1]) + r"\w*")
        else:
            reszek.append(r"(?<!\w)" + re.escape(sz) + r"(?:e?s)?(?!\w)")
    return re.compile("|".join(reszek) if reszek else r"(?!x)x", re.IGNORECASE)


def beallitasok_betolt():
    cfg = json.loads(json.dumps(ALAP))
    try:
        sajat = json.loads(BEALLITAS_FAJL.read_text(encoding="utf-8"))
        cfg.update(sajat)  # amit a fájlban megadsz, az felülírja az alapot
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        sys.exit(f"Hiba a {BEALLITAS_FAJL.name} fájlban (valószínűleg hiányzó vessző vagy idézőjel): {e}")
    return cfg


def inicializal(cfg):
    global CFG, FEEDEK, TEMA_RE, SURGOS_RE, KIZART_RE, NTFY_TOPIC
    CFG = cfg
    NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    FEEDEK = {}
    for nev, f in cfg["forrasok"].items():
        if isinstance(f, str):
            f = {"url": f}
        if not f.get("aktiv", True):
            continue
        if f.get("gnews"):
            FEEDEK[nev] = gnews(f["gnews"], f.get("nyelv", "en-US"), f.get("orszag", "US"))
        elif f.get("url"):
            FEEDEK[nev] = f["url"]
    for cs in cfg.get("telegram_csatornak", []):
        # elfogad: nev, @nev, t.me/nev, https://t.me/s/nev
        cs = re.sub(r"^(https?://)?t\.me/(s/)?", "", str(cs).strip()).lstrip("@").split("/")[0]
        if cs:
            FEEDEK[f"Telegram: @{cs}"] = "tg:" + cs
    TEMA_RE = {}
    for nev, t in cfg["temak"].items():
        if not t.get("aktiv", True):
            continue
        es = kulcs_regex(t["es_szavak"]) if t.get("es_szavak") else None
        TEMA_RE[nev] = (kulcs_regex(t.get("szavak", [])), es)
    SURGOS_RE = kulcs_regex(cfg.get("surgos", []))
    KIZART_RE = kulcs_regex(cfg.get("kizart", []))


def letolt(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def szoveg(elem, *nevek):
    for n in nevek:
        e = elem.find(n)
        if e is not None and (e.text or "").strip():
            return e.text.strip()
    return ""


def tisztit(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def feed_elemek(adat):
    """RSS 2.0 és Atom feedeket is kezel. Visszaad: (cím, link, leírás, időpont|None)."""
    gyoker = ET.fromstring(adat)
    ki = []
    A = "{http://www.w3.org/2005/Atom}"
    for it in gyoker.iter():
        tag = it.tag.split("}")[-1]
        if tag == "item":
            cim, link = szoveg(it, "title"), szoveg(it, "link")
            leiras, datum = tisztit(szoveg(it, "description")), szoveg(it, "pubDate")
        elif tag == "entry":
            cim = szoveg(it, A + "title")
            le = it.find(A + "link")
            link = le.get("href", "") if le is not None else ""
            leiras, datum = tisztit(szoveg(it, A + "summary")), szoveg(it, A + "updated", A + "published")
        else:
            continue
        ido = None
        if datum:
            try:
                ido = parsedate_to_datetime(datum).timestamp()
            except Exception:
                try:
                    ido = datetime.fromisoformat(datum.replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass
        if cim:
            ki.append((tisztit(cim), link, leiras, ido))
    return ki


def telegram_elemek(oldal, csatorna):
    """Egy nyilvános Telegram-csatorna webes előnézetének (t.me/s/csatorna) feldolgozása.
    Visszaad: (cím, link, leírás, időpont|None), ugyanúgy, mint a feed_elemek."""
    ki = []
    jelek = list(re.finditer(r'data-post="([^"]+)"', oldal))
    for n, m in enumerate(jelek):
        vege = jelek[n + 1].start() if n + 1 < len(jelek) else len(oldal)
        blokk = oldal[m.start():vege]
        t = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', blokk, re.S)
        if not t:
            continue  # csak kép/videó, szöveg nélkül
        szov = html.unescape(tisztit(re.sub(r"<br\s*/?>", " ", t.group(1))))
        if not szov:
            continue
        ido = None
        d = re.search(r'<time[^>]*datetime="([^"]+)"', blokk)
        if d:
            try:
                ido = datetime.fromisoformat(d.group(1).replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        cim = szov if len(szov) <= 200 else szov[:200].rsplit(" ", 1)[0] + "…"
        ki.append((cim, "https://t.me/" + m.group(1), szov, ido))
    return ki


def elemzes(cim, leiras):
    """Visszaad: (témák listája, sürgős-e). Üres lista = nem érdekes."""
    t = cim + " " + leiras
    if KIZART_RE.search(t):
        return [], False
    temak = [nev for nev, (rx, es) in TEMA_RE.items() if rx.search(t) and (es is None or es.search(t))]
    return temak, bool(temak) and bool(SURGOS_RE.search(cim))


def cim_kulcs(cim):
    """Ugyanaz a cím két forrásból ne jöjjön kétszer ('... - Reuters' végződés nélkül)."""
    c = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", cim.lower())
    return "t" + hashlib.sha1(re.sub(r"\W+", "", c).encode("utf-8")).hexdigest()[:14]


def link_kulcs(link):
    return "l" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:14]


def forditas(szoveg_, cel="hu"):
    """Cím fordítása (a Google Translate nem hivatalos, kulcs nélküli végpontján át).
    Ha bármi hiba van, az eredeti szöveget adja vissza, így az értesítés nem marad el."""
    try:
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl="
               + urllib.parse.quote(cel) + "&dt=t&q=" + urllib.parse.quote(szoveg_))
        adat = json.loads(letolt(url, timeout=10).decode("utf-8"))
        ford = "".join(r[0] for r in adat[0] if r and r[0]).strip()
        return ford or szoveg_
    except Exception as e:
        print(f"[!] fordítás nem sikerült ({e.__class__.__name__})", file=sys.stderr)
        return szoveg_


MAGYAR_RE = re.compile(r"[áéíóöőúüűÁÉÍÓÖŐÚÜŰ]")


def json_betolt(fajl, alap):
    try:
        return json.loads(fajl.read_text(encoding="utf-8"))
    except Exception:
        return alap


def ertesit(cim, uzenet, link=None, surgos=False):
    # JSON API-t használunk, így az ékezetes szöveg is hibátlanul megy át
    adat = {"topic": NTFY_TOPIC, "title": cim, "message": uzenet, "priority": 4 if surgos else 3,
            "tags": ["rotating_light" if surgos else "chart_with_upwards_trend"]}
    if link:
        adat["click"] = link
    req = urllib.request.Request(NTFY_SERVER.rstrip("/") + "/", data=json.dumps(adat).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **UA})
    urllib.request.urlopen(req, timeout=20).read()


def hirlista_ment(uj_tetelek, eredeti=None):
    """hirek.json (widgetekhez, más programokhoz) és hirek.html (telefonon megnyitható oldal)."""
    regi = json_betolt(HIREK_JSON, [])
    van = {x["link"] or x["cim"] for x in regi}
    eredeti = eredeti or {}
    lista = [{"ido": int(t[1]), "forras": t[2], "temak": t[3], "cim": t[4], "link": t[5], "surgos": t[0],
              "eredeti": eredeti.get(t[4], "")}
             for t in uj_tetelek if (t[5] or t[4]) not in van] + regi
    lista.sort(key=lambda x: x["ido"], reverse=True)
    lista = lista[: int(CFG.get("max_hir_a_listaban", 80))]
    HIREK_JSON.write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")

    sorok = []
    for x in lista:
        ido = datetime.fromtimestamp(x["ido"]).strftime("%m.%d. %H:%M")
        cimke = html.escape(" · ".join(x["temak"][:2]))
        link = html.escape(x["link"] or "#")
        sorok.append(
            f'<a class="h{" s" if x["surgos"] else ""}" href="{link}"><div class="m">'
            f'{"🔴 " if x["surgos"] else ""}{cimke} · {html.escape(x["forras"])} · {ido}</div>'
            f'<div class="c">{html.escape(x["cim"])}</div>'
            + (f'<div class="o">{html.escape(x["eredeti"])}</div>' if x.get("eredeti") else "")
            + '</a>')
    HIREK_HTML.write_text(
        '<!doctype html><html lang="hu"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="refresh" content="300"><title>Hírek</title><style>'
        ':root{color-scheme:light dark}body{font:16px/1.4 system-ui,sans-serif;margin:0;padding:12px;max-width:640px;margin:auto}'
        'h1{font-size:20px;margin:4px 0 12px}.h{display:block;padding:10px 0;border-bottom:1px solid #8884;'
        'text-decoration:none;color:inherit}.m{font-size:12px;opacity:.65}.c{margin-top:2px}.s .c{font-weight:600}.o{margin-top:2px;font-size:13px;opacity:.55;font-style:italic}'
        '</style></head><body><h1>Fontos hírek</h1>' + "".join(sorok) +
        f'<p class="m">Frissítve: {datetime.now().strftime("%Y.%m.%d. %H:%M")}</p></body></html>', encoding="utf-8")


def egy_kor(kuldes=True):
    lattott = json_betolt(ALLAPOT_FAJL, None)
    elso_futas = lattott is None
    lattott = lattott or []
    halmaz = set(lattott)
    most = time.time()
    max_kor = float(CFG.get("max_kor_ora", 24)) * 3600
    uj, lista_uj = [], []

    for nev, url in FEEDEK.items():
        try:
            if url.startswith("tg:"):
                elemek = telegram_elemek(letolt("https://t.me/s/" + url[3:]).decode("utf-8", "replace"), url[3:])
            else:
                elemek = feed_elemek(letolt(url))
        except Exception as e:
            print(f"[!] {nev}: nem sikerült ({e.__class__.__name__}: {e})", file=sys.stderr)
            continue
        for cim, link, leiras, ido in elemek:
            kulcsok = [cim_kulcs(cim)] + ([link_kulcs(link)] if link else [])
            if any(k in halmaz for k in kulcsok):
                continue
            for k in kulcsok:
                halmaz.add(k)
                lattott.append(k)
            if ido and most - ido > max_kor:
                continue
            temak, surgos = elemzes(cim, leiras)
            if not temak:
                continue
            tetel = (surgos, ido or most, nev, temak, cim, link)
            lista_uj.append(tetel)
            if not elso_futas:  # az első futás csak "megtanulja" a jelenlegi híreket
                uj.append(tetel)

    uj.sort(key=lambda x: (x[0], x[1]), reverse=True)
    lista_uj.sort(key=lambda x: x[1], reverse=True)
    maxe = int(CFG.get("max_ertesites", 8))

    # fordítás magyarra (először az értesítésre szánt hírek, utána a lista többi tagja)
    eredeti = {}
    cel = CFG.get("fordit", "")
    if cel:
        fm, keret = {}, int(CFG.get("max_fordit", 60))
        for tet in uj + lista_uj:
            c = tet[4]
            if c in fm or len(fm) >= keret:
                continue
            fm[c] = c if MAGYAR_RE.search(c) else forditas(c, cel)
        for orig, mag in fm.items():
            if mag != orig:
                eredeti[mag] = orig
        uj = [(s, i, n, t, fm.get(c, c), l) for (s, i, n, t, c, l) in uj]
        lista_uj = [(s, i, n, t, fm.get(c, c), l) for (s, i, n, t, c, l) in lista_uj]

    def kuld_egyet(x):
        surgos, _, nev, temak, cim, link = x
        uzenet = cim + (f"\n{eredeti[cim]}" if cim in eredeti else "")
        ertesit(f"{'🔴 ' if surgos else ''}{' · '.join(temak[:2])} | {nev}", uzenet, link, surgos)

    if kuldes:
        if elso_futas:
            ertesit("Hírfigyelő elindult", "Innentől csak az új, fontos híreket küldöm.")
        elif len(uj) > maxe:
            for x in uj[:maxe - 1]:
                kuld_egyet(x)
            maradek = uj[maxe - 1:]
            ertesit(f"+{len(maradek)} további fontos hír", "\n".join("• " + x[4] for x in maradek[:15]))
        else:
            for x in uj:
                kuld_egyet(x)
    else:
        for surgos, _, nev, temak, cim, link in uj:
            print(f"{'!!' if surgos else '  '} [{'/'.join(temak)}] {nev}: {cim}  {link}")

    json_ment = json.dumps(lattott[-6000:])
    ALLAPOT_FAJL.write_text(json_ment)
    hirlista_ment(lista_uj, eredeti)
    print(f"Kész: {len(uj)} új fontos hír" + (" (első futás, alapállapot mentve)" if elso_futas else ""))
    return len(uj)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="folyamatos figyelés")
    ap.add_argument("--test", action="store_true", help="tesztértesítés küldése")
    ap.add_argument("--szaraz", action="store_true", help="csak kiírja a híreket, nem küld értesítést")
    ap.add_argument("--beallitasok-letrehoz", action="store_true", help="kiírja az alap beallitasok.json fájlt")
    a = ap.parse_args()

    if a.beallitasok_letrehoz:
        BEALLITAS_FAJL.write_text(json.dumps(ALAP, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Elkészült: {BEALLITAS_FAJL}")
        return

    inicializal(beallitasok_betolt())
    if a.test:
        ertesit("Teszt a hírfigyelőtől", "Ha ezt látod, működik az értesítés.")
        print("Tesztértesítés elküldve.")
        return
    if NTFY_TOPIC.startswith("CSERELD") and not a.szaraz:
        sys.exit("Előbb írd át az ntfy_topic értékét a beallitasok.json fájlban egy saját, egyedi névre.")
    if a.loop:
        while True:
            try:
                inicializal(beallitasok_betolt())  # így a szerkesztés újraindítás nélkül életbe lép
                egy_kor(kuldes=not a.szaraz)
            except Exception as e:
                print(f"[!] hiba: {e}", file=sys.stderr)
            time.sleep(int(CFG.get("percek", 10)) * 60)
    else:
        egy_kor(kuldes=not a.szaraz)


if __name__ == "__main__":
    main()
