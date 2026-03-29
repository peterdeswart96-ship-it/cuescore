"""
cuescore_notifier.py
====================
Stuurt gepersonaliseerde Ntfy push-notificaties per speler voor Mokum Pool & Darts.

Elke speler heeft een eigen Ntfy-topic op basis van zijn Cuescore player-ID:
    <NTFY_TOPIC_BASIS>-<playerId>
    bijv. mokum-pool-live-2781830

Een speler abonneert zich eenmalig op zijn eigen topic in de Ntfy-app.
Zodra zijn wedstrijd op het spel staat of klaar is, krijgt hij een notificatie.

Twee notificatietypes per speler:
  GROEN  "Jouw volgende wedstrijd"  → tegenstander + tafelnummer + ronde + starttijd
  VLAG   "Wedstrijd klaar"          → eindstand + winnaar + ronde + tafelnummer

Automatische toernooi-detectie: het script scrapt de Mokum-organisatiepagina
en vindt zelf welke toernooien vandaag plaatsvinden.

Omgevingsvariabelen (GitHub Actions Secrets):
  NTFY_TOPIC_BASIS    : bijv. mokum-pool-live
  NTFY_SERVER         : https://ntfy.sh
  ORG_STUB            : mokumpooldarts
  EXTRA_TOERNOOI_IDS  : optioneel, kommagescheiden extra IDs
"""

import os
import re
import json
import time
import requests
from datetime import datetime, date
from pathlib import Path

# ─── CONFIGURATIE ─────────────────────────────────────────────────────────────

NTFY_TOPIC_BASIS = os.environ.get("NTFY_TOPIC_BASIS", "mokum")
NTFY_SERVER      = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
ORG_STUB         = os.environ.get("ORG_STUB", "mokumpooldarts")

_extra_raw   = os.environ.get("EXTRA_TOERNOOI_IDS", "")
EXTRA_IDS    = [int(x.strip()) for x in _extra_raw.split(",") if x.strip()]

STATE_BESTAND = Path(os.environ.get("STATE_PATH", "cuescore_state.json"))

# Rate-limit instellingen voor Ntfy
NTFY_DELAY        = 0.5   # seconden pauze tussen notificaties
NTFY_MAX_RETRIES  = 3     # maximaal aantal pogingen bij 429
NTFY_BACKOFF_BASE = 2.0   # basis voor exponentiële backoff (seconden)

# ─── NTFY ─────────────────────────────────────────────────────────────────────

def speler_topic(player_id: int) -> str:
    """
    Genereert het persoonlijke Ntfy-topic voor een speler.
    Bijv: mokum-pool-live-2781830

    De speler abonneert zich op dit topic in de Ntfy-app door de naam in
    te typen. Zijn player-ID staat in de URL van zijn Cuescore-profiel:
    cuescore.com/player/Naam/2781830 → laatste getal = player-ID
    """
    return f"{NTFY_TOPIC_BASIS}-{player_id}"

def stuur_notificatie(player_id: int, titel: str, bericht: str,
                      prioriteit: str = "default", tag: str = "sports"):
    """
    Stuurt een push-notificatie naar het persoonlijke topic van één speler.
    Alleen die speler ontvangt de melding.
    Bevat retry-logica met exponentiële backoff bij 429 rate-limit fouten.
    """
    topic = speler_topic(player_id)
    url   = f"{NTFY_SERVER}/{topic}"

    headers = {
        "Title":    titel.encode("utf-8"),
        "Priority": prioriteit,
        "Tags":     tag,
    }

    for poging in range(NTFY_MAX_RETRIES):
        try:
            # Korte pauze vóór elke request om rate-limit te voorkomen
            if poging == 0:
                time.sleep(NTFY_DELAY)
            else:
                wachttijd = NTFY_BACKOFF_BASE ** poging
                print(f"    [Retry] Poging {poging + 1}/{NTFY_MAX_RETRIES} "
                      f"voor {topic} na {wachttijd:.1f}s wachten...")
                time.sleep(wachttijd)

            r = requests.post(url, data=bericht.encode("utf-8"),
                              headers=headers, timeout=15)
            r.raise_for_status()
            print(f"    [Ntfy OK] → {topic}: {titel}")
            return  # Gelukt — stop retry-loop

        except requests.exceptions.HTTPError as e:
            if r.status_code == 429 and poging < NTFY_MAX_RETRIES - 1:
                # Rate-limited: probeer opnieuw met langere pauze
                continue
            print(f"    [ERROR] Ntfy mislukt voor {topic}: {e}")
            return

        except Exception as e:
            print(f"    [ERROR] Ntfy mislukt voor {topic}: {e}")
            return

# ─── TOERNOOI-DETECTIE ────────────────────────────────────────────────────────

def haal_toernooien_vandaag(org_stub: str) -> list[int]:
    """
    Scrapt de Cuescore-organisatiepagina en retourneert alle toernooi-IDs
    van vandaag en gisteren (gisteren vanwege avondtoernooien over middernacht).

    De pagina bevat links als: /tournament/Naam/76166350
    Het getal is het toernooi-ID.
    """
    url = f"https://cuescore.com/{org_stub}/tournaments"
    try:
        r = requests.get(url, timeout=20, headers={"Accept-Language": "en"})
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"[ERROR] Organisatiepagina ophalen mislukt: {e}")
        return []

    vandaag  = date.today()
    gisteren = date.fromordinal(vandaag.toordinal() - 1)

    datum_patroon    = re.compile(
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+'
        r'([A-Z][a-z]+ \d{1,2}, \d{4})'
    )
    toernooi_patroon = re.compile(r'/tournament/[^/]+/(\d+)')

    gevonden_ids = []
    secties = datum_patroon.split(html)

    i = 1
    while i < len(secties) - 1:
        datum_str = secties[i]
        blok      = secties[i + 1] if i + 1 < len(secties) else ""

        try:
            datum = datetime.strptime(datum_str.strip(), "%B %d, %Y").date()
        except ValueError:
            i += 2
            continue

        if datum in (vandaag, gisteren):
            for tid in toernooi_patroon.findall(blok):
                tid_int = int(tid)
                if tid_int not in gevonden_ids:
                    gevonden_ids.append(tid_int)
                    print(f"  Toernooi gevonden: {datum_str} → ID {tid_int}")

        i += 2

    return gevonden_ids

# ─── CUESCORE API ─────────────────────────────────────────────────────────────

def haal_toernooi_data(toernooi_id: int) -> dict | None:
    url = f"https://api.cuescore.com/tournament/?id={toernooi_id}"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[ERROR] Toernooi {toernooi_id} ophalen mislukt: {e}")
        return None

# ─── HULPFUNCTIES ─────────────────────────────────────────────────────────────

def speler_naam(speler: dict) -> str:
    return speler.get("name", "") if speler else ""

def speler_id(speler: dict) -> int | None:
    pid = speler.get("playerId") if speler else None
    return int(pid) if pid else None

def is_speelbaar(match: dict) -> bool:
    """True als beide spelersnamen bekend zijn (niet leeg/TBD)."""
    return bool(
        speler_naam(match.get("playerA", {})) and
        speler_naam(match.get("playerB", {}))
    )

def tafel_info(match: dict) -> str:
    tafel = match.get("table", {})
    return f"Tafel {tafel['name']}" if tafel and tafel.get("name") else "Tafel onbekend"

def format_tijd(iso_str: str) -> str:
    try:
        return datetime.fromisoformat(iso_str).strftime("%H:%M")
    except Exception:
        return "?"

# ─── NOTIFICATIES ─────────────────────────────────────────────────────────────

def notificeer_klaar_om_te_starten(match: dict, toernooi_naam: str,
                                    speler_a_id: int, speler_b_id: int):
    """
    Stuurt naar beide spelers een persoonlijke "jouw wedstrijd kan starten" melding.
    Elke speler ziet zijn eigen naam als "jij" en de ander als tegenstander.
    """
    naam_a   = speler_naam(match.get("playerA", {}))
    naam_b   = speler_naam(match.get("playerB", {}))
    ronde    = match.get("roundName", "?")
    tafel    = tafel_info(match)
    race_to  = match.get("raceTo", "?")
    starttijd = format_tijd(match["starttime"]) if match.get("starttime") else "?"

    # Notificatie voor speler A
    stuur_notificatie(
        player_id = speler_a_id,
        titel     = f"Jouw wedstrijd — {tafel}",
        bericht   = (
            f"vs {naam_b}\n"
            f"{ronde} | Race to {race_to}\n"
            f"{tafel} | Start: {starttijd}\n"
            f"{toernooi_naam}"
        ),
        prioriteit= "high",
        tag       = "green_circle"
    )

    # Notificatie voor speler B (gespiegeld)
    stuur_notificatie(
        player_id = speler_b_id,
        titel     = f"Jouw wedstrijd — {tafel}",
        bericht   = (
            f"vs {naam_a}\n"
            f"{ronde} | Race to {race_to}\n"
            f"{tafel} | Start: {starttijd}\n"
            f"{toernooi_naam}"
        ),
        prioriteit= "high",
        tag       = "green_circle"
    )

def notificeer_wedstrijd_klaar(match: dict, toernooi_naam: str,
                                speler_a_id: int, speler_b_id: int):
    """
    Stuurt naar beide spelers de eindstand.
    De winnende speler krijgt "Gewonnen!", de verliezende "Verloren".
    """
    naam_a  = speler_naam(match.get("playerA", {}))
    naam_b  = speler_naam(match.get("playerB", {}))
    score_a = match.get("scoreA", 0)
    score_b = match.get("scoreB", 0)
    ronde   = match.get("roundName", "?")
    tafel   = tafel_info(match)

    a_wint  = score_a > score_b
    stand   = f"{score_a}-{score_b}"

    # Notificatie voor speler A
    stuur_notificatie(
        player_id = speler_a_id,
        titel     = f"{'Gewonnen!' if a_wint else 'Verloren'} {stand} vs {naam_b}",
        bericht   = (
            f"Eindstand: {naam_a} {stand} {naam_b}\n"
            f"{ronde} | {tafel}\n"
            f"{toernooi_naam}"
        ),
        prioriteit= "high" if a_wint else "default",
        tag       = "trophy" if a_wint else "x"
    )

    # Notificatie voor speler B (gespiegeld)
    b_wint = score_b > score_a
    stuur_notificatie(
        player_id = speler_b_id,
        titel     = f"{'Gewonnen!' if b_wint else 'Verloren'} {score_b}-{score_a} vs {naam_a}",
        bericht   = (
            f"Eindstand: {naam_b} {score_b}-{score_a} {naam_a}\n"
            f"{ronde} | {tafel}\n"
            f"{toernooi_naam}"
        ),
        prioriteit= "high" if b_wint else "default",
        tag       = "trophy" if b_wint else "x"
    )

# ─── STATE ────────────────────────────────────────────────────────────────────

def laad_state() -> dict:
    if STATE_BESTAND.exists():
        try:
            return json.loads(STATE_BESTAND.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] State laden mislukt: {e}")
    return {}

def sla_state(state: dict):
    STATE_BESTAND.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"[State] Opgeslagen ({len(state)} entries)")

# ─── TOERNOOI VERWERKEN ───────────────────────────────────────────────────────

def verwerk_toernooi(toernooi_id: int, state: dict):
    """Verwerkt alle wedstrijden van één toernooi en stuurt notificaties waar nodig."""

    data = haal_toernooi_data(toernooi_id)
    if not data:
        return

    naam        = data.get("name", f"Toernooi {toernooi_id}")
    matches_all = data.get("matches", [])
    print(f"  '{naam}' | {data.get('status')} | {len(matches_all)} matches")

    for match in matches_all:

        match_id  = match.get("matchId")
        m_key     = f"match_{match_id}"
        status    = match.get("matchstatus", "")
        pid_a     = speler_id(match.get("playerA", {}))
        pid_b     = speler_id(match.get("playerB", {}))
        naam_a    = speler_naam(match.get("playerA", {}))
        naam_b    = speler_naam(match.get("playerB", {}))

        # Sla over als één of beide spelers nog onbekend zijn (TBD in later ronde)
        if not pid_a or not pid_b:
            continue

        print(f"    Match {match_id}: {naam_a} vs {naam_b} | {status}")

        if m_key not in state:
            state[m_key] = {
                "vorigeStatus":      "",
                "meldingKlaarStart": False,
                "meldingAfgelopen":  False,
            }

        ms = state[m_key]

        # 1. Wedstrijd kan starten / is gestart
        # Stuur pas een notificatie zodra er een tafel is toegewezen —
        # dat is het echte sein dat een speler naar de tafel moet.
        # "notstarted" zonder tafel betekent alleen dat de wedstrijd in de
        # bracket staat; pas met een tafelnummer is de wedstrijd echt geroepen.
        heeft_tafel = bool(match.get("table") and match.get("table", {}).get("name"))
        if status in ("notstarted", "waiting", "playing") and is_speelbaar(match) and heeft_tafel and not ms["meldingKlaarStart"]:
            notificeer_klaar_om_te_starten(match, naam, pid_a, pid_b)
            state[m_key]["meldingKlaarStart"] = True

        # 2. Wedstrijd afgelopen
        if (status == "finished"
                and ms["vorigeStatus"] != "finished"
                and not ms["meldingAfgelopen"]):
            notificeer_wedstrijd_klaar(match, naam, pid_a, pid_b)
            state[m_key]["meldingAfgelopen"] = True

        state[m_key]["vorigeStatus"] = status

# ─── HOOFDLOGICA ──────────────────────────────────────────────────────────────

def main():
    print(f"=== Cuescore speler-notifier: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")

    state = laad_state()

    # Stap 1: automatisch toernooien van vandaag vinden
    print(f"\nToernooien zoeken voor: {ORG_STUB}")
    toernooi_ids = haal_toernooien_vandaag(ORG_STUB)

    # Stap 2: voeg handmatig opgegeven IDs toe
    for eid in EXTRA_IDS:
        if eid not in toernooi_ids:
            toernooi_ids.append(eid)
            print(f"  Extra toernooi: ID {eid}")

    if not toernooi_ids:
        print("Geen toernooien vandaag — niets te doen.")
        sla_state(state)
        return

    print(f"\nTe monitoren: {len(toernooi_ids)} toernooi(en)")

    for tid in toernooi_ids:
        print(f"\nToernooi {tid}:")
        verwerk_toernooi(tid, state)

    sla_state(state)
    print("\n=== Check klaar ===\n")

    # ── HOE ABONNEREN ALS SPELER ─────────────────────────────────────────────
    # 1. Installeer de Ntfy-app (iOS / Android)
    # 2. Tik op + (abonnement toevoegen)
    # 3. Vul in: mokum-pool-live-<jouw player-ID>
    #    Jouw player-ID staat in de URL van je Cuescore-profiel:
    #    cuescore.com/player/Naam/2781830  →  player-ID = 2781830
    #    Topic wordt dan: mokum-pool-live-2781830
    # 4. Tik op Subscribe — klaar, geen account nodig


if __name__ == "__main__":
    main()
