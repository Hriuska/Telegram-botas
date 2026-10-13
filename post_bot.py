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
  DRY_RUN=1          – nieko nesiųsti, tik parodyti, kas būtų paskelbta
"""

import csv
import hashlib
import io
import json
import mimetypes
import os
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
    laikas = (laikas or "00:00")[:5].strip()
    if len(laikas) == 4:  # 9:00 -> 09:00
        laikas = "0" + laikas
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
            return zinutes_id(telegram("sendMessage", su_mygtuku({"chat_id": CHANNEL, "text": tekstas})))
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
            mid = zinutes_id(telegram("sendMessage", su_mygtuku({"chat_id": CHANNEL, "text": tekstas})))
        return mid

    # Albumas. Telegram neleidžia mygtukų po albumu, todėl jei yra mygtukas
    # (arba tekstas per ilgas), tekstas siunčiamas atskira žinute iškart po albumu.
    aprasas = tekstas if (len(tekstas) <= APRASO_RIBA and not kb) else ""
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
        mid = zinutes_id(telegram("sendMessage", su_mygtuku({"chat_id": CHANNEL, "text": tekstas})))
    return mid



# ---------- pagrindinė dalis ----------

def main() -> int:
    if not DRY_RUN and (not TOKEN or not CHANNEL):
        print("Trūksta TELEGRAM_BOT_TOKEN arba TELEGRAM_CHANNEL.")
        return 1

    paskelbti_id = set()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            paskelbti_id = {l.strip() for l in f if l.strip()}

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

    return 1 if klaidos else 0


if __name__ == "__main__":
    sys.exit(main())
