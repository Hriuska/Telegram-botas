"""
Telegram kanalo įrašų automatinis kėlimas.

Skaito įrašus iš Google Sheets lentelės (arba vietinio CSV failo) ir
paskelbia tuos, kurių data ir laikas jau atėjo. Jau paskelbti įrašai
pažymimi faile paskelbta.txt, kad nebūtų keliami du kartus.

Lentelės stulpeliai:
  data       – 2026-10-14 arba 14.10.2026
  laikas     – 09:00
  tekstas    – įrašo tekstas (gali būti keli paragrafai)
  nuotrauka  – nebūtina; tiesioginė nuoroda į paveikslėlį

Nustatymai (aplinkos kintamieji):
  TELEGRAM_BOT_TOKEN – boto raktas iš @BotFather
  TELEGRAM_CHANNEL   – kanalas, pvz. @mano_kanalas
  SHEET_CSV_URL      – Google Sheets „Publish to web“ CSV nuoroda
                       (arba kelias į vietinį .csv failą)
  DRY_RUN=1          – nieko nesiųsti, tik parodyti, kas būtų paskelbta
"""

import csv
import hashlib
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("LAIKO_ZONA", "Europe/Vilnius"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")
SOURCE = os.environ.get("SHEET_CSV_URL", "irasai.csv")
STATE_FILE = os.environ.get("STATE_FILE", "paskelbta.txt")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
# Seni įrašai (pvz. pirmą kartą paleidus) nekeliami, kad kanalas nebūtų užtvindytas
MAX_VELAVIMAS = timedelta(hours=int(os.environ.get("MAX_VELAVIMAS_VAL", "24")))


def dabar() -> datetime:
    if os.environ.get("DABAR"):  # testavimui
        return datetime.fromisoformat(os.environ["DABAR"]).replace(tzinfo=TZ)
    return datetime.now(TZ)


def nuskaityti_lentele() -> list[dict]:
    if SOURCE.startswith("http"):
        with urllib.request.urlopen(SOURCE, timeout=30) as r:
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


def telegram(metodas: str, duomenys: dict) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/{metodas}"
    body = urllib.parse.urlencode(duomenys).encode()
    with urllib.request.urlopen(url, data=body, timeout=30) as r:
        ats = json.loads(r.read())
    if not ats.get("ok"):
        raise RuntimeError(f"Telegram klaida: {ats}")


def paskelbti(e: dict) -> None:
    tekstas = e.get("tekstas", "")
    nuotrauka = e.get("nuotrauka", "")
    if nuotrauka:
        if len(tekstas) <= 1024:  # Telegram nuotraukos aprašo riba
            telegram("sendPhoto", {"chat_id": CHANNEL, "photo": nuotrauka, "caption": tekstas})
            return
        telegram("sendPhoto", {"chat_id": CHANNEL, "photo": nuotrauka})
    if tekstas:
        telegram("sendMessage", {"chat_id": CHANNEL, "text": tekstas})


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
        if DRY_RUN:
            print(f"[BANDYMAS] Būtų paskelbta {kada:%Y-%m-%d %H:%M}: {pradzia}…")
            continue
        try:
            paskelbti(e)
            with open(STATE_FILE, "a") as f:
                f.write(iid + "\n")
            print(f"Paskelbta {kada:%Y-%m-%d %H:%M}: {pradzia}…")
        except Exception as ex:
            klaidos += 1
            print(f"Nepavyko ({kada:%Y-%m-%d %H:%M}): {ex}")

    return 1 if klaidos else 0


if __name__ == "__main__":
    sys.exit(main())
