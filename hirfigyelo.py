#!/usr/bin/env python3
"""Pénzügyi és Trump-hírfigyelő: hírfolyamokat néz, téma szerint szűri a fontos híreket,
push értesítést küld a telefonodra (ntfy.sh), és egy telefonbarát hírlistát is készít.

Csak a Python beépített moduljait használja, nincs mit telepíteni.
A témákat, szavakat és forrásokat a beallitasok.json fájlban tudod szerkeszteni.

Használat:
    python3 hirfigyelo.py                         # egyszeri ellenőrzés
    python3 hirfigyelo.py --loop                  # folyamatos figyelés
    python3 hirfigyelo.py --test                  # tesztértesítést küld (ntfy és/vagy Telegram)
    python3 hirfigyelo.py --telegram-id           # kiírja a Telegram chat azonosítódat
    python3 hirfigyelo.py --szaraz                # csak kiírja a híreket, nem küld értesítést
    python3 hirfigyelo.py --beallitasok-letrehoz  # kiírja az alap beallitasok.json fájlt
"""
import argparse
import hashlib
import html
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

MAPPA = Path(__file__).resolve().parent
BEALLITAS_FAJL = Path(os.environ.get("HIRFIGYELO_BEALLITASOK", MAPPA / "beallitasok.json"))
ALLAPOT_FAJL = Path(os.environ.get("HIRFIGYELO_ALLAPOT", MAPPA / "lattott.json"))
KULDOTT_FAJL = Path(os.environ.get("HIRFIGYELO_KULDOTT", MAPPA / "kuldott.json"))
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
    "percek": 15,
    "max_ertesites": 8,
    "max_kor_ora": 24,
    "max_hir_a_listaban": 80,
    # Fordítás: "hu" = az angol címeket magyarra fordítja (az eredeti zárójelben mellette marad).
    # Kikapcsolás: "fordit": "" . A már magyar címeket nem fordítja.
    "fordit": "hu",
    "max_fordit": 60,
    # Az angol eredeti cím megjelenése az értesítésben: "dolt" (dőlt betűk, minden telefonon működik),
    # "markdown" (*dőlt*, csak ha az ntfy appod támogatja), "sima" (semmi különleges).
    "eredeti_stilus": "dolt",
    # Nem sürgős hírekből legfeljebb ennyi óránként megy egyetlen összefoglaló (0 = azonnal, külön-külön).
    # Sürgős hír továbbra is azonnal jön. Ha nincs új hír, semmi sem megy.
    "osszefoglalo_ora": 3,
    # Csendes órák: ebben az idősávban nem megy ki értesítés, a hírek összegyűlnek, és reggel egyben érkeznek.
    "csendes_tol": 22,
    "csendes_ig": 7,
    # Reggeli összefoglaló (az éjszakai fontos hírek + árfolyamok), a megadott órától az első futáskor
    "reggeli_ora": 7,
    "reggeli_arfolyamok": {
        "Nvidia": "NVDA", "Apple": "AAPL", "Microsoft": "MSFT", "Alphabet": "GOOGL", "Amazon": "AMZN",
        "Meta": "META", "Tesla": "TSLA", "Broadcom": "AVGO", "Bitcoin": "BTC-USD",
    },
    # Két sürgős értesítés között legalább ennyi perc teljen el; ami közben jön, az a következő összefoglalóba kerül
    "surgos_szunet_perc": 0,
    # Az a téma, amin "napi_osszefoglalo": true van (alapból a háborús), naponta legfeljebb EGY összefoglalót küld,
    # ennyi órától (helyi idő) kezdve. Kivétel: ha a cím a "sulyos_szavak" valamelyikét tartalmazza,
    # az azonnal, sürgősként megy ki.
    "napi_ora": 18,
    "sulyos_szavak": [
        "ceasefire", "cease-fire", "truce", "peace deal", "peace agreement", "peace accord", "peace treaty",
        "agreement", "agrees", "agreed", "nuclear strike", "nuclear attack", "invasion", "invades", "invaded",
        "declares war", "declared war", "martial law", "mass casualties",
        "tűzszünet*", "megállapod*", "békemegállapodás*", "békeszerződés*", "nukleáris támadás*",
        "atomtámadás*", "invázió*", "hadüzenet*", "hadiállapot*",
    ],
    # Értesítési csatornák: ntfy és/vagy Telegram-bot (mindkettő mehet egyszerre).
    # A Telegram-bot tokenjét és a chat azonosítót ide ne írd, ha a repó nyilvános:
    # használd a TELEGRAM_BOT_TOKEN és TELEGRAM_CHAT_ID GitHub secreteket.
    "ntfy_aktiv": True,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_api_url": "",  # üresen a közvetlen Telegram-API; vagy a saját Cloudflare Worker címe
    # Telegram: NYILVÁNOS csatornák neve @ nélkül, például ["valamicsatorna", "masikcsatorna"].
    # Privát csoportot és csoportbeszélgetést nem tud olvasni.
    "telegram_csatornak": [],
    # Az összefoglalók témánként csoportosítva jönnek, ezekkel az ikonokkal a fejlécben.
    "ikonok": {
        "Trump – háború": "🪖", "Trump – döntések": "🏛️", "Trump": "🇺🇸", "Kripto": "₿", "Forex": "💱",
        "Tőzsde": "📈", "Jegybank/kamat": "🏦", "Világgazdaság": "🌍", "Blue chip": "🏢",
    },
    "temak": {
        "Trump – háború": {
            "aktiv": True,
            "napi_osszefoglalo": True,
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
    # Sürgős = erős szó (2 pont) VAGY legalább két különböző közepes szó (1-1 pont); a küszöb a surgos_kuszob.
    "surgos_eros": [
        "breaking", "just in", "urgent", "azonnali", "crash", "crashed", "plunge", "plunged", "plummet",
        "plummeted", "collapse", "collapsed", "halts trading", "trading halted", "circuit breaker", "default",
        "emergency", "bankruptcy", "files for bankruptcy", "profit warning", "stock split",
        "cuts interest rates", "raises interest rates", "rate decision", "executive order", "ceasefire",
        "invasion", "zuhan*", "csőd*", "összeomlás*", "hadüzenet*",
    ],
    "surgos": [
        "surge", "surged", "surging", "soar", "soared", "soaring", "tumble", "tumbled", "tumbling",
        "record high", "all-time high", "rate cut", "rate hike", "tariff", "sanction", "ban", "approves",
        "hack", "hacked", "downgrade", "layoffs", "recall", "veto", "pardon", "beats estimates",
        "beat estimates", "misses estimates", "missed estimates", "raises guidance", "cuts guidance",
        "earnings beat", "earnings miss", "attack", "strike", "missile", "nuclear", "war",
        "szárnyal*", "rekord*", "támadás*", "háború*",
    ],
    "surgos_kuszob": 2,
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
SULYOS_RE = None
EROS_RE = None
SURGOS_KUSZOB = 2
NAPI = set()
NTFY_AKTIV = False
TG_TOKEN = ""
TG_CHAT = ""
TG_API = "https://api.telegram.org"
TG_SZUNET_IG = 0.0  # ennyi időpontig (epoch) nem próbálkozunk a Telegrammal (korlátozás után)
TG_HIBAK = 0  # egymás utáni, teljesen elbukott Telegram-küldések ebben a futásban
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
    global CFG, FEEDEK, TEMA_RE, SURGOS_RE, KIZART_RE, SULYOS_RE, EROS_RE, SURGOS_KUSZOB, NAPI, NTFY_TOPIC, NTFY_AKTIV, TG_TOKEN, TG_CHAT, TG_API
    CFG = cfg
    NTFY_TOPIC = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    NTFY_AKTIV = bool(cfg.get("ntfy_aktiv", True)) and bool(NTFY_TOPIC) and not NTFY_TOPIC.startswith("CSERELD")
    # Telegram-értesítés: a bot tokenjét és a chat azonosítót inkább környezeti változóban (GitHub secret) add meg
    TG_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("telegram_bot_token", "")).strip()
    TG_CHAT = str(os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id", "")).strip()
    # Ha a Telegram korlátozza a GitHub szervereit (429), egy saját közvetítőn (pl. Cloudflare Worker) át is küldhetünk
    TG_API = (os.environ.get("TELEGRAM_API_URL") or cfg.get("telegram_api_url", "") or "https://api.telegram.org").strip().rstrip("/")
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
    EROS_RE = kulcs_regex(cfg.get("surgos_eros", []))
    SURGOS_KUSZOB = int(cfg.get("surgos_kuszob", 2))
    SULYOS_RE = kulcs_regex(cfg.get("sulyos_szavak", []))
    NAPI = {nev for nev, t in cfg["temak"].items() if t.get("aktiv", True) and t.get("napi_osszefoglalo")}
    KIZART_RE = kulcs_regex(cfg.get("kizart", []))


def letolt(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _szam(x, tizedes=2):
    return f"{x:,.{tizedes}f}".replace(",", " ").replace(".", ",")


def _egy_arfolyam(par):
    nev, jel = par
    try:
        adat = json.loads(letolt("https://query1.finance.yahoo.com/v8/finance/chart/"
                                 f"{urllib.parse.quote(jel)}?interval=1d&range=7d", timeout=10).decode("utf-8"))
        r = adat["chart"]["result"][0]
        ar = float(r["meta"]["regularMarketPrice"])
        zarasok = [float(c) for c in r["indicators"]["quote"][0]["close"] if c is not None]
        elozo = None
        if len(zarasok) >= 2:
            # ha az utolsó nap értéke az aktuális ár, az előző nap záró az összehasonlítás alapja
            elozo = zarasok[-2] if abs(zarasok[-1] - ar) / ar < 0.005 else zarasok[-1]
        return nev, jel, ar, ((ar / elozo - 1) * 100 if elozo else None)
    except Exception:
        pass
    if jel == "BTC-USD":  # tartalék forrás a bitcoinhoz
        try:
            d = json.loads(letolt("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
                                  "&include_24hr_change=true", timeout=10).decode("utf-8"))["bitcoin"]
            return nev, jel, float(d["usd"]), d.get("usd_24h_change")
        except Exception:
            pass
    return nev, jel, None, None


def arfolyamok_szoveg():
    """Rövid árfolyamlista a reggeli üzenethez; ha nem érhető el, üres szöveget ad."""
    lista = CFG.get("reggeli_arfolyamok") or {}
    if not lista:
        return ""
    with ThreadPoolExecutor(max_workers=8) as ex:
        eredmeny = list(ex.map(_egy_arfolyam, lista.items()))
    sorok = []
    for nev, jel, ar, valt in eredmeny:
        if ar is None:
            continue
        iv = "" if valt is None else f" {'▲' if valt >= 0 else '▼'}{_szam(abs(valt), 1)}%"
        egyseg = "pont" if jel.startswith("^") else "USD"  # az indexek pontban, a többi dollárban
        sorok.append(f"{nev} {_szam(ar, 0 if ar >= 10000 else 2)} {egyseg}{iv}")
    return "\n".join(sorok)  # a fejlécet az üzenetépítő adja hozzá


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


def surgos_pont(cim):
    """Sürgősségi pont: egy erős szó 2 pont, minden további különböző közepes szó 1 pont. Küszöb: surgos_kuszob."""
    pont = 2 if EROS_RE.search(cim) else 0
    return pont + len({m.group(0).lower().rstrip("s") for m in SURGOS_RE.finditer(cim)})


def elemzes(cim, leiras):
    """Visszaad: (témák listája, sürgős-e). Üres lista = nem érdekes."""
    t = cim + " " + leiras
    if KIZART_RE.search(t):
        return [], False
    temak = [nev for nev, (rx, es) in TEMA_RE.items() if rx.search(t) and (es is None or es.search(t))]
    if any(n in NAPI for n in temak):
        # napi összefoglalós téma: csak a tényleg súlyos hír (pl. megállapodás) sürgős
        return temak, bool(SULYOS_RE.search(cim))
    return temak, bool(temak) and surgos_pont(cim) >= SURGOS_KUSZOB


def cim_kulcs(cim):
    """Ugyanaz a cím két forrásból ne jöjjön kétszer ('... - Reuters' végződés nélkül)."""
    c = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", cim.lower())
    return "t" + hashlib.sha1(re.sub(r"\W+", "", c).encode("utf-8")).hexdigest()[:14]


STOP = set("the a an of to in on for and or at by with as is are was were be from that this it its after over "
           "new says say said will would could may amid vs into than more has have had not but "
           "trump market markets stock stocks news report reports update live today price".split())


def tokenek(cim):
    """A cím lényegi szavai (tövezevel): így az ugyanarról szóló, de másként megfogalmazott hírt felismerjük."""
    c = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", cim.lower())
    ki = set()
    for w in re.findall(r"\w+", c):
        if len(w) <= 2 or w in STOP:
            continue
        for veg in ("ing", "ed", "es", "s"):
            if w.endswith(veg) and len(w) - len(veg) >= 4:
                w = w[:-len(veg)]
                break
        ki.add(w[:6])
    return sorted(ki)


def hasonlo(a, b):
    a, b = set(a), set(b)
    if len(a) < 3 or len(b) < 3:
        return False
    k = len(a & b)
    jac = k / len(a | b)
    ov = k / min(len(a), len(b))
    return (k >= 2 and jac >= 0.5) or (k >= 3 and (jac >= 0.3 or ov >= 0.55))


def feed_letolt(elem):
    nev, url = elem
    try:
        if url.startswith("tg:"):
            return nev, telegram_elemek(letolt("https://t.me/s/" + url[3:]).decode("utf-8", "replace"), url[3:]), None
        return nev, feed_elemek(letolt(url)), None
    except Exception as e:
        return nev, [], e


def link_kulcs(link):
    return "l" + hashlib.sha1(link.encode("utf-8")).hexdigest()[:14]


FORD_STAT = {"ok": 0, "hiba": 0, "kihagyva": 0, "mm": 0}


def forditas(szoveg_, cel="hu"):
    """Cím fordítása. Először a Google (kulcs nélküli végpont), kétszer próbálva,
    ha az nem megy, a MyMemory ingyenes szolgáltatás. Ha mindkettő hibázik, az eredeti szöveg marad,
    de a hiba bekerül a naplóba és a statisztikába."""
    hiba = None
    for kiserlet in range(2):
        try:
            url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl="
                   + urllib.parse.quote(cel) + "&dt=t&q=" + urllib.parse.quote(szoveg_))
            adat = json.loads(letolt(url, timeout=10).decode("utf-8"))
            ford = "".join(r[0] for r in adat[0] if r and r[0]).strip()
            if ford:
                FORD_STAT["ok"] += 1
                time.sleep(0.15)
                return ford
        except Exception as e:
            hiba = e
            time.sleep(1.5)
    try:
        url = ("https://api.mymemory.translated.net/get?q=" + urllib.parse.quote(szoveg_)
               + "&langpair=en|" + urllib.parse.quote(cel))
        adat = json.loads(letolt(url, timeout=10).decode("utf-8"))
        ford = html.unescape((adat.get("responseData") or {}).get("translatedText", "")).strip()
        if ford and "MYMEMORY WARNING" not in ford.upper():
            FORD_STAT["ok"] += 1
            FORD_STAT["mm"] += 1
            return ford
    except Exception as e:
        hiba = hiba or e
    FORD_STAT["hiba"] += 1
    print(f"[!] fordítás nem sikerült ({hiba.__class__.__name__ if hiba else 'üres válasz'}): {szoveg_[:50]}",
          file=sys.stderr)
    return szoveg_


MAGYAR_RE = re.compile(r"[áéíóöőúüűÁÉÍÓÖŐÚÜŰ]")


def json_betolt(fajl, alap):
    try:
        return json.loads(fajl.read_text(encoding="utf-8"))
    except Exception:
        return alap


def dolt(szoveg_):
    """Latin betűk átalakítása dőlt (Unicode sans-serif italic) változatra. Más karakter marad."""
    ki = []
    for ch in szoveg_:
        if "a" <= ch <= "z":
            ki.append(chr(0x1D622 + ord(ch) - 97))
        elif "A" <= ch <= "Z":
            ki.append(chr(0x1D608 + ord(ch) - 65))
        else:
            ki.append(ch)
    return "".join(ki)


def eredeti_formaz(szoveg_):
    st = CFG.get("eredeti_stilus", "dolt")
    if st == "dolt":
        return "🇬🇧 " + dolt(szoveg_)
    if st == "markdown":
        return "🇬🇧 *" + szoveg_.replace("*", "") + "*"
    return szoveg_


def forras_cimke(nev):
    """A Google News-lekérdezések neve a témát ismételné ("Blue chip | Blue chip (Google News)"), ezért
    ott csak "Google News" látszik; a valódi kiadó úgyis a cím végén áll (pl. "- Benzinga")."""
    nev = str(nev)
    return "Google News" if nev.endswith("(Google News)") else nev


def fordito_link(cim, link, forras=""):
    """Google Fordító-link magyarra. Közvetlen cikklinknél a teljes oldalt fordítja,
    Google News átirányító linknél (ott az oldalfordítás nem megbízható) a címet.
    Kikapcsolás: \"fordito_gomb\": false. A magyar címhez nem ad linket."""
    if CFG.get("fordito_gomb") or not CFG.get("fordito_link", True) or not cim or MAGYAR_RE.search(cim):
        return None  # ha a Telegram-gomb be van kapcsolva, a link felesleges
    if "portfolio" in (forras or "").lower() or "magyar" in (forras or "").lower():
        return None  # magyar nyelvű forrás
    if link and "news.google.com" not in link:
        return "https://translate.google.com/translate?sl=auto&tl=hu&u=" + urllib.parse.quote(link, safe="")
    return "https://translate.google.com/?sl=auto&tl=hu&op=translate&text=" + urllib.parse.quote(cim[:500])


def ido_szoveg(ts):
    """Mai hírnél csak óra:perc, régebbinél hónap.nap. óra:perc."""
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%m.%d. %H:%M")


def csoportosit(tetelek):
    """Hírek (rekord-szótárak) témánként, a beállításokban megadott témasorrendben.
    Egy hír oda kerül, ahol az első (legfontosabb) témája van. Témán belül: sürgős elöl, majd legfrissebb."""
    sorrend = list(CFG.get("temak", {}).keys())
    csop = {}
    for o in tetelek:
        csop.setdefault((o.get("temak") or ["Egyéb"])[0], []).append(o)
    return [(t, sorted(csop[t], key=lambda o: (not o.get("surgos"), -o["ido"])))
            for t in sorted(csop, key=lambda t: sorrend.index(t) if t in sorrend else len(sorrend))]


def szekcios_reszek(tetelek, fejlec=True, max_sima=3300, max_html=3600, max_reszek=6):
    """Témák szerint tagolt üzenet(ek). Visszaad egy listát: [(sima szöveg az ntfy-hez, HTML a Telegramhoz), ...].
    Ha a hírek nem férnek el egy üzenetben (a Telegram üzenetenként max. 4096 karaktert enged, ez nem függ
    a Premium előfizetéstől), több üzenetre bontja őket. Legfeljebb max_reszek üzenetet ad; ami ezen túl
    maradna, arról az utolsó üzenet végén jelzés van."""
    reszek = []
    sima, htm = [], []
    hs = hh = 0
    db = 0      # hír az aktuális üzenetben (a fordító-gomb számozásához)
    kiirt = 0   # összes kiírt hír
    ikonok = CFG.get("ikonok", {})

    def lezar():
        nonlocal sima, htm, hs, hh, db
        if sima:
            reszek.append(("\n".join(sima), "\n".join(htm)))
        sima, htm, hs, hh, db = [], [], 0, 0, 0

    tele = False
    for tema, elemek in csoportosit(tetelek):
        if tele:
            break
        ikon = ikonok.get(tema, "📰")
        elso = True      # kell-e témafejléc az aktuális üzenetben
        folyt = False    # a téma már átcsúszott egy új üzenetbe
        for o in elemek:
            while True:
                fej_s = fej_h = None
                if fejlec and elso:
                    pot = " (folyt.)" if folyt else ""
                    fej_s = f"{ikon} {tema.upper()} ({len(elemek)}){pot}"
                    fej_h = f"<b>{ikon} {html.escape(tema)}</b> ({len(elemek)}){html.escape(pot)}"
                jel = "🔴 " if o.get("surgos") else ""
                s = f"• {jel}{o['cim']} ({forras_cimke(o['forras'])})"
                cim_h = html.escape(o["cim"])
                if o.get("link"):
                    cim_h = f'<a href="{html.escape(o["link"], quote=True)}">{cim_h}</a>'
                jelzo = f"{db + 1}." if CFG.get("fordito_gomb") else "•"
                h = f"{jelzo} {jel}{cim_h} <i>{html.escape(forras_cimke(o['forras']))}</i>"
                fl = fordito_link(o["cim"], o.get("link"), o.get("forras", ""))
                h_hossz = 0
                if fl:
                    fl_h = html.escape(fl, quote=True)
                    h += f'\n   ↳ <a href="{fl_h}">🇭🇺 Fordítás magyarra</a>'
                    h_hossz = len(fl_h)
                extra_s = (len(fej_s) + 2 if fej_s else 0) + (2 if sima else 0)
                extra_h = (len(fej_h) + 2 if fej_h else 0) + (2 if htm else 0)
                nem_fer = hs + extra_s + len(s) + 1 > max_sima or hh + extra_h + len(h) - h_hossz + 1 > max_html
                if nem_fer and sima:
                    # az aktuális üzenet megtelt: lezárjuk, és új üzenetben folytatjuk
                    lezar()
                    if len(reszek) >= max_reszek:
                        tele = True
                        break
                    elso, folyt = True, True
                    continue
                break
            if tele:
                break
            if fej_s:
                sima.append(("\n" if sima else "") + fej_s)
                htm.append(("\n" if htm else "") + fej_h)
                hs += len(sima[-1]) + 1
                hh += len(htm[-1]) + 1
            elso = False
            sima.append(s)
            htm.append(h)
            hs += len(s) + 1
            hh += len(h) - h_hossz + 1
            db += 1
            kiirt += 1
    lezar()
    if kiirt < len(tetelek) and reszek:
        maradt = f"\n… és még {len(tetelek) - kiirt} hír (lásd a hírlistát)"
        reszek[-1] = (reszek[-1][0] + maradt, reszek[-1][1] + html.escape(maradt))
    return reszek


def szekcios_uzenet(tetelek, fejlec=True, max_sima=3300, max_html=3600):
    """Régi, együzenetes változat (az első részt adja vissza)."""
    r = szekcios_reszek(tetelek, fejlec, max_sima, max_html, max_reszek=1)
    return r[0] if r else ("", "")


def ntfy_kuld(cim, uzenet, link=None, surgos=False, html_uzenet=None):
    # JSON API-t használunk, így az ékezetes szöveg is hibátlanul megy át
    adat = {"topic": NTFY_TOPIC, "title": cim, "message": uzenet, "priority": 4 if surgos else 3,
            "tags": ["rotating_light" if surgos else "chart_with_upwards_trend"]}
    if CFG.get("eredeti_stilus") == "markdown":
        adat["markdown"] = True
    if link:
        adat["click"] = link
    req = urllib.request.Request(NTFY_SERVER.rstrip("/") + "/", data=json.dumps(adat).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **UA})
    urllib.request.urlopen(req, timeout=20).read()


class CsakIPv4:
    """Ideiglenesen csak IPv4-en old fel neveket (néha az IPv6 út akad a GitHub szervereiről)."""
    def __enter__(self):
        self.eredeti = socket.getaddrinfo
        eredeti = self.eredeti
        socket.getaddrinfo = lambda host, port, family=0, *a, **k: eredeti(host, port, socket.AF_INET, *a, **k)

    def __exit__(self, *kivetel):
        socket.getaddrinfo = self.eredeti


def fordito_gombok(uzenet, html_uzenet=None):
    """Telegram-gombok az üzenet alá: egy gomb hírenként (1, 2, 3 ...), megnyomásra a bot (Cloudflare Worker)
    felugró ablakban, csak a gombot megnyomónak mutatja a hír magyar fordítását. A csatornában senki más
    üzenete nem változik. Kikapcsolás: "fordito_gomb": false."""
    if not CFG.get("fordito_gomb"):
        return None
    if html_uzenet:
        n = sum(1 for l in html_uzenet.split("\n") if re.match(r"\d+\. ", l))
        if n == 0:
            return None
        n = min(n, 24)
        gombok = [{"text": f"🇭🇺 {i}", "callback_data": f"t:{i}"} for i in range(1, n + 1)]
        if n == 1:
            gombok[0]["text"] = "🇭🇺 Fordítás magyarra"
        return [gombok[i:i + 5] for i in range(0, len(gombok), 5)]
    elso = (uzenet or "").split("\n")[0]
    if not elso or MAGYAR_RE.search(elso):
        return None
    return [[{"text": "🇭🇺 Fordítás magyarra", "callback_data": "t:1"}]]


def telegram_kuld(cim, uzenet, link=None, surgos=False, html_uzenet=None):
    global TG_HIBAK, TG_SZUNET_IG
    if time.time() < TG_SZUNET_IG:
        raise RuntimeError(f"Telegram szünetel még ~{int((TG_SZUNET_IG - time.time()) / 60) + 1} percig (korábbi korlátozás miatt)")
    if TG_HIBAK >= 2:
        raise RuntimeError("Telegram ebben a futásban kihagyva (ismételt időtúllépés)")
    # szerkezetes üzenetnél a HTML-változatot küldjük (kattintható címek), egyébként a sima szöveget
    szov = f"<b>{html.escape(cim)}</b>\n\n" + (html_uzenet if html_uzenet else html.escape(uzenet[:3500]))
    if link and not html_uzenet:
        szov += f'\n\n<a href="{html.escape(link, quote=True)}">Megnyitás</a>'
        fl = fordito_link(uzenet.split("\n")[0], link)
        if fl:
            szov += f' · <a href="{html.escape(fl, quote=True)}">🇭🇺 Fordítás magyarra</a>'
    kuldes = {"chat_id": TG_CHAT, "text": szov, "parse_mode": "HTML", "disable_web_page_preview": True}
    gombok = fordito_gombok(uzenet, html_uzenet)
    if gombok:
        kuldes["reply_markup"] = {"inline_keyboard": gombok}
    adat = json.dumps(kuldes).encode("utf-8")
    utolso = None
    hatarido = time.time() + 45  # egy üzenetre legfeljebb ennyi ideig próbálkozunk
    kiserlet = 0
    while kiserlet < 8 and (kiserlet == 0 or time.time() < hatarido):
        # böngészős User-Agent kell: a Cloudflare a gyári Python-azonosítót tiltja (1010-es hiba); a 3. próbától csak IPv4-en
        req = urllib.request.Request(f"{TG_API}/bot{TG_TOKEN}/sendMessage", data=adat,
                                     headers={"Content-Type": "application/json", **UA})
        kiserlet += 1
        try:
            if kiserlet < 3:
                urllib.request.urlopen(req, timeout=15).read()
            else:
                with CsakIPv4():
                    urllib.request.urlopen(req, timeout=15).read()
            TG_HIBAK = 0
            time.sleep(0.5)  # a Telegram korlátoz a gyors egymás utáni üzeneteket
            return
        except urllib.error.HTTPError as e:
            torzs = e.read()[:300].decode("utf-8", "replace")
            if e.code == 429:
                # a GitHub szerverei közös IP-n osztoznak, ezért a Telegram néha korlátoz: kivárjuk a megadott időt
                try:
                    var = int(json.loads(torzs).get("parameters", {}).get("retry_after", 2))
                except Exception:
                    var = 2
                utolso = RuntimeError(f"HTTP 429: {torzs}")
                if var > 20:
                    # hosszú tiltás: a további próbálkozás csak növeli, ezért ebben a futásban feladjuk
                    TG_HIBAK = 2
                    TG_SZUNET_IG = time.time() + 1800  # fél órát pihentetjük a Telegramot, hogy a korlát lejárhasson
                    raise RuntimeError(f"HTTP 429: a Telegram {var} mp várakozást kér, most nem próbálkozom tovább") from None
                time.sleep(min(var + 1, 20))
                continue
            raise RuntimeError(f"HTTP {e.code}: {torzs}") from None  # más 4xx: újrapróbálás nem segít
        except Exception as e:
            utolso = e
            time.sleep(2)
    TG_HIBAK += 1
    if TG_HIBAK >= 2:
        TG_SZUNET_IG = time.time() + 1800
    raise utolso


def teszt_cimzettre_valt():
    """Teszt/próba módban csak a külön teszt-címzett kap üzenetet (TELEGRAM_TEST_CHAT_ID, NTFY_TEST_TOPIC),
    a felhasználók semmit sem látnak. Ha nincs teszt-címzett beállítva, inkább nem küld semmit."""
    global NTFY_TOPIC, NTFY_AKTIV, TG_CHAT
    nt = os.environ.get("NTFY_TEST_TOPIC", "").strip()
    tc = os.environ.get("TELEGRAM_TEST_CHAT_ID", "").strip()
    NTFY_TOPIC, NTFY_AKTIV = nt, bool(nt)
    TG_CHAT = tc
    if not (NTFY_AKTIV or (TG_TOKEN and TG_CHAT)):
        sys.exit("Nincs teszt-címzett beállítva (TELEGRAM_TEST_CHAT_ID és/vagy NTFY_TEST_TOPIC secret), "
                 "ezért nem küldök semmit: így a felhasználók nem kapnak felesleges üzenetet.")


def telegram_diagnozis():
    """Megmutatja, hogy innen (pl. GitHub szerver) elérhető-e a Telegram, és melyik úton."""
    print("Telegram diagnózis" + ("" if "api.telegram.org" in TG_API else " (közvetítőn át)"))
    try:
        cimek = sorted({(("IPv6" if a[0] == socket.AF_INET6 else "IPv4"), a[4][0])
                        for a in socket.getaddrinfo(urllib.parse.urlparse(TG_API).hostname, 443, type=socket.SOCK_STREAM)})
        print("  feloldott címek:", ", ".join(f"{t} {i}" for t, i in cimek))
    except Exception as e:
        print("  névfeloldás hiba:", e)
    if not TG_TOKEN:
        print("  nincs token, a további próbák kimaradnak")
        return
    for nev, ctx in (("alap", None), ("csak IPv4", CsakIPv4)):
        t0 = time.time()
        try:
            req = urllib.request.Request(f"{TG_API}/bot{TG_TOKEN}/getMe", headers=UA)
            if ctx:
                with ctx():
                    r = urllib.request.urlopen(req, timeout=15).read()
            else:
                r = urllib.request.urlopen(req, timeout=15).read()
            print(f"  getMe [{nev}]: OK {time.time() - t0:.1f} mp, bot: "
                  f"{(json.loads(r).get('result') or {}).get('username')}")
        except urllib.error.HTTPError as e:
            print(f"  getMe [{nev}]: HTTP {e.code} {e.read()[:150].decode('utf-8', 'replace')}")
        except Exception as e:
            print(f"  getMe [{nev}]: HIBA {e.__class__.__name__}: {e} ({time.time() - t0:.1f} mp)")


def ertesit(cim, uzenet, link=None, surgos=False, html_uzenet=None):
    """Minden bekapcsolt csatornára elküldi. Csak akkor hiba, ha mindegyik csatorna elbukott."""
    csatornak = []
    if NTFY_AKTIV:
        csatornak.append(("ntfy", ntfy_kuld))
    if TG_TOKEN and TG_CHAT:
        csatornak.append(("Telegram", telegram_kuld))
    if not csatornak:
        raise RuntimeError("nincs beállított értesítési csatorna (ntfy vagy Telegram)")
    hibak = []
    for nev, fv in csatornak:
        try:
            fv(cim, uzenet, link, surgos, html_uzenet)
        except Exception as e:
            hibak.append(e)
            print(f"[!] {nev} értesítés nem ment ki ({e.__class__.__name__}: {str(e).replace(TG_TOKEN, '***') if TG_TOKEN else e})",
                  file=sys.stderr)
    if len(hibak) == len(csatornak):
        raise hibak[0]


FF_NAPTAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ZEN_ZSEBEL = {"EUR": "🇪🇺", "USD": "🇺🇸"}
TRUMP_IKON = {"High": "🔴", "Medium": "🟠", "Low": "🟡"}


def trump_kulcs(d, cim):
    return f"{d.strftime('%Y-%m-%d %H:%M')}|{cim}"


def trump_allapot_ment(lattott):
    """A már jelzett Trump-események mentése (a 2 napnál régebbieket eldobja)."""
    from datetime import timedelta
    hatar = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    allapot = json_betolt(KULDOTT_FAJL, {})
    if not isinstance(allapot, dict):
        allapot = {}
    allapot["trump_beszed"] = {k: v for k, v in lattott.items() if k[:10] >= hatar}
    KULDOTT_FAJL.write_text(json.dumps(allapot, ensure_ascii=False), encoding="utf-8")


def trump_jelol(ma, trump):
    """A reggeli naptárban már szereplő Trump-eseményeket ismertnek jelöli, hogy ne jöjjenek újra."""
    try:
        allapot = json_betolt(KULDOTT_FAJL, {})
        lattott = dict(allapot.get("trump_beszed", {})) if isinstance(allapot, dict) else {}
        for d, penz, cim, imp, elo, elozo in trump:
            lattott.setdefault(trump_kulcs(d, cim), "naptar")
        trump_allapot_ment(lattott)
    except Exception as e:
        print(f"[!] Trump-állapot mentése nem sikerült: {e}", file=sys.stderr)


# ===================== BŐVÍTMÉNYEK: emlékeztetők, heti előnézet, árfigyelés, jelentések, működésfigyelés =====================
HU_NAPOK = ["hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap"]
FF_KOV_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
NASDAQ_JEL_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
_FF_MEMO, _JEL_MEMO, SZOLG = {}, {}, {}

# alapértelmezések (a beallitasok.json-ban szerkeszthetők; ha ott nincs megadva, ezek érvényesek)
AR_ALAP = {
    "Arany": {"jel": "GC=F", "szazalek": 1.0},
    "Ezüst": {"jel": "SI=F", "szazalek": 2.0},
    "Olaj (WTI)": {"jel": "CL=F", "szazalek": 2.0},
    "Olaj (Brent)": {"jel": "BZ=F", "szazalek": 2.0},
    "Bitcoin": {"jel": "BTC-USD", "szazalek": 3.0},
    "EUR/USD": {"jel": "EURUSD=X", "szazalek": 0.5},
    "Nvidia": {"jel": "NVDA", "szazalek": 3.0},
    "Apple": {"jel": "AAPL", "szazalek": 3.0},
    "Microsoft": {"jel": "MSFT", "szazalek": 3.0},
    "Alphabet": {"jel": "GOOGL", "szazalek": 3.0},
    "Amazon": {"jel": "AMZN", "szazalek": 3.0},
    "Meta": {"jel": "META", "szazalek": 3.0},
    "Tesla": {"jel": "TSLA", "szazalek": 4.0},
    "Broadcom": {"jel": "AVGO", "szazalek": 3.0},
    "MOL": {"jel": "MOL.BD", "szazalek": 3.0},
    "OTP": {"jel": "OTP.BD", "szazalek": 3.0},
    "Richter": {"jel": "RICHTER.BD", "szazalek": 3.0},
    "Magyar Telekom": {"jel": "MTELEKOM.BD", "szazalek": 3.0},
}
JEL_ALAP = {
    "NVDA": "Nvidia", "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Alphabet", "GOOG": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla", "AVGO": "Broadcom", "TSM": "TSMC", "AMD": "AMD",
    "INTC": "Intel", "NFLX": "Netflix", "ORCL": "Oracle", "PLTR": "Palantir", "JPM": "JPMorgan",
    "GS": "Goldman Sachs", "BRK.B": "Berkshire", "WMT": "Walmart", "V": "Visa", "MA": "Mastercard",
    "BA": "Boeing", "XOM": "Exxon", "LLY": "Eli Lilly", "PFE": "Pfizer", "ASML": "ASML", "MU": "Micron",
    "ARM": "Arm Holdings",
}


def _pct(x):
    return f"{x:+.1f}".replace(".", ",") + "%"


def ff_nyers(url=None):
    """A Forex Factory heti JSON nyers listája. Futásonként/5 percenként csak egyszer kérdezi le (a szolgáltatás korlátoz)."""
    url = url or FF_NAPTAR_URL
    m = _FF_MEMO.get(url)
    if m and time.time() - m[0] < 300:
        return m[1]
    adat = json.loads(letolt(url, timeout=25).decode("utf-8"))
    if not isinstance(adat, list):
        raise RuntimeError("váratlan válasz a naptárból")
    _FF_MEMO[url] = (time.time(), adat)
    return adat


def csendes_most(most_dt=None):
    from zoneinfo import ZoneInfo
    most_dt = most_dt or datetime.now(ZoneInfo("Europe/Budapest"))
    ora = most_dt.hour
    tol, ig = int(CFG.get("csendes_tol", 22)), int(CFG.get("csendes_ig", 7))
    return (ora >= tol or ora < ig) if tol > ig else (tol <= ora < ig)


def allapot_kulcs(kulcs, alap=None):
    a = json_betolt(KULDOTT_FAJL, {})
    return a.get(kulcs, alap) if isinstance(a, dict) else alap


def allapot_kulcs_ment(kulcs, ertek):
    a = json_betolt(KULDOTT_FAJL, {})
    if not isinstance(a, dict):
        a = {}
    a[kulcs] = ertek
    KULDOTT_FAJL.write_text(json.dumps(a, ensure_ascii=False), encoding="utf-8")


# ---------- működésfigyelés (csak a teszt-címzettnek / adminnak megy) ----------
def admin_ertesit(cim, uzenet):
    """Csak a teszt-címzettnek küld (TELEGRAM_TEST_CHAT_ID / NTFY_TEST_TOPIC); a felhasználók nem látják."""
    global NTFY_TOPIC, NTFY_AKTIV, TG_CHAT
    nt = os.environ.get("NTFY_TEST_TOPIC", "").strip()
    tc = os.environ.get("TELEGRAM_TEST_CHAT_ID", "").strip()
    if not (nt or (TG_TOKEN and tc)):
        print(f"[!] nincs teszt-címzett, az admin-üzenet nem megy ki: {cim}: {uzenet}", file=sys.stderr)
        return False
    regi = (NTFY_TOPIC, NTFY_AKTIV, TG_CHAT)
    NTFY_TOPIC, NTFY_AKTIV, TG_CHAT = nt, bool(nt), tc
    try:
        ertesit(cim, uzenet, None, False)
        return True
    except Exception as e:
        print(f"[!] admin-üzenet nem ment ki ({e.__class__.__name__}: {e})", file=sys.stderr)
        return False
    finally:
        NTFY_TOPIC, NTFY_AKTIV, TG_CHAT = regi


def szolg(nev, ok):
    """Egy szolgáltatás (hírforrás, naptár, árfolyam, jelentések) ebben a futásban sikerült-e."""
    SZOLG[nev] = bool(ok)


def szolg_kiertekeles():
    """Ha egy szolgáltatás a `figyeles_ora` óránál (alap: 6) tovább folyamatosan hibázik, egyszer szól az adminnak
    (24 óránként újra, amíg tart), és jelzi, ha helyreállt."""
    if not SZOLG:
        return
    most = time.time()
    adat = allapot_kulcs("szolg_hibak", {})
    if not isinstance(adat, dict):
        adat = {}
    kuszob = float(CFG.get("figyeles_ora", 6)) * 3600
    uzenetek, valtozott = [], False
    for nev, ok in SZOLG.items():
        e = adat.get(nev)
        if ok:
            if e:
                if e.get("jelezve"):
                    uzenetek.append(f"✅ Helyreállt: {nev}")
                del adat[nev]
                valtozott = True
            continue
        if not e:
            e = adat[nev] = {"tol": most, "jelezve": 0}
            valtozott = True
        if most - e["tol"] >= kuszob and most - e.get("jelezve", 0) >= 24 * 3600:
            uzenetek.append(f"⚠️ Nem működik: {nev} (kb. {int((most - e['tol']) / 3600)} órája)")
            e["jelezve"] = most
            valtozott = True
    if valtozott:
        allapot_kulcs_ment("szolg_hibak", adat)
    if uzenetek:
        admin_ertesit("🛠 Hírfigyelő – állapot", "\n".join(uzenetek))


# ---------- gyorsjelentések (blue chip) ----------
def letolt_fejlec(url, fejlec, timeout=20):
    h = dict(UA)
    h.update(fejlec)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def jelentes_nap(datum):
    if datum in _JEL_MEMO:
        return _JEL_MEMO[datum]
    nyers = letolt_fejlec(NASDAQ_JEL_URL.format(d=datum),
                          {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
                           "Referer": "https://www.nasdaq.com/"}, 20)
    sorok = (json.loads(nyers.decode("utf-8")).get("data") or {}).get("rows") or []
    _JEL_MEMO[datum] = sorok
    return sorok


def jelentesek(datumok):
    """A figyelt blue chip cégek gyorsjelentései a megadott napokra (Nasdaq naptár) + a kézzel megadottak.
    Visszaad: ({datum: [(név, mikor, EPS-várakozás)]}, sikerült-e a lekérés)."""
    tick = {k.upper(): v for k, v in (CFG.get("jelentes_tickerek") or JEL_ALAP).items()}
    kezi = CFG.get("jelentesek_kezi") or {}
    ki, ok = {}, True
    for d in datumok:
        sorok_ = []
        try:
            lattott_nev = set()
            for r in jelentes_nap(d):
                sz = str(r.get("symbol", "")).upper().replace("/", ".")
                if sz in tick and tick[sz] not in lattott_nev:
                    lattott_nev.add(tick[sz])
                    mikor = {"time-pre-market": "nyitás előtt", "time-after-hours": "zárás után"}.get(
                        r.get("time"), "időpont nincs megadva")
                    sorok_.append((tick[sz], mikor, r.get("epsForecast") or ""))
        except Exception as e:
            ok = False
            print(f"[!] Nasdaq jelentési naptár nem érhető el ({e.__class__.__name__}: {e})", file=sys.stderr)
        for k in kezi.get(d, []):
            sorok_.append((k, "", ""))
        if sorok_:
            ki[d] = sorok_
    szolg("Nasdaq jelentési naptár", ok)
    return ki, ok


def jelentes_sor(nev, mikor, eps):
    return f"📊 {nev}" + (f" – gyorsjelentés, {mikor}" if mikor else "") + (f" (EPS-várakozás: {eps})" if eps else "")


# ---------- piros mappás emlékeztető + Trump-események ----------
def ff_piros_dt(penznemek, ma):
    from zoneinfo import ZoneInfo
    bud = ZoneInfo("Europe/Budapest")
    ki = []
    for e in ff_nyers(FF_NAPTAR_URL):
        if e.get("impact") != "High" or e.get("country") not in penznemek:
            continue
        try:
            d = datetime.fromisoformat(e["date"]).astimezone(bud)
        except Exception:
            continue
        if d.strftime("%Y-%m-%d") == ma:
            ki.append((d, e["country"], str(e.get("title", "")).strip(), e.get("forecast") or "", e.get("previous") or ""))
    ki.sort(key=lambda x: x[0])
    return ki


def trump_beszed_ellenorzes():
    """Napközbeni figyelés a Forex Factory naptárára:
    - bármilyen színű Trump-esemény (beszéd, sajtótájékoztató): azonnali jelzés a pontos időponttal + emlékeztető indulás előtt;
    - piros mappás EUR/USD esemény: emlékeztető az indulás előtt (naptar_emlekezteto_perc, alap 20 perc).
    Csendes órákban nem küld (a reggeli naptár tartalmazza az aznapi eseményeket)."""
    trump_be = CFG.get("trump_beszed_aktiv", True)
    emlek_p = int(CFG.get("naptar_emlekezteto_perc", 20))
    if not trump_be and not emlek_p:
        return 0
    from zoneinfo import ZoneInfo
    most_dt = datetime.now(ZoneInfo("Europe/Budapest"))
    ma = most_dt.strftime("%Y-%m-%d")
    if csendes_most(most_dt):
        return 0
    if time.time() - float(allapot_kulcs("ff_utolso", 0) or 0) < float(CFG.get("ff_min_perc", 8)) * 60:
        return 0  # a Forex Factory korlátoz, ha túl sűrűn kérdezik
    allapot_kulcs_ment("ff_utolso", time.time())
    penznemek = CFG.get("naptar_penznemek") or ["EUR", "USD"]
    try:
        _, _, trump = ff_naptar(penznemek, ma)
        piros_dt = ff_piros_dt(penznemek, ma)
        szolg("Forex Factory naptár", True)
    except Exception as e:
        szolg("Forex Factory naptár", False)
        print(f"[!] naptár-figyelés: a Forex Factory nem érhető el ({e.__class__.__name__}: {e})", file=sys.stderr)
        return 0
    lattott = dict(allapot_kulcs("trump_beszed", {}) or {})
    emlek_t = int(CFG.get("trump_emlekezteto_perc", 20))
    kuldeni = []  # ([kulcsok], cím, üzenet)
    if trump_be:
        for d, penz, cim, imp, elo, elozo in trump:
            perc = (d - most_dt).total_seconds() / 60
            if perc < -60:
                continue  # már lezajlott
            k = trump_kulcs(d, cim)
            ikon = TRUMP_IKON.get(imp, "⚪")
            if k not in lattott:
                lattott[k] = "uj"
                if 0 < perc <= emlek_t:
                    lattott[k + "#r"] = "r"  # indulás előtt közvetlenül derült ki: emlékeztető már nem kell
                kuldeni.append(([k, k + "#r"] if k + "#r" in lattott else [k], f"🎤 Trump – {d.strftime('%H:%M')}",
                                f"{ikon} {d.strftime('%H:%M')} – {cim}\n(Forex Factory szerint, budapesti idő; mappa színe: {ikon})"))
            elif emlek_t and 0 < perc <= emlek_t and (k + "#r") not in lattott:
                lattott[k + "#r"] = "r"
                kuldeni.append(([k + "#r"], f"⏰ Trump {int(perc) + 1} percen belül – {d.strftime('%H:%M')}",
                                f"{ikon} {d.strftime('%H:%M')} – {cim}"))
    if emlek_p:
        csop, kulcsok = [], []
        for d, penz, cim, elo, elozo in piros_dt:
            perc = (d - most_dt).total_seconds() / 60
            k = f"{d.strftime('%Y-%m-%d %H:%M')}|P|{cim}#r"
            if 0 < perc <= emlek_p and k not in lattott:
                lattott[k] = "r"
                kulcsok.append(k)
                csop.append((d.strftime("%H:%M"), penz, cim, elo, elozo))
        if csop:
            sima, _ = naptar_sorok(csop, "🔴")
            kuldeni.append((kulcsok, f"⏰ Piros mappás hír {csop[0][0]}-kor", "\n".join(sima)))
    if not kuldeni:
        return 0
    trump_allapot_ment(lattott)  # előbb mentünk, így hiba esetén sem megy ki kétszer
    kiment = 0
    for kulcsok, cim, uzenet in kuldeni:
        try:
            ertesit(cim, uzenet, None, True)
            kiment += 1
            STAT_UJ["trump" if cim.startswith("🎤") else "emlek"] += 1
        except Exception as e:
            print(f"[!] naptár-értesítés nem ment ki ({e.__class__.__name__}: {e})", file=sys.stderr)
            for k in kulcsok:
                lattott.pop(k, None)
            trump_allapot_ment(lattott)
    return kiment


# ---------- heti előnézet (vasárnap este) ----------
def ff_nap_csoport(adat, penznemek, nap_szuro):
    """Az események napokra bontva: {nap: {"piros": [...], "trump": [...], "szurke": [...]}} (budapesti idő)."""
    from zoneinfo import ZoneInfo
    bud = ZoneInfo("Europe/Budapest")
    ki, lattott = {n: {"piros": [], "trump": [], "szurke": []} for n in nap_szuro}, set()
    for e in adat:
        imp, cim, orszag = e.get("impact"), str(e.get("title", "")).strip(), e.get("country")
        kul = (str(e.get("date")), cim, orszag)
        if kul in lattott:
            continue
        lattott.add(kul)
        trump_e = "trump" in cim.lower() and imp != "Holiday"
        if not trump_e and (imp not in ("High", "Holiday") or orszag not in penznemek):
            continue
        if imp == "Holiday":
            n = str(e.get("date", ""))[:10]
            if n in ki:
                ki[n]["szurke"].append(("egész nap", orszag, cim, "", ""))
            continue
        try:
            d = datetime.fromisoformat(e["date"]).astimezone(bud)
        except Exception:
            continue
        n = d.strftime("%Y-%m-%d")
        if n not in ki:
            continue
        if trump_e:
            ki[n]["trump"].append((d, orszag, cim, imp or ""))
        if imp == "High" and orszag in penznemek:
            ki[n]["piros"].append((d, orszag, cim, e.get("forecast") or "", e.get("previous") or ""))
    return ki


def kovetkezo_hetfo(most_dt=None):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    most_dt = most_dt or datetime.now(ZoneInfo("Europe/Budapest"))
    return (most_dt + timedelta(days=(7 - most_dt.weekday()) % 7 or 7)).date()


def heti_elonezet():
    """A következő hét (hétfő–péntek) piros mappás EUR/USD eseményei, Trump-események és blue chip gyorsjelentések
    napokra bontva. True, ha elment; False, ha a naptár még nem elérhető (később újra próbálja)."""
    from datetime import timedelta
    penznemek = CFG.get("naptar_penznemek") or ["EUR", "USD"]
    hetfo = kovetkezo_hetfo()
    napok = [hetfo + timedelta(days=i) for i in range(5)]
    nap_s = [d.strftime("%Y-%m-%d") for d in napok]
    nyers, sikeres = [], 0
    for url in (FF_NAPTAR_URL, FF_KOV_URL):
        try:
            nyers += ff_nyers(url)
            sikeres += 1
        except Exception as e:
            print(f"[!] heti előnézet: {url} nem érhető el ({e.__class__.__name__}: {e})", file=sys.stderr)
    szolg("Forex Factory heti naptár", sikeres > 0)
    csop = ff_nap_csoport(nyers, penznemek, nap_s) if nyers else {}
    if not csop or not any(v["piros"] or v["trump"] or v["szurke"] for v in csop.values()):
        return False  # a következő hét naptára még nincs kint
    jel, jel_ok = jelentesek(nap_s)
    blokkok = []
    for dt, n in zip(napok, nap_s):
        v = csop[n]
        sorok_ = [f"{HU_NAPOK[dt.weekday()].capitalize()} ({dt.strftime('%m.%d.')})"]
        if v["piros"]:
            sorok_ += naptar_sorok([(d.strftime("%H:%M"), p, c, f, e) for d, p, c, f, e in v["piros"]], "🔴")[0]
        else:
            sorok_.append("✅ Nincs piros mappás hír.")
        for d, p, c, imp in v["trump"]:
            if imp != "High":
                sorok_.append(f"🎤 {TRUMP_IKON.get(imp, '⚪')} {d.strftime('%H:%M')} – {c}")
        for ido, p, c, _, _ in v["szurke"]:
            sorok_.append(f"⚪ {ido} {ZEN_ZSEBEL.get(p, '')} {p} – {c}")
        for nev, mikor, eps in jel.get(n, []):
            sorok_.append(jelentes_sor(nev, mikor, eps))
        blokkok.append("\n".join(sorok_))
    if not jel_ok:
        blokkok.append("(A gyorsjelentések listája most nem érhető el.)")
    # Telegram-limit miatt részekre bontjuk
    reszek, akt = [], ""
    for b in blokkok:
        if akt and len(akt) + len(b) > 3300:
            reszek.append(akt)
            akt = ""
        akt += ("\n\n" if akt else "") + b
    if akt:
        reszek.append(akt)
    alap = f"📆 Heti előnézet – {napok[0].strftime('%m.%d.')}–{napok[-1].strftime('%m.%d.')} (budapesti idő)"
    for i, r in enumerate(reszek, 1):
        ertesit(alap if len(reszek) == 1 else f"{alap} [{i}/{len(reszek)}]", r, None, False)
    return True


def heti_ellenorzes():
    if not CFG.get("heti_aktiv", True):
        return 0
    from zoneinfo import ZoneInfo
    most_dt = datetime.now(ZoneInfo("Europe/Budapest"))
    if most_dt.weekday() != int(CFG.get("heti_nap", 6)) or not (int(CFG.get("heti_ora", 18)) <= most_dt.hour < 22):
        return 0
    het = kovetkezo_hetfo(most_dt).strftime("%Y-%m-%d")
    if allapot_kulcs("utolso_heti", "") == het:
        return 0
    allapot_kulcs_ment("utolso_heti", het)  # előbb mentünk: nem mehet ki kétszer
    ok = False
    try:
        ok = heti_elonezet()
    except Exception as e:
        print(f"[!] heti előnézet nem ment ki ({e.__class__.__name__}: {e})", file=sys.stderr)
    if not ok:
        allapot_kulcs_ment("utolso_heti", "")  # a következő futás újra próbálja (22:00-ig)
    return 1 if ok else 0


# ---------- árfigyelés: blue chipek, arany, ezüst, olaj, BTC, EUR/USD ----------
def ar_vizsgal(jel, ablak_perc, most):
    """Yahoo 5 perces adatai alapján: aktuális ár, változás az ablakban és a napi záróhoz képest.
    None, ha a piac zárva van (az utolsó adat 25 percnél régebbi)."""
    adat = json.loads(letolt("https://query1.finance.yahoo.com/v8/finance/chart/"
                             f"{urllib.parse.quote(jel)}?interval=5m&range=1d", timeout=12).decode("utf-8"))
    r = adat["chart"]["result"][0]
    ts, cl = r.get("timestamp") or [], (r["indicators"]["quote"][0].get("close") or [])
    pont = [(t, float(c)) for t, c in zip(ts, cl) if c is not None]
    if not pont:
        return None
    t_utolso, ar = pont[-1]
    if most - t_utolso > 25 * 60:
        return None
    reg = [p for p in pont if p[0] <= t_utolso - ablak_perc * 60]
    ref = reg[-1][1] if reg else pont[0][1]
    meta = r.get("meta", {})
    elozo = meta.get("chartPreviousClose") or meta.get("previousClose")
    return {"ar": ar, "valt": (ar / ref - 1) * 100, "nap": ((ar / float(elozo) - 1) * 100) if elozo else None,
            "penz": meta.get("currency", "")}


def ar_mozgasok():
    lista = CFG.get("ar_figyeles") or AR_ALAP
    ablak = int(CFG.get("ar_ablak_perc", 60))
    most = time.time()

    def egy(it):
        nev, c = it
        try:
            return nev, c["jel"], float(c.get("szazalek", 3.0)), ar_vizsgal(c["jel"], ablak, most), None
        except Exception as e:
            return nev, c.get("jel", "?"), float(c.get("szazalek", 3.0)), None, e
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(egy, lista.items()))


def ar_figyeles():
    """Ha egy figyelt eszköz az ablakban (alap: 60 perc) a megadott %-nál többet mozdul, vagy a napi mozgása
    eléri a küszöb ar_nap_szorzo-szorosát (alap 2,5), egyetlen üzenetben jelez. Zárt piacnál nem szól."""
    if not CFG.get("ar_figyeles_aktiv", True):
        return 0
    if csendes_most() and not CFG.get("ar_csendes_alatt", False):
        return 0
    from zoneinfo import ZoneInfo
    ma = datetime.now(ZoneInfo("Europe/Budapest")).strftime("%Y-%m-%d")
    most, szorzo = time.time(), float(CFG.get("ar_nap_szorzo", 2.5))
    ujra_s = float(CFG.get("ar_ujrajelzes_perc", 120)) * 60
    ablak = int(CFG.get("ar_ablak_perc", 60))
    eredm = ar_mozgasok()
    szolg("Yahoo árfolyamok", (not eredm) or any(e[4] is None for e in eredm))
    ref = allapot_kulcs("ar_riasztas", {})
    ref = {k: v for k, v in (ref if isinstance(ref, dict) else {}).items() if most - v.get("ts", 0) < 2 * 86400}
    sorok_, uj_kulcsok = [], []
    for nev, jel, szaz, v, hiba in eredm:
        if hiba or not v:
            continue
        jelez = False
        for tip, valt, kusz in (("a", v["valt"], szaz), ("n", v["nap"], szaz * szorzo)):
            if valt is None or abs(valt) < kusz:
                continue
            irany = 1 if valt > 0 else -1
            kulcs = f"{jel}|{tip}"
            e = ref.get(kulcs)
            if tip == "a":
                uj = (not e or most - e["ts"] >= ujra_s or e["irany"] != irany
                      or irany * (v["ar"] / e["ar"] - 1) * 100 >= szaz)
            else:
                uj = not e or e.get("nap") != ma or e["irany"] != irany
            if uj:
                ref[kulcs] = {"ts": most, "ar": v["ar"], "irany": irany, "nap": ma}
                uj_kulcsok.append(kulcs)
                jelez = True
        if jelez:
            ik = "🟢▲" if v["valt"] >= 0 else "🔴▼"
            sor = f"{ik} {nev}: {_szam(v['ar'], 0 if v['ar'] >= 10000 else 2)} {v['penz']} – {_pct(v['valt'])} / {ablak} perc"
            if v["nap"] is not None:
                sor += f", {_pct(v['nap'])} a tegnapi záróhoz képest"
            sorok_.append(sor)
    if not sorok_:
        return 0
    allapot_kulcs_ment("ar_riasztas", ref)  # előbb mentünk, így nem megy ki kétszer
    try:
        ertesit(f"⚡ Árfolyammozgás ({len(sorok_)})", "\n".join(sorok_), None, True)
        STAT_UJ["ar"] += len(sorok_)
        return 1
    except Exception as e:
        print(f"[!] árfolyam-értesítés nem ment ki ({e.__class__.__name__}: {e})", file=sys.stderr)
        for k in uj_kulcsok:
            ref.pop(k, None)
        allapot_kulcs_ment("ar_riasztas", ref)
        return 0


def ff_oldal_teszt():
    """Próba: eléri-e a GitHub-futtató a Forex Factory weboldalát, és látszanak-e benne az actual-cellák.
    Az eredményt kiírja a naplóba és elküldi a teszt-címzettnek."""
    most = datetime.now()
    url = f"https://www.forexfactory.com/calendar?day={most.strftime('%b').lower()}{most.day}.{most.year}"
    kod, nyers, hiba = None, b"", ""
    try:
        req = urllib.request.Request(url, headers={**UA, "Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(req, timeout=20) as r:
            kod, nyers = r.status, r.read()
    except urllib.error.HTTPError as e:
        kod, nyers = e.code, e.read()
    except Exception as e:
        hiba = f"{e.__class__.__name__}: {e}"
    szoveg_ = nyers.decode("utf-8", "replace")
    db = szoveg_.count("calendar__actual")
    vedett = any(x in szoveg_ for x in ("Just a moment", "cf-chl", "Attention Required", "challenge-platform"))
    i = szoveg_.find("calendar__actual")
    print(f"URL: {url}\nHTTP: {kod} {hiba}\nméret: {len(nyers)} bájt\nactual-cella: {db}\nvédelem (Cloudflare) jelei: {vedett}")
    print("--- részlet ---")
    print(szoveg_[max(0, i - 300): i + 900] if i >= 0 else szoveg_[:600])
    ertesit("Forex Factory oldal – próba",
            f"HTTP: {kod} {hiba}\nméret: {len(nyers)} bájt\nactual-cella: {db}\nvédelem jelei: {'IGEN' if vedett else 'nem'}\n"
            + ("→ az oldal elérhető és látszanak az actual-cellák." if kod == 200 and db and not vedett
               else "→ az oldal innen nem használható megbízhatóan."), None, False)


def ar_teszt():
    """Kiírja és a teszt-címzettnek elküldi az összes figyelt eszköz aktuális állását (a jelek ellenőrzéséhez)."""
    ablak = int(CFG.get("ar_ablak_perc", 60))
    sorok_ = []
    for nev, jel, szaz, v, hiba in ar_mozgasok():
        if hiba:
            sorok_.append(f"❌ {nev} ({jel}): nem elérhető ({hiba.__class__.__name__})")
        elif not v:
            sorok_.append(f"💤 {nev} ({jel}): a piac zárva / nincs friss adat")
        else:
            nap = f", nap: {_pct(v['nap'])}" if v["nap"] is not None else ""
            sorok_.append(f"✅ {nev} ({jel}): {_szam(v['ar'])} {v['penz']} – {ablak} perc: {_pct(v['valt'])}{nap} (küszöb {szaz}%)")
    szoveg_ = "\n".join(sorok_)
    print(szoveg_)
    ertesit("Árfigyelés teszt", szoveg_, None, False)


# ---------- heti statisztika az adminnak (finomhangoláshoz) ----------
def _stat_ures():
    return {"futas": 0, "hit": 0, "surgos": 0, "forras": {}, "tema": {}, "lista": [], "ar": 0, "emlek": 0,
            "trump": 0, "hiba": {}}


STAT_UJ = _stat_ures()


def stat_rogzit(uj, eredmenyek):
    """Egy futás találatainak gyűjtése (a futás végén egyszerre mentjük)."""
    S = STAT_UJ
    S["futas"] += 1
    for nev, _, hiba in eredmenyek:
        S["forras"].setdefault(nev, 0)
        if hiba:
            S["hiba"][nev] = S["hiba"].get(nev, 0) + 1
    for surgos, ido, nev, temak, cim, link in uj:
        S["hit"] += 1
        S["forras"][nev] = S["forras"].get(nev, 0) + 1
        if temak:
            S["tema"][temak[0]] = S["tema"].get(temak[0], 0) + 1
        if surgos:
            S["surgos"] += 1
            S["lista"].append({"f": nev, "c": str(cim)[:120]})


def stat_mentes():
    s = allapot_kulcs("stat", None)
    if not isinstance(s, dict):
        s = _stat_ures()
        s["tol"] = datetime.now().strftime("%Y-%m-%d")
    for k in ("futas", "hit", "surgos", "ar", "emlek", "trump"):
        s[k] = int(s.get(k, 0)) + STAT_UJ[k]
    for k in ("forras", "tema", "hiba"):
        d = s.setdefault(k, {})
        for kk, v in STAT_UJ[k].items():
            d[kk] = int(d.get(kk, 0)) + v
    s["lista"] = (s.get("lista", []) + STAT_UJ["lista"])[-40:]
    allapot_kulcs_ment("stat", s)
    STAT_UJ.clear()
    STAT_UJ.update(_stat_ures())


def stat_uzenet(s):
    sor = [f"📊 Heti statisztika ({s.get('tol', '?')} óta, {s.get('futas', 0)} futás)",
           f"Találat: {s.get('hit', 0)}, ebből sürgősnek jelölt: {s.get('surgos', 0)}",
           f"Jelzések: árfigyelés {s.get('ar', 0)}, piros mappás emlékeztető {s.get('emlek', 0)}, Trump-esemény {s.get('trump', 0)}"]
    tema = sorted(s.get("tema", {}).items(), key=lambda x: -x[1])
    if tema:
        sor += ["", "Témák: " + ", ".join(f"{k} {v}" for k, v in tema)]
    forr = sorted(s.get("forras", {}).items(), key=lambda x: -x[1])
    van = [f"{k} {v}" for k, v in forr if v][:12]
    if van:
        sor += ["", "Források (találat): " + ", ".join(van)]
    nulla = [k for k, v in forr if not v]
    if nulla:
        sor += ["", "Nulla találat (kikapcsolási jelölt vagy hibás): " + ", ".join(nulla)]
    hiba = sorted(s.get("hiba", {}).items(), key=lambda x: -x[1])
    if hiba:
        sor += ["", "Hibás lekérések: " + ", ".join(f"{k} {v}×" for k, v in hiba[:8])]
    lista = s.get("lista", [])
    if lista:
        sor += ["", "🔴 Sürgősnek jelölt hírek (ellenőrizd, tényleg azok voltak-e):"]
        sor += [f"• [{x['f']}] {x['c']}" for x in lista[-15:]]
    return "\n".join(sor)[:3800]


def stat_ellenorzes():
    """Vasárnap este (stat_nap/stat_ora, alap 6 és 20 óra) elküldi a statisztikát az adminnak, majd nullázza."""
    if not CFG.get("stat_aktiv", True):
        return 0
    from zoneinfo import ZoneInfo
    most_dt = datetime.now(ZoneInfo("Europe/Budapest"))
    if most_dt.weekday() != int(CFG.get("stat_nap", 6)) or not (int(CFG.get("stat_ora", 20)) <= most_dt.hour < 22):
        return 0
    nap = most_dt.strftime("%Y-%m-%d")
    if allapot_kulcs("stat_utolso", "") == nap:
        return 0
    allapot_kulcs_ment("stat_utolso", nap)
    s = allapot_kulcs("stat", None)
    if not isinstance(s, dict) or not s.get("futas"):
        return 0
    if admin_ertesit("📊 Hírfigyelő – heti statisztika", stat_uzenet(s)):
        allapot_kulcs_ment("stat", {**_stat_ures(), "tol": nap})
        return 1
    return 0


# ---------- minden bővítmény egy helyen, a hírellenőrzés végén ----------
def bovitmenyek(kuldes, elso_futas, eredmenyek, uj=()):
    for nev, _, hiba in eredmenyek:
        szolg(f"Hírforrás: {nev}", hiba is None)
    if kuldes:
        try:
            stat_rogzit(uj, eredmenyek)
        except Exception as e:
            print(f"[!] statisztika-gyűjtés hibázott ({e.__class__.__name__}: {e})", file=sys.stderr)
    if kuldes and not elso_futas:
        for fv in (trump_beszed_ellenorzes, ar_figyeles, heti_ellenorzes):
            try:
                fv()
            except Exception as e:
                print(f"[!] {fv.__name__} hibázott ({e.__class__.__name__}: {e})", file=sys.stderr)
    if kuldes:
        try:
            stat_mentes()
            stat_ellenorzes()
        except Exception as e:
            print(f"[!] statisztika hibázott ({e.__class__.__name__}: {e})", file=sys.stderr)
        try:
            szolg_kiertekeles()
        except Exception as e:
            print(f"[!] működésfigyelés hibázott ({e.__class__.__name__}: {e})", file=sys.stderr)


def ff_naptar(penznemek, ma):
    """Forex Factory heti JSON-csatornája: a mai PIROS (High) és SZÜRKE (Holiday) események, valamint a
    TRUMP-eseményei (beszéd stb.) a mappa színétől függetlenül.
    Visszaad: (piros, szurke, trump). piros/szurke: [(idő, pénznem, cím, előrejelzés, előző)];
    trump: [(datetime, pénznem, cím, impact, előrejelzés, előző)] időrendben."""
    from zoneinfo import ZoneInfo
    bud = ZoneInfo("Europe/Budapest")
    adat = ff_nyers(FF_NAPTAR_URL)
    piros, szurke, trump = [], [], []
    for e in adat:
        imp = e.get("impact")
        cim = str(e.get("title", "")).strip()
        trump_e = "trump" in cim.lower() and imp != "Holiday"
        if not trump_e and (imp not in ("High", "Holiday") or e.get("country") not in penznemek):
            continue
        if imp == "Holiday":
            # a szürke (ünnep) esemény egész napos: a dátum szerinti napot nézzük, időzóna-átváltás nélkül
            if str(e.get("date", ""))[:10] == ma:
                szurke.append(("egész nap", e["country"], cim, "", ""))
            continue
        try:
            d = datetime.fromisoformat(e["date"]).astimezone(bud)
        except Exception:
            continue
        if d.strftime("%Y-%m-%d") != ma:
            continue
        if trump_e:
            trump.append((d, e.get("country", ""), cim, imp or "", e.get("forecast") or "", e.get("previous") or ""))
        if imp == "High" and e.get("country") in penznemek:
            piros.append((d, e["country"], cim, e.get("forecast") or "", e.get("previous") or ""))
    piros.sort(key=lambda x: x[0]); trump.sort(key=lambda x: x[0])
    return [(d.strftime("%H:%M"), c, t, f, p) for d, c, t, f, p in piros], szurke, trump


def naptar_sorok(lista, jel="🔴"):
    sima, htm = [], []
    for ido, penz, cim, elo, elozo in lista:
        extra = " – ".join(x for x in (f"várható: {elo}" if elo else "", f"előző: {elozo}" if elozo else "") if x)
        s = f"{jel} {ido} {ZEN_ZSEBEL.get(penz, '')} {penz} – {cim}" + (f" ({extra})" if extra else "")
        sima.append(s)
        htm.append(html.escape(s))
    return sima, htm


def naptar_kuldes(ma, teszt=False):
    """Reggeli naptár-üzenet (Forex Factory): a mai piros mappás EUR/USD hírek névvel és budapesti időponttal;
    ha nincs, ezt kiírja. Szürke mappa (ünnep) és más színű Trump-események csak akkor szerepelnek, ha vannak.
    Mellette a mai/holnapi blue chip gyorsjelentések. True, ha elment."""
    penznemek = CFG.get("naptar_penznemek") or ["EUR", "USD"]
    sima_sorok, htm_sorok = [], []

    def hozzaad(sima, htm=None):
        sima_sorok.append(sima)
        htm_sorok.append(htm if htm is not None else html.escape(sima))

    try:
        piros, szurke, trump = ff_naptar(penznemek, ma)
        szolg("Forex Factory naptár", True)
    except Exception as e:
        szolg("Forex Factory naptár", False)
        print(f"[!] Forex Factory naptár nem érhető el ({e.__class__.__name__}: {e})", file=sys.stderr)
        return False  # a hívó újra próbálja
    db = len(piros)
    if piros:
        s_, h_ = naptar_sorok(piros, "🔴")
        sima_sorok.extend(s_); htm_sorok.extend(h_)
    else:
        hozzaad("✅ Nincs mára piros mappás hír.")
    if szurke:  # szürke mappa: csak akkor, ha aznap van
        s_, h_ = naptar_sorok(szurke, "⚪")
        hozzaad("")
        hozzaad("Szürke mappa (ünnep / nem gazdasági):")
        sima_sorok.extend(s_); htm_sorok.extend(h_)
    trump_t = [t for t in trump if t[3] != "High"]  # a piros Trump-esemény már fent szerepel
    if trump_t:
        hozzaad("")
        hozzaad("🎤 Trump-események (más színű mappa):")
        for d, penz, cim, imp, elo, elozo in trump_t:
            hozzaad(f"{TRUMP_IKON.get(imp, '⚪')} {d.strftime('%H:%M')} – {cim}")
    if not teszt:
        trump_jelol(ma, trump)
    try:
        from datetime import timedelta
        holnap = (datetime.strptime(ma, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        jel, jel_ok = jelentesek([ma, holnap])
        van = False
        for nap_, cimke in ((ma, "Mai"), (holnap, "Holnapi")):
            if jel.get(nap_):
                hozzaad("")
                hozzaad(f"📊 {cimke} blue chip gyorsjelentések:")
                for nev, mikor, eps in jel[nap_]:
                    hozzaad(jelentes_sor(nev, mikor, eps))
                van = True
        if not jel_ok and not van:
            hozzaad("")
            hozzaad("📊 A gyorsjelentések listája most nem érhető el.")
    except Exception as e:
        print(f"[!] gyorsjelentések nem érhetők el ({e.__class__.__name__}: {e})", file=sys.stderr)
    sima = "\n".join(sima_sorok).strip()
    htm = "\n".join(htm_sorok).strip()
    cim = (f"📅 Mai piros mappás hírek – {'/'.join(penznemek)} ({db})" if db
           else f"📅 Nincs mára piros mappás hír – {'/'.join(penznemek)}")
    ertesit(cim + (" [TESZT]" if teszt else ""), sima, None, False, htm)
    return True


def telegram_chat_id_kiir():
    """Segéd: kiírja azokat a chat azonosítókat, akik már írtak a botnak."""
    if not TG_TOKEN:
        sys.exit("Előbb add meg a bot tokenjét: TELEGRAM_BOT_TOKEN környezeti változó vagy telegram_bot_token a beállításokban.")
    adat = json.loads(letolt(f"{TG_API}/bot{TG_TOKEN}/getUpdates").decode("utf-8"))
    van = {}
    for u in adat.get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = m.get("chat") or {}
        if c.get("id") is not None:
            van[c["id"]] = c.get("title") or c.get("username") or c.get("first_name") or ""
    if not van:
        sys.exit("Még nincs üzenet. Írj egy üzenetet a botodnak a Telegramban (pl. /start), aztán futtasd újra.")
    for i, n in van.items():
        print(f"chat_id: {i}   ({n})")


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
    global TG_SZUNET_IG
    t0 = time.time()
    lattott = json_betolt(ALLAPOT_FAJL, None)
    elso_futas = lattott is None
    lattott = lattott or []
    halmaz = set(lattott)
    most = time.time()
    max_kor = float(CFG.get("max_kor_ora", 24)) * 3600
    uj, lista_uj = [], []

    # a hírforrásokat egyszerre kérdezi le, így a futás gyors marad
    with ThreadPoolExecutor(max_workers=8) as ex:
        eredmenyek = list(ex.map(feed_letolt, FEEDEK.items()))

    for nev, elemek, hiba in eredmenyek:
        if hiba:
            print(f"[!] {nev}: nem sikerült ({hiba.__class__.__name__}: {hiba})", file=sys.stderr)
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

    # --- ugyanarról a történetről ne jöjjön több értesítés (más forrás, más cím) ---
    kall = json_betolt(KULDOTT_FAJL, {})
    if not isinstance(kall, dict):
        kall = {}
    TG_SZUNET_IG = float(kall.get("tg_szunet_ig", 0))
    tg_szunet_regi = TG_SZUNET_IG
    mult = [x for x in kall.get("kuldott", []) if most - x.get("ido", 0) < 48 * 3600]
    fuggo = [x for x in kall.get("fuggo", []) if most - x.get("ido", 0) < max_kor]
    utolso = float(kall.get("utolso_osszefoglalo", 0))
    utolso_reggeli = kall.get("utolso_reggeli", "")
    utolso_surgos = float(kall.get("utolso_surgos", 0))
    napi_fuggo = [x for x in kall.get("napi_fuggo", []) if most - x.get("ido", 0) < max(max_kor, 36 * 3600)]
    utolso_nap = kall.get("utolso_napi", "")
    ismert = [x["t"] for x in mult] + [x["t"] for x in fuggo] + [x["t"] for x in napi_fuggo]
    szurt = []
    for tet in uj:
        tk = tokenek(tet[4])
        if any(hasonlo(tk, m) for m in ismert):
            continue
        ismert.append(tk)
        szurt.append((tet, tk))

    # --- fordítás magyarra (először az értesítésre szánt hírek, utána a lista többi tagja) ---
    eredeti = {}
    cel = CFG.get("fordit", "")
    if cel:
        fm, keret = {}, int(CFG.get("max_fordit", 60))
        for tet in [x[0] for x in szurt] + lista_uj:
            c = tet[4]
            if c in fm:
                continue
            if len(fm) >= keret or time.time() - t0 > 150:
                FORD_STAT["kihagyva"] += 1
                continue
            fm[c] = c if MAGYAR_RE.search(c) else forditas(c, cel)
        for orig, mag in fm.items():
            if mag != orig:
                eredeti[mag] = orig
        szurt = [((s_, i, n, t, fm.get(c, c), l), tk) for ((s_, i, n, t, c, l), tk) in szurt]
        lista_uj = [(s_, i, n, t, fm.get(c, c), l) for (s_, i, n, t, c, l) in lista_uj]
    # --- második szűrés a (lefordított) magyar címekkel: így a más nyelvű / más szavú azonos hír is kiesik ---
    ismert_hu = [x["th"] for x in mult + fuggo + napi_fuggo if x.get("th")]
    tokenek_hu, szurt2 = {}, []
    for tet, tk in szurt:
        th = tokenek(tet[4])
        if any(hasonlo(th, m) for m in ismert_hu):
            continue
        ismert_hu.append(th)
        tokenek_hu[tet[4]] = th
        szurt2.append((tet, tk))
    szurt = szurt2
    tokenek_cim = {x[0][4]: x[1] for x in szurt}

    def rekord(x):
        s_, i, n, t, c, l = x
        return {"ido": i, "forras": n, "temak": t, "cim": c, "link": l, "surgos": s_,
                "eredeti": eredeti.get(c, ""), "t": tokenek_cim.get(c, tokenek(c)),
                "th": tokenek_hu.get(c, tokenek(c))}

    surgosak = [x[0] for x in szurt if x[0][0]]
    for x in (x[0] for x in szurt if not x[0][0]):
        (napi_fuggo if any(n in NAPI for n in x[3]) else fuggo).append(rekord(x))

    # --- idő: csendes órák, reggeli összefoglaló ---
    most_dt = datetime.now()
    ma = most_dt.strftime("%Y-%m-%d")
    ora = most_dt.hour
    tol, ig = int(CFG.get("csendes_tol", 22)), int(CFG.get("csendes_ig", 7))
    csendes = (ora >= tol or ora < ig) if tol > ig else (tol <= ora < ig)
    reggeli_ora = int(CFG.get("reggeli_ora", 7))

    # a sürgős hír sem megy ki azonnal, ha csendes óra van, vagy nemrég már ment egy: összefoglalóba kerül
    szunet = float(CFG.get("surgos_szunet_perc", 0)) * 60
    if surgosak and (csendes or most - utolso_surgos < szunet):
        fuggo.extend(rekord(x) for x in surgosak)
        surgosak = []
    if surgosak:
        utolso_surgos = most

    osszefoglalo, reggeli = [], False
    kozte = float(CFG.get("osszefoglalo_ora", 3)) * 3600
    if not csendes and reggeli_ora <= ora < reggeli_ora + 5 and ma != utolso_reggeli and not elso_futas:
        # reggeli összefoglaló: minden összegyűlt hír, a sürgősek elöl
        reggeli = True
        utolso_reggeli = ma
        fuggo.sort(key=lambda x: (not x.get("surgos"), -x["ido"]))
        osszefoglalo, fuggo = fuggo[:15], fuggo[15:]
        utolso = most
    elif not csendes and fuggo and (most - utolso >= kozte):
        fuggo.sort(key=lambda x: x["ido"], reverse=True)
        osszefoglalo, fuggo = fuggo[:maxe], fuggo[maxe:]
        utolso = most

    # napi összefoglaló (pl. háború): naponta egyszer, a megadott órától, ha van mit küldeni
    napi_kuld = []
    if not csendes and napi_fuggo and ma != utolso_nap and ora >= int(CFG.get("napi_ora", 18)):
        napi_fuggo.sort(key=lambda x: x["ido"], reverse=True)
        napi_kuld, napi_fuggo = napi_fuggo[:15], []
        utolso_nap = ma

    # reggeli naptár (Forex Factory piros mappás EUR/USD): naponta egyszer, a megadott időponttól
    utolso_naptar = kall.get("utolso_naptar", "")
    naptar_probak = kall.get("naptar_probak", {})
    naptar_most = False
    if (CFG.get("naptar_aktiv", True) and not elso_futas and ma != utolso_naptar
            and (ora, most_dt.minute) >= (int(CFG.get("naptar_ora", 7)), int(CFG.get("naptar_perc", 30)))
            and ora < int(CFG.get("naptar_ora", 7)) + 5
            and naptar_probak.get(ma, 0) < 4):
        naptar_most = True
        utolso_naptar = ma
        naptar_probak = {ma: naptar_probak.get(ma, 0) + 1}

    # --- ÁLLAPOT MENTÉSE AZ ÉRTESÍTÉS ELŐTT: így semmi sem mehet ki kétszer, még hiba/időtúllépés esetén sem ---
    if kuldes and not elso_futas:
        for x in surgosak:
            mult.append({"ido": most, "t": tokenek_cim.get(x[4], tokenek(x[4])),
                         "th": tokenek_hu.get(x[4], tokenek(x[4]))})
        for x in osszefoglalo + napi_kuld:
            mult.append({"ido": most, "t": x["t"], "th": x.get("th", x["t"])})
    elif kuldes:
        fuggo, utolso = [], most
    if kuldes:
        KULDOTT_FAJL.write_text(json.dumps({**kall, "kuldott": mult[-500:], "fuggo": fuggo, "utolso_osszefoglalo": utolso,
                                            "napi_fuggo": napi_fuggo, "utolso_napi": utolso_nap,
                                            "utolso_reggeli": utolso_reggeli, "utolso_surgos": utolso_surgos,
                                            "utolso_naptar": utolso_naptar, "naptar_probak": naptar_probak,
                                            "trump_beszed": kall.get("trump_beszed", {}),
                                            "tg_szunet_ig": TG_SZUNET_IG},
                                           ensure_ascii=False), encoding="utf-8")
    ALLAPOT_FAJL.write_text(json.dumps(lattott[-10000:]))
    hirlista_ment(lista_uj, eredeti)

    def kuld_egyet(x):
        surgos, _, nev, temak, cim, link = x
        uzenet = cim + (f"\n\n{eredeti_formaz(eredeti[cim])}" if cim in eredeti else "")
        ikon = CFG.get("ikonok", {}).get(temak[0], "📰")
        ertesit(f"{'🔴 ' if surgos else ''}{ikon} {' · '.join(temak[:2])} | {forras_cimke(nev)}", uzenet, link, surgos)

    def rek(x):  # (sürgős, idő, forrás, témák, cím, link) -> rekord-szótár
        return {"ido": x[1], "forras": x[2], "temak": x[3], "cim": x[4], "link": x[5], "surgos": x[0]}

    def szekcios(cim, tetelek, fejlec=True, link=None, surgos=False):
        reszek = szekcios_reszek(tetelek, fejlec)
        lnk = link or next((o["link"] for o in tetelek if o.get("link")), None)
        for i, (sima, htm) in enumerate(reszek, 1):
            ertesit(cim if len(reszek) == 1 else f"{cim} [{i}/{len(reszek)}]", sima, lnk, surgos, htm)

    def biztonsagos(fv, *args):
        try:
            fv(*args)
        except Exception as e:
            print(f"[!] értesítés nem ment ki ({e.__class__.__name__}: {e})", file=sys.stderr)

    if cel:
        print(f"Fordítás: {FORD_STAT['ok']} sikeres (ebből MyMemory: {FORD_STAT['mm']}), "
              f"{FORD_STAT['hiba']} sikertelen, {FORD_STAT['kihagyva']} kihagyva (keret/idő), "
              f"futásidő eddig: {int(time.time() - t0)} mp")
    kiment = 0
    if kuldes:
        if elso_futas:
            biztonsagos(ertesit, "Hírfigyelő elindult", "Innentől csak az új, fontos híreket küldöm.")
        else:
            # sürgős hírek azonnal, de a futásonként ugyanabba a témába esőket EGY értesítésbe vonjuk össze
            csoportok = {}
            for x in surgosak:
                csoportok.setdefault(x[3][0], []).append(x)
            csoport_lista = list(csoportok.values())
            for n, cs in enumerate(csoport_lista):
                if n >= maxe:
                    maradek = [y for c in csoport_lista[n:] for y in c]
                    biztonsagos(ertesit, f"+{len(maradek)} további sürgős hír",
                                "\n".join("• " + y[4] for y in maradek[:15]))
                    break
                if len(cs) == 1:
                    biztonsagos(kuld_egyet, cs[0])
                else:
                    ikon = CFG.get("ikonok", {}).get(cs[0][3][0], "📰")
                    biztonsagos(szekcios, f"🔴 {ikon} {cs[0][3][0]} | {len(cs)} hír",
                                [rek(y) for y in cs[:10]], False, None, True)
            kiment += len(surgosak)
            # nem sürgős hírek: egyetlen összefoglaló, témánként csoportosítva
            if reggeli:
                def reggeli_kuld():
                    ar = ""
                    try:
                        ar = arfolyamok_szoveg()
                    except Exception as e:
                        print(f"[!] árfolyamok nem érhetők el ({e.__class__.__name__})", file=sys.stderr)
                    reszek = szekcios_reszek(osszefoglalo) if osszefoglalo else []
                    if not reszek:
                        reszek = [("Éjszaka nem volt fontos hír.", html.escape("Éjszaka nem volt fontos hír."))]
                    if ar:
                        sima, htm = reszek[-1]
                        sima += "\n\n📊 ÁRFOLYAMOK (legutóbbi záró)\n" + ar
                        htm += "\n\n<b>📊 Árfolyamok</b> (legutóbbi záró)\n" + html.escape(ar)
                        reszek[-1] = (sima, htm)
                    alap = f"☀️ Reggeli összefoglaló ({len(osszefoglalo)} hír)" if osszefoglalo else "☀️ Reggeli összefoglaló"
                    for i, (sima, htm) in enumerate(reszek, 1):
                        ertesit(alap if len(reszek) == 1 else f"{alap} [{i}/{len(reszek)}]", sima,
                                osszefoglalo[0]["link"] if osszefoglalo else None, False, htm)
                biztonsagos(reggeli_kuld)
            elif len(osszefoglalo) == 1:
                o = osszefoglalo[0]
                biztonsagos(kuld_egyet, (o["surgos"], o["ido"], o["forras"], o["temak"], o["cim"], o["link"]))
            elif osszefoglalo:
                biztonsagos(szekcios, f"📋 Hírösszefoglaló ({len(osszefoglalo)} hír)", osszefoglalo)
            kiment += len(osszefoglalo)
            # napi összefoglaló (háború): egy téma, ezért külön témafejléc nélkül
            if napi_kuld:
                biztonsagos(szekcios, f"🗓️ Napi összefoglaló – {', '.join(sorted(NAPI))} ({len(napi_kuld)} hír)",
                            napi_kuld, False)
            kiment += len(napi_kuld)
    else:
        for x in surgosak + [(o["surgos"], o["ido"], o["forras"], o["temak"], o["cim"], o["link"]) for o in osszefoglalo + napi_kuld]:
            print(f"{'!!' if x[0] else '  '} [{'/'.join(x[3])}] {x[2]}: {x[4]}  {x[5]}")
            kiment += 1

    if kuldes and naptar_most:
        try:
            ok = naptar_kuldes(ma)
        except Exception as e:
            print(f"[!] naptár értesítés nem ment ki ({e.__class__.__name__}: {e})", file=sys.stderr)
            ok = False
        if ok:
            kiment += 1
        else:
            # semelyik forrás nem válaszolt: a következő futás újra próbálja (legfeljebb 4-szer aznap)
            try:
                allapot = json_betolt(KULDOTT_FAJL, {})
                allapot["utolso_naptar"] = ""
                KULDOTT_FAJL.write_text(json.dumps(allapot, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print(f"[!] naptár állapot visszaállítása nem sikerült: {e}", file=sys.stderr)

    bovitmenyek(kuldes, elso_futas, eredmenyek, uj)

    if kuldes and TG_SZUNET_IG != tg_szunet_regi:
        # a Telegram korlátozott: a szünet időpontját elmentjük, hogy a következő futások ne terheljék tovább
        try:
            allapot = json_betolt(KULDOTT_FAJL, {})
            allapot["tg_szunet_ig"] = TG_SZUNET_IG
            KULDOTT_FAJL.write_text(json.dumps(allapot, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[!] szünet mentése nem sikerült: {e}", file=sys.stderr)

    print(f"Kész: {kiment} értesítés, {len(fuggo)} vár összefoglalóra, {len(napi_fuggo)} vár a napi összefoglalóra" + (" (első futás, alapállapot mentve)" if elso_futas else ""))
    return kiment


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="folyamatos figyelés")
    ap.add_argument("--test", action="store_true", help="tesztértesítés küldése")
    ap.add_argument("--szaraz", action="store_true", help="csak kiírja a híreket, nem küld értesítést")
    ap.add_argument("--kozlemeny", metavar="SZOVEG", help="a megadott közleményt kiküldi a felhasználóknak (a híreket nem ellenőrzi)")
    ap.add_argument("--teszt-cimzett", action="store_true", help="a közlemény/teszt csak a teszt-címzettnek megy")
    ap.add_argument("--naptar", action="store_true", help="a mai piros mappás EUR/USD naptárat azonnal elküldi (a --teszt-cimzett-tel csak a teszt-címzettnek)")
    ap.add_argument("--heti", action="store_true", help="a következő hét előnézetét azonnal elküldi (a --teszt-cimzett-tel csak a teszt-címzettnek)")
    ap.add_argument("--ar-teszt", action="store_true", help="az összes figyelt eszköz állását a teszt-címzettnek küldi (jelek ellenőrzése)")
    ap.add_argument("--ff-oldal-teszt", action="store_true", help="eléri-e a futtató a Forex Factory weboldalát (actual-értékekhez)")
    ap.add_argument("--stat", action="store_true", help="az eddigi statisztikát a teszt-címzettnek küldi (nullázás nélkül)")
    ap.add_argument("--admin-teszt", action="store_true", help="próba-üzenet a működésfigyelő csatornára (teszt-címzett)")
    ap.add_argument("--telegram-id", action="store_true", help="kiírja a Telegram chat azonosítódat (a botnak írt üzenetből)")
    ap.add_argument("--telegram-diag", action="store_true", help="Telegram-elérés diagnosztika")
    ap.add_argument("--beallitasok-letrehoz", action="store_true", help="kiírja az alap beallitasok.json fájlt")
    a = ap.parse_args()

    if a.beallitasok_letrehoz:
        BEALLITAS_FAJL.write_text(json.dumps(ALAP, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Elkészült: {BEALLITAS_FAJL}")
        return

    inicializal(beallitasok_betolt())
    if a.telegram_diag:
        telegram_diagnozis()
        return
    if a.telegram_id:
        telegram_chat_id_kiir()
        return
    if a.kozlemeny is not None:
        szov = a.kozlemeny.strip()
        if not szov:
            sys.exit("Üres közlemény, nem küldök semmit.")
        if a.teszt_cimzett:
            teszt_cimzettre_valt()
        ertesit("📢 Közlemény" + (" (előnézet)" if a.teszt_cimzett else ""), szov)
        print("Közlemény elküldve" + (" (csak a teszt-címzettnek)." if a.teszt_cimzett else " a felhasználóknak."))
        return
    if a.ff_oldal_teszt:
        teszt_cimzettre_valt()
        ff_oldal_teszt()
        return
    if a.ar_teszt:
        teszt_cimzettre_valt()
        ar_teszt()
        return
    if a.stat:
        st = json_betolt(KULDOTT_FAJL, {}).get("stat")
        if not isinstance(st, dict) or not st.get("futas"):
            sys.exit("Még nincs összegyűjtött statisztika (néhány futás után lesz).")
        if not admin_ertesit("📊 Hírfigyelő – statisztika (próba)", stat_uzenet(st)):
            sys.exit("Nincs teszt-címzett beállítva vagy a küldés nem sikerült.")
        print("Statisztika elküldve.")
        return
    if a.admin_teszt:
        if not admin_ertesit("🛠 Hírfigyelő – próba", "Ha ezt látod, a működésfigyelő üzenetei megérkeznek."):
            sys.exit("Nincs teszt-címzett beállítva vagy a küldés nem sikerült.")
        print("Admin-próba elküldve.")
        return
    if a.heti:
        if a.teszt_cimzett:
            teszt_cimzettre_valt()
        if not heti_elonezet():
            sys.exit("A következő hét naptára még nem érhető el.")
        print("Heti előnézet elküldve.")
        return
    if a.naptar:
        if a.teszt_cimzett:
            teszt_cimzettre_valt()
        if not naptar_kuldes(datetime.now().strftime("%Y-%m-%d"), teszt=a.teszt_cimzett):
            sys.exit("A Forex Factory naptár most nem érhető el.")
        print("Naptár elküldve.")
        return
    if a.test:
        teszt_cimzettre_valt()  # a teszt sosem megy a felhasználóknak
        print(f"Csatornák: ntfy={'BE' if NTFY_AKTIV else 'KI (nincs/alap ntfy_topic)'}, "
              f"Telegram={'BE' if (TG_TOKEN and TG_CHAT) else 'KI'}"
              f"{' (közvetítőn át)' if 'api.telegram.org' not in TG_API else ''} "
              f"(token {'van' if TG_TOKEN else 'HIÁNYZIK'}, chat_id {'van' if TG_CHAT else 'HIÁNYZIK'})")
        ertesit("Teszt a hírfigyelőtől", "Ha ezt látod, működik az értesítés.")
        print("Tesztértesítés elküldve.")
        return
    if not (NTFY_AKTIV or (TG_TOKEN and TG_CHAT)) and not a.szaraz:
        sys.exit("Nincs értesítési csatorna: írd át az ntfy_topic értékét egyedi névre, és/vagy add meg a "
                 "TELEGRAM_BOT_TOKEN és TELEGRAM_CHAT_ID értékeket.")
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
