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
        "Tőzsde": "📈", "Jegybank/kamat": "🏦", "Világgazdaság": "🌍", "Nagy cégek": "🏢",
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
    with ThreadPoolExecutor(max_workers=5) as ex:
        eredmeny = list(ex.map(_egy_arfolyam, lista.items()))
    sorok = []
    for nev, jel, ar, valt in eredmeny:
        if ar is None:
            continue
        iv = "" if valt is None else f" {'▲' if valt >= 0 else '▼'}{_szam(abs(valt), 1)}%"
        sorok.append(f"{nev} {_szam(ar, 0 if ar >= 10000 else 2)} USD{iv}")
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


def szekcios_uzenet(tetelek, fejlec=True, max_sima=3300, max_html=3600):
    """Átlátható, témák szerint tagolt üzenet. Visszaad: (sima szöveg az ntfy-hez, HTML a Telegramhoz).
    A Telegram-változatban minden cím kattintható link. Ha nem fér el, a végén jelzi, hány hír maradt ki."""
    sima, htm = [], []
    hs = hh = 0
    kiirt = 0
    ikonok = CFG.get("ikonok", {})
    for tema, elemek in csoportosit(tetelek):
        fej_s = fej_h = None
        if fejlec:
            ikon = ikonok.get(tema, "📰")
            fej_s = f"{ikon} {tema.upper()} ({len(elemek)})"
            fej_h = f"<b>{ikon} {html.escape(tema)}</b> ({len(elemek)})"
        elso = True
        for o in elemek:
            jel = "🔴 " if o.get("surgos") else ""
            ido = ido_szoveg(o["ido"])
            s = f"• {jel}{ido} · {o['cim']} ({o['forras']})"
            cim_h = html.escape(o["cim"])
            if o.get("link"):
                cim_h = f'<a href="{html.escape(o["link"], quote=True)}">{cim_h}</a>'
            h = f"• {jel}<code>{ido}</code> {cim_h} <i>{html.escape(o['forras'])}</i>"
            extra_s = (len(fej_s) + 2 if fej_s and elso else 0) + (2 if sima else 0)
            extra_h = (len(fej_h) + 2 if fej_h and elso else 0) + (2 if htm else 0)
            if hs + extra_s + len(s) + 1 > max_sima or hh + extra_h + len(h) + 1 > max_html:
                break
            if elso and fej_s:
                sima.append(("\n" if sima else "") + fej_s)
                htm.append(("\n" if htm else "") + fej_h)
                hs += len(sima[-1]) + 1
                hh += len(htm[-1]) + 1
            elso = False
            sima.append(s)
            htm.append(h)
            hs += len(s) + 1
            hh += len(h) + 1
            kiirt += 1
    if kiirt < len(tetelek):
        maradt = f"\n… és még {len(tetelek) - kiirt} hír (lásd a hírlistát)"
        sima.append(maradt)
        htm.append(html.escape(maradt))
    return "\n".join(sima), "\n".join(htm)


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
    adat = json.dumps({"chat_id": TG_CHAT, "text": szov, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode("utf-8")
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
        KULDOTT_FAJL.write_text(json.dumps({"kuldott": mult[-500:], "fuggo": fuggo, "utolso_osszefoglalo": utolso,
                                            "napi_fuggo": napi_fuggo, "utolso_napi": utolso_nap,
                                            "utolso_reggeli": utolso_reggeli, "utolso_surgos": utolso_surgos,
                                            "tg_szunet_ig": TG_SZUNET_IG},
                                           ensure_ascii=False), encoding="utf-8")
    ALLAPOT_FAJL.write_text(json.dumps(lattott[-10000:]))
    hirlista_ment(lista_uj, eredeti)

    def kuld_egyet(x):
        surgos, _, nev, temak, cim, link = x
        uzenet = cim + (f"\n\n{eredeti_formaz(eredeti[cim])}" if cim in eredeti else "")
        ikon = CFG.get("ikonok", {}).get(temak[0], "📰")
        ertesit(f"{'🔴 ' if surgos else ''}{ikon} {' · '.join(temak[:2])} | {nev}", uzenet, link, surgos)

    def rek(x):  # (sürgős, idő, forrás, témák, cím, link) -> rekord-szótár
        return {"ido": x[1], "forras": x[2], "temak": x[3], "cim": x[4], "link": x[5], "surgos": x[0]}

    def szekcios(cim, tetelek, fejlec=True, link=None, surgos=False):
        sima, htm = szekcios_uzenet(tetelek, fejlec)
        ertesit(cim, sima, link or next((o["link"] for o in tetelek if o.get("link")), None), surgos, htm)

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
                    sima, htm = szekcios_uzenet(osszefoglalo) if osszefoglalo else ("", "")
                    if not osszefoglalo:
                        sima = "Éjszaka nem volt fontos hír."
                        htm = html.escape(sima)
                    if ar:
                        sima += "\n\n📊 ÁRFOLYAMOK (legutóbbi záró)\n" + ar
                        htm += "\n\n<b>📊 Árfolyamok</b> (legutóbbi záró)\n" + html.escape(ar)
                    ertesit(f"☀️ Reggeli összefoglaló ({len(osszefoglalo)} hír)" if osszefoglalo
                            else "☀️ Reggeli összefoglaló", sima,
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
    if a.test:
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
