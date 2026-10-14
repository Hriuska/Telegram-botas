"""
Telegram kanalo įrašų automatinis kėlimas.

Skaito įrašus iš Google Sheets lentelės (arba vietinio CSV failo) ir
paskelbia tuos, kurių data ir laikas jau atėjo. Jau paskelbti įrašai
pažymimi faile paskelbta.txt, kad nebūtų keliami du kartus.

Lentelės stulpeliai:
  data       – 2026-10-14 arba 14.10.2026
  laikas     – 09:00
  tekstas    – įrašo tekstas (gali būti keli paragrafai)
  nuotrauka  – nebūtina; viena ar kelios nuorodos (iki 10) į nuotraukas
               ir/arba vaizdo įrašus, atskirtos tarpu, kableliu arba nauja
               eilute. Tinka tiesioginės nuorodos ir Google Drive nuorodos
               (failas turi būti bendrinamas „Visi, turintys nuorodą“).
               Kelios nuorodos paskelbiamos kaip vienas albumas.
  mygtukas   – nebūtina. Tuščia: po įrašu dedamas numatytasis mygtukas
               (žr. NUMATYTAS_MYGTUKAS žemiau). „-“: mygtuko nebus.
               „Tekstas“: mygtukas su šiuo tekstu ir numatytąja nuoroda.
               „Tekstas | https://...“: mygtukas su savo tekstu ir nuoroda.
  prisegti   – nebūtina. „taip“: paskelbtas įrašas prisegamas kanalo viršuje
               (botui reikia administratoriaus teisės redaguoti / prisegti žinutes).

Nustatymai (aplinkos kintamieji):
  TELEGRAM_BOT_TOKEN – boto raktas iš @BotFather
  TELEGRAM_CHANNEL   – kanalas, pvz. @mano_kanalas
  SHEET_CSV_URL      – Google Sheets CSV nuoroda (arba kelias į .csv failą)
  GOOGLE_API_KEY     – Google Drive API raktas (tik automatiniam režimui)
  DRY_RUN=1          – nieko nesiųsti, tik parodyti, kas būtų paskelbta

Automatinis režimas (nebūtina) aprašytas skyriuje „automatinis režimas“ žemiau.
"""

import csv
import hashlib
import io
import json
import mimetypes
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("LAIKO_ZONA", "Europe/Vilnius"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")
SOURCE = os.environ.get("SHEET_CSV_URL", "irasai.csv")
STATE_FILE = os.environ.get("STATE_FILE", "paskelbta.txt")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
# Seni įrašai (pvz. pirmą kartą paleidus) nekeliami, kad kanalas nebūtų užtvindytas
MAX_VELAVIMAS = timedelta(hours=int(os.environ.get("MAX_VELAVIMAS_VAL", "24")))

APRASO_RIBA = 1024           # Telegram nuotraukos / vaizdo aprašo ilgio riba
ALBUMO_RIBA = 10             # daugiausia failų viename albume
NUOTR_DYDIS = 10 * 1024**2   # botas gali įkelti nuotrauką iki 10 MB
FAILO_DYDIS = 50 * 1024**2   # ir vaizdo įrašą iki 50 MB
VIDEO_PLETINIAI = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi")
UA = {"User-Agent": "Mozilla/5.0 (telegram-irasu-botas)"}

# Nuorodų peržiūros (didelės svetainės kortelės) žinutėse nerodomos
BE_PERZIUROS = {"link_preview_options": json.dumps({"is_disabled": True})}

# Mygtukas po kiekvienu įrašu (galima keisti čia)
NUMATYTAS_MYGTUKAS = "🛒 Įsigyti tiekėjus"
NUMATYTA_NUORODA = "https://bizniava.lt"


def dabar() -> datetime:
    if os.environ.get("DABAR"):  # testavimui
        return datetime.fromisoformat(os.environ["DABAR"]).replace(tzinfo=TZ)
    return datetime.now(TZ)


# ---------- lentelė ----------

def nuskaityti_lentele() -> list[dict]:
    if SOURCE.startswith("http"):
        req = urllib.request.Request(SOURCE, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            turinys = r.read().decode("utf-8-sig")
    else:
        with open(SOURCE, encoding="utf-8-sig") as f:
            turinys = f.read()
    eilutes = []
    for e in csv.DictReader(io.StringIO(turinys)):
        eilutes.append({(k or "").strip().lower(): (v or "").strip() for k, v in e.items()})
    return eilutes


def isskaidyti_laika(data: str, laikas: str) -> datetime | None:
    data = data.replace("/", ".").replace("-", ".")
    hm = laikas_is_teksto(laikas or "00:00")
    if hm is None:
        return None
    laikas = f"{hm[0]:02d}:{hm[1]:02d}"
    for fmt in ("%Y.%m.%d", "%d.%m.%Y"):
        try:
            d = datetime.strptime(f"{data} {laikas}", f"{fmt} %H:%M")
            return d.replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def iraso_id(e: dict) -> str:
    raktas = f"{e.get('data')}|{e.get('laikas')}|{e.get('tekstas')}|{e.get('nuotrauka')}"
    return hashlib.sha1(raktas.encode()).hexdigest()[:16]


# ---------- nuorodos ir failų atsisiuntimas ----------

def nuorodos(laukas: str) -> list[str]:
    return [n for n in re.split(r"[\s,;]+", laukas or "") if n.startswith("http")]


def drive_tiesiogine(url: str) -> str:
    """Google Drive bendrinimo nuorodą paverčia tiesioginio atsisiuntimo nuoroda."""
    if "drive.google.com" not in url:
        return url
    m = re.search(r"/file/d/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    if not m:
        return url
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}"


def atsisiusti(url: str) -> tuple[bytes, str, str]:
    """Grąžina (turinys, failo vardas, tipas: 'photo' arba 'video')."""
    req = urllib.request.Request(drive_tiesiogine(url), headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        disp = r.headers.get("Content-Disposition") or ""
        duomenys = r.read(FAILO_DYDIS + 1)
    if len(duomenys) > FAILO_DYDIS:
        raise RuntimeError(f"failas per didelis (daugiau nei 50 MB): {url}")
    if ctype == "text/html":
        raise RuntimeError(
            f"nuoroda veda į puslapį, o ne į failą (ar failas bendrinamas visiems?): {url}")

    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disp)
    vardas = urllib.parse.unquote(m.group(1)) if m else \
        os.path.basename(urllib.parse.urlparse(url).path) or "failas"

    if ctype.startswith("video/") or vardas.lower().endswith(VIDEO_PLETINIAI):
        tipas = "video"
    elif ctype.startswith("image/") or vardas.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp", ".gif")):
        tipas = "photo"
    else:
        raise RuntimeError(f"neatpažintas failo tipas ({ctype or 'nežinomas'}): {url}")

    if tipas == "photo" and len(duomenys) > NUOTR_DYDIS:
        raise RuntimeError(f"nuotrauka per didelė (daugiau nei 10 MB): {url}")
    if "." not in vardas:
        vardas += mimetypes.guess_extension(ctype) or (".mp4" if tipas == "video" else ".jpg")
    return duomenys, vardas, tipas


# ---------- Telegram ----------

def telegram(metodas: str, laukai: dict, failai: dict | None = None):
    """Siunčia užklausą Telegram. failai: {lauko_vardas: (failo_vardas, baitai)}."""
    url = f"{API}/bot{TOKEN}/{metodas}"
    if failai:
        riba = uuid.uuid4().hex
        dalys = []
        for k, v in laukai.items():
            dalys.append(f"--{riba}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode()
                         + str(v).encode() + b"\r\n")
        for k, (vardas, baitai) in failai.items():
            tipas = mimetypes.guess_type(vardas)[0] or "application/octet-stream"
            saugus = re.sub(r'[\r\n"]', "_", vardas)
            dalys.append(
                f"--{riba}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{saugus}\"\r\n"
                f"Content-Type: {tipas}\r\n\r\n".encode() + baitai + b"\r\n")
        body = b"".join(dalys) + f"--{riba}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={riba}"}
    else:
        body = urllib.parse.urlencode(laukai).encode()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            ats = json.loads(r.read())
    except urllib.error.HTTPError as ex:
        ats = json.loads(ex.read() or b"{}")
    if not ats.get("ok"):
        raise RuntimeError(f"Telegram klaida: {ats.get('description', ats)}")
    return ats.get("result")


def zinutes_id(rez) -> int | None:
    """Iš Telegram atsakymo paima žinutės ID (albumo atveju – pirmos)."""
    if isinstance(rez, list):
        rez = rez[0] if rez else None
    return rez.get("message_id") if isinstance(rez, dict) else None


def mygtukas(laukas: str) -> str | None:
    """Grąžina Telegram mygtuko JSON (reply_markup) arba None, jei mygtuko nereikia."""
    laukas = (laukas or "").strip()
    if laukas == "-":
        return None
    if not laukas:
        tekstas, url = NUMATYTAS_MYGTUKAS, NUMATYTA_NUORODA
    elif "|" in laukas:
        tekstas, url = (x.strip() for x in laukas.split("|", 1))
    else:
        tekstas, url = laukas, NUMATYTA_NUORODA
    if not tekstas or not url.startswith(("http://", "https://")):
        raise RuntimeError(f"netinkamas mygtukas „{laukas}“ (formatas: Tekstas | https://...)")
    return json.dumps({"inline_keyboard": [[{"text": tekstas, "url": url}]]}, ensure_ascii=False)


def paskelbti(e: dict) -> int | None:
    """Paskelbia įrašą ir grąžina žinutės su tekstu / mygtuku ID."""
    tekstas = e.get("tekstas", "")
    sarasas = nuorodos(e.get("nuotrauka", ""))
    if len(sarasas) > ALBUMO_RIBA:
        raise RuntimeError(f"per daug failų ({len(sarasas)}); viename įraše galima iki {ALBUMO_RIBA}")
    kb = mygtukas(e.get("mygtukas", ""))

    def su_mygtuku(laukai: dict) -> dict:
        if kb:
            laukai["reply_markup"] = kb
        return laukai

    if not sarasas:
        if tekstas:
            return zinutes_id(telegram("sendMessage", su_mygtuku({"chat_id": CHANNEL, "text": tekstas, **BE_PERZIUROS})))
        return None

    failai = [atsisiusti(u) for u in sarasas]  # jei kuris nepavyks – nieko neskelbiama

    if len(failai) == 1:
        aprasas = tekstas if len(tekstas) <= APRASO_RIBA else ""
        baitai, vardas, tipas = failai[0]
        laukai = {"chat_id": CHANNEL}
        if aprasas:
            laukai["caption"] = aprasas
        if tipas == "video":
            laukai["supports_streaming"] = "true"
        if aprasas or not tekstas:  # mygtukas po nuotrauka, jei tekstas tilpo
            su_mygtuku(laukai)
        mid = zinutes_id(telegram("sendVideo" if tipas == "video" else "sendPhoto",
                                  laukai, {tipas: (vardas, baitai)}))
        if tekstas and not aprasas:  # per ilgas tekstas – atskira žinute su mygtuku
            mid = zinutes_id(telegram("sendMessage", su_mygtuku({"chat_id": CHANNEL, "text": tekstas, **BE_PERZIUROS})))
        return mid

    # Albumas. Telegram neleidžia mygtukų po albumu, todėl tekstas dedamas į patį
    # albumą (vienas įrašas, be mygtuko). Tik per ilgas tekstas siunčiamas atskirai.
    aprasas = tekstas if len(tekstas) <= APRASO_RIBA else ""
    media, priedai = [], {}
    for i, (baitai, vardas, tipas) in enumerate(failai):
        item = {"type": tipas, "media": f"attach://f{i}"}
        if i == 0 and aprasas:
            item["caption"] = aprasas
        if tipas == "video":
            item["supports_streaming"] = True
        media.append(item)
        priedai[f"f{i}"] = (vardas, baitai)
    mid = zinutes_id(telegram("sendMediaGroup",
                              {"chat_id": CHANNEL, "media": json.dumps(media, ensure_ascii=False)}, priedai))
    if tekstas and not aprasas:
        mid = zinutes_id(telegram("sendMessage", su_mygtuku({"chat_id": CHANNEL, "text": tekstas, **BE_PERZIUROS})))
    return mid


# ---------- automatinis režimas (Drive aplankai) ----------
#
# Lentelėje papildomi lapai (skirtukai apačioje):
#   auto      – laikas | kategorija | dienos (nebūtina, pvz. „Pr, Tr, Pn“; tuščia = kasdien)
#   aplankai  – kategorija | aplankas (Google Drive aplanko nuoroda; ta pati kategorija
#               gali būti keliose eilutėse, pvz. atskiri nuotraukų ir vaizdo įrašų aplankai)
#   tekstai   – kategorija | tekstas (keli variantai kiekvienai kategorijai)
# Kategorija „Visi“ ima failus iš bet kurio aplanko. Kategorija, kurios nėra lape
# „aplankai“ (pvz. DUK, Gyvos), skelbiama tik tekstu. Stulpelis „mygtukas“ – kaip pagr. lape.
# Reikia GOOGLE_API_KEY (Google Drive API raktas) ir kad aplankai būtų bendrinami
# „Visi, turintys nuorodą“.

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
DRIVE_API = os.environ.get("DRIVE_API", "https://www.googleapis.com/drive/v3/files")
DRIVE_DL = os.environ.get("DRIVE_DL", "https://drive.google.com/uc?export=download&id=")
AUTO_VELAVIMAS = timedelta(hours=int(os.environ.get("AUTO_VELAVIMAS_VAL", "3")))
VIDEO_TIKIMYBE = 0.4          # kaip dažnai rinktis vaizdo įrašą, kai yra ir nuotraukų
ALBUMAS_NUO, ALBUMAS_IKI = 2, 5
DIENOS = {"pr": 0, "an": 1, "tr": 2, "kt": 3, "pn": 4, "št": 5, "st": 5, "sk": 6}
VISI = {"visi", "visos", "bet kuri", "bet koks", "mix"}


def laikas_is_teksto(t: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{1,2})[:.](\d{2})", t or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def lapas(pavadinimas: str, privalomi: set[str], draudziami: set[str] = frozenset()) -> list[dict] | None:
    """Nuskaito kitą lentelės lapą pagal pavadinimą. None – jei tokio lapo nėra."""
    if SOURCE.startswith("http"):
        m = re.search(r"/spreadsheets/d/([\w-]+)", SOURCE)
        if not m:
            return None
        url = (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/gviz/tq?tqx=out:csv&headers=1"
               f"&sheet={urllib.parse.quote(pavadinimas)}")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                turinys = r.read().decode("utf-8-sig")
        except Exception:
            return None
    else:  # vietinis bandymas: irasai.csv -> irasai_auto.csv
        kelias = re.sub(r"\.csv$", "", SOURCE) + f"_{pavadinimas}.csv"
        if not os.path.exists(kelias):
            return None
        with open(kelias, encoding="utf-8-sig") as f:
            turinys = f.read()
    eil = [{(k or "").strip().lower(): (v or "").strip() for k, v in e.items()}
           for e in csv.DictReader(io.StringIO(turinys))]
    stulpeliai = set(eil[0].keys()) if eil else set()
    # Google, neradęs lapo, kartais grąžina pirmą lapą – patikriname stulpelius
    if not privalomi <= stulpeliai or stulpeliai & draudziami:
        return None
    return eil


def kat_raktas(k: str) -> str:
    return re.sub(r"\s+", " ", (k or "").strip().lower())


def drive_aplanko_failai(nuoroda: str) -> list[dict]:
    m = re.search(r"/folders/([\w-]+)", nuoroda) or re.search(r"[?&]id=([\w-]+)", nuoroda)
    if not m:
        raise RuntimeError(f"neatpažinta Drive aplanko nuoroda: {nuoroda}")
    failai, token = [], ""
    while True:
        q = {"q": f"'{m.group(1)}' in parents and trashed = false", "key": GOOGLE_API_KEY,
             "fields": "nextPageToken, files(id, name, mimeType, size)", "pageSize": "1000"}
        if token:
            q["pageToken"] = token
        req = urllib.request.Request(f"{DRIVE_API}?{urllib.parse.urlencode(q)}", headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ats = json.loads(r.read())
        except urllib.error.HTTPError as ex:
            raise RuntimeError(f"Google Drive klaida ({ex.code}). Ar teisingas GOOGLE_API_KEY "
                               f"ir ar aplankas bendrinamas visiems? {nuoroda}")
        for f in ats.get("files", []):
            mt, dydis = f.get("mimeType", ""), int(f.get("size") or 0)
            if mt.startswith("image/") and dydis <= NUOTR_DYDIS:
                failai.append({"id": f["id"], "tipas": "photo"})
            elif mt.startswith("video/") and dydis <= FAILO_DYDIS:
                failai.append({"id": f["id"], "tipas": "video"})
        token = ats.get("nextPageToken")
        if not token:
            return failai


def maziausiai_naudoti(elementai: list, raktas, naudota: dict, kiek: int, atsitiktinis) -> list:
    """Parenka tuos, kurie dar nenaudoti arba naudoti seniausiai."""
    tvarka = sorted(elementai, key=lambda x: (naudota.get(raktas(x), -1), atsitiktinis.random()))
    return tvarka[:kiek]


def auto_skelbti(now: datetime, busena: list[str]) -> int:
    """Skelbia automatinius įrašus. Grąžina klaidų skaičių."""
    grafikas = lapas("auto", {"laikas", "kategorija"}, {"tekstas", "data"})
    if grafikas is None:
        return 0
    aplankai = lapas("aplankai", {"kategorija", "aplankas"}, {"tekstas", "data"}) or []
    tekstai = lapas("tekstai", {"kategorija", "tekstas"}, {"data", "laikas"}) or []
    if not GOOGLE_API_KEY and not DRY_RUN:
        print("Automatinis režimas: trūksta GOOGLE_API_KEY, praleidžiama.")
        return 1

    padaryta = set(busena)
    naudota = {}  # elementas -> paskutinio naudojimo eilės nr.
    for i, l in enumerate(busena):
        naudota[l] = i
    aplanku_map: dict[str, list[str]] = {}  # viena kategorija gali turėti kelis aplankus
    for a in aplankai:
        if a.get("aplankas", "").startswith("http"):
            aplanku_map.setdefault(kat_raktas(a["kategorija"]), []).append(a["aplankas"])
    failu_talpykla: dict[str, list] = {}
    klaidos = 0

    for eil in grafikas:
        kat = eil.get("kategorija", "")
        hm = laikas_is_teksto(eil.get("laikas", ""))
        if not kat or not hm:
            continue
        dienos = [DIENOS[d] for d in re.findall(r"[a-zš]+", eil.get("dienos", "").lower()) if d in DIENOS]
        if dienos and now.weekday() not in dienos:
            continue
        kada = now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
        raktas = f"auto:{kada:%Y-%m-%d %H:%M} {kat_raktas(kat)}"
        if kada > now or raktas in padaryta or now - kada > AUTO_VELAVIMAS:
            continue

        rnd = random.Random()
        try:
            # tekstas
            kr = kat_raktas(kat)
            variantai = [t["tekstas"] for t in tekstai if kat_raktas(t["kategorija"]) == kr and t["tekstas"]]
            if not variantai:
                raise RuntimeError(f"lape „tekstai“ nėra tekstų kategorijai „{kat}“")
            tekstas = maziausiai_naudoti(variantai, lambda t: "t:" + hashlib.sha1(t.encode()).hexdigest()[:12],
                                         naudota, 1, rnd)[0]

            # failai iš tos pačios kategorijos aplanko
            # Iš kurių kategorijų imami failai:
            #  „MIX“ / „Visi“ – iš visų; „A + B“ – iš kelių nurodytų; kitaip – iš vienos.
            if kr in VISI:
                grupes = list(aplanku_map)
            elif "+" in kr:
                grupes = [kat_raktas(d) for d in kr.split("+")]
                truksta = [g for g in grupes if g not in aplanku_map]
                if truksta:
                    raise RuntimeError(f"lape „aplankai“ nėra kategorijos „{truksta[0]}“")
            elif kr in aplanku_map:
                grupes = [kr]
            else:  # kategorija be aplanko (pvz. DUK, Gyvos) – skelbiamas tik tekstas
                grupes = []
            be_failu = not grupes

            def grupes_failai(g):
                out = []
                for s in aplanku_map[g]:
                    if s not in failu_talpykla:
                        failu_talpykla[s] = [] if DRY_RUN and not GOOGLE_API_KEY else drive_aplanko_failai(s)
                    out += failu_talpykla[s]
                return out

            fr = lambda f: "f:" + f["id"]
            gf = {g: grupes_failai(g) for g in grupes}
            nuotr_gr = {g: [f for f in v if f["tipas"] == "photo"] for g, v in gf.items()}
            nuotr_gr = {g: v for g, v in nuotr_gr.items() if v}
            video = [f for v in gf.values() for f in v if f["tipas"] == "video"]
            nuotr_kiek = sum(len(v) for v in nuotr_gr.values())

            maisyti = len(nuotr_gr) > 1  # MIX ar „A + B“ – visada nuotraukų rinkinys
            if video and not maisyti and (nuotr_kiek < 2 or rnd.random() < VIDEO_TIKIMYBE):
                # vaizdo įrašas – iš atsitiktinės grupės, kad visos pasirodytų
                vg = [g for g in gf if any(f["tipas"] == "video" for f in gf[g])]
                g = rnd.choice(vg)
                pasirinkta = maziausiai_naudoti([f for f in gf[g] if f["tipas"] == "video"], fr, naudota, 1, rnd)
            elif nuotr_kiek >= 2:
                k = rnd.randint(ALBUMAS_NUO, min(ALBUMAS_IKI, nuotr_kiek))
                if maisyti:
                    k = max(k, min(len(nuotr_gr), ALBUMAS_IKI), 4)
                    k = min(k, nuotr_kiek)
                    # po lygiai iš kiekvienos kategorijos (pakaitomis)
                    eiles = {g: maziausiai_naudoti(v, fr, naudota, len(v), rnd) for g, v in nuotr_gr.items()}
                    tvarka = list(eiles)
                    rnd.shuffle(tvarka)
                    pasirinkta, i = [], 0
                    while len(pasirinkta) < k and any(eiles.values()):
                        g = tvarka[i % len(tvarka)]
                        if eiles[g]:
                            pasirinkta.append(eiles[g].pop(0))
                        i += 1
                else:
                    v = next(iter(nuotr_gr.values()))
                    pasirinkta = maziausiai_naudoti(v, fr, naudota, k, rnd)
            elif nuotr_kiek == 1:
                pasirinkta = next(iter(nuotr_gr.values()))
            else:
                pasirinkta = []
                if not DRY_RUN and not be_failu:
                    raise RuntimeError(f"kategorijos „{kat}“ aplankuose nėra nuotraukų ar vaizdo įrašų")

            aprasymas = (f"{len(pasirinkta)} nuotr." if pasirinkta and pasirinkta[0]["tipas"] == "photo"
                         else "vaizdo įrašas" if pasirinkta else "tik tekstas")
            pradzia = tekstas[:45].replace("\n", " ")
            if DRY_RUN:
                print(f"[BANDYMAS] Auto {kada:%H:%M} {kat} ({aprasymas}): {pradzia}…")
                continue

            paskelbti({"tekstas": tekstas,
                       "nuotrauka": " ".join(DRIVE_DL + f["id"] for f in pasirinkta),
                       "mygtukas": eil.get("mygtukas", "")})
            nauji = [raktas, "t:" + hashlib.sha1(tekstas.encode()).hexdigest()[:12]] + [fr(f) for f in pasirinkta]
            with open(STATE_FILE, "a") as f:
                f.write("\n".join(nauji) + "\n")
            for n in nauji:
                padaryta.add(n)
                naudota[n] = len(naudota) + len(busena)
            print(f"Auto paskelbta {kada:%H:%M} {kat} ({aprasymas}): {pradzia}…")
        except Exception as ex:
            klaidos += 1
            print(f"Auto nepavyko ({kada:%H:%M} {kat}): {ex}")
    return klaidos


# ---------- pagrindinė dalis ----------

def main() -> int:
    if not DRY_RUN and (not TOKEN or not CHANNEL):
        print("Trūksta TELEGRAM_BOT_TOKEN arba TELEGRAM_CHANNEL.")
        return 1

    busena = []
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            busena = [l.strip() for l in f if l.strip()]
    paskelbti_id = set(busena)

    now = dabar()
    klaidos = 0
    eiles = []
    for e in nuskaityti_lentele():
        if not e.get("tekstas") and not e.get("nuotrauka"):
            continue
        kada = isskaidyti_laika(e.get("data", ""), e.get("laikas", ""))
        if kada is None:
            print(f"Praleista: neatpažinta data/laikas „{e.get('data')} {e.get('laikas')}“")
            continue
        eiles.append((kada, e))

    for kada, e in sorted(eiles, key=lambda x: x[0]):
        iid = iraso_id(e)
        if iid in paskelbti_id or kada > now:
            continue
        if now - kada > MAX_VELAVIMAS:
            print(f"Praleista (per senas): {kada:%Y-%m-%d %H:%M}")
            continue
        pradzia = e.get("tekstas", "")[:50].replace("\n", " ")
        n = len(nuorodos(e.get("nuotrauka", "")))
        priedas = f" [+{n} failai]" if n else ""
        if DRY_RUN:
            print(f"[BANDYMAS] Būtų paskelbta {kada:%Y-%m-%d %H:%M}{priedas}: {pradzia}…")
            continue
        try:
            mid = paskelbti(e)
            with open(STATE_FILE, "a") as f:
                f.write(iid + "\n")
            print(f"Paskelbta {kada:%Y-%m-%d %H:%M}{priedas}: {pradzia}…")
            if e.get("prisegti", "").strip().lower() in ("taip", "t", "yes", "x", "1") and mid:
                try:
                    telegram("pinChatMessage", {"chat_id": CHANNEL, "message_id": mid,
                                                "disable_notification": "true"})
                    print("  ↳ prisegta kanalo viršuje")
                except Exception as ex:
                    klaidos += 1
                    print(f"  ↳ nepavyko prisegti ({ex}). Ar botas turi teisę redaguoti žinutes?")
        except Exception as ex:
            klaidos += 1
            print(f"Nepavyko ({kada:%Y-%m-%d %H:%M}): {ex}")

    if os.path.exists(STATE_FILE):  # perskaitome iš naujo – galėjo pasipildyti
        with open(STATE_FILE) as f:
            busena = [l.strip() for l in f if l.strip()]
    klaidos += auto_skelbti(now, busena)

    return 1 if klaidos else 0


if __name__ == "__main__":
    sys.exit(main())
