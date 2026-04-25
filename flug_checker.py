#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import os
import math
import logging
from logging.handlers import RotatingFileHandler
import json
import time
import argparse
from datetime import datetime, timedelta
import pytz
from ics import Calendar
from soco import SoCo
from soco.snapshot import Snapshot
import re
import threading
import smtplib
from email.mime.text import MIMEText
import sys

# ================= CONFIG =================

def load_config():

    config_path = "/home/pi/.flugchecker_config"

    # Datei vorhanden?
    if not os.path.exists(config_path):
        print("Config Datei fehlt!")
        sys.exit(1)

    config = {}

    with open(config_path) as f:
        for line in f:
            if "=" in line:
                key, value = line.strip().split("=", 1)
                config[key.strip()] = value.strip().strip('"').strip("'")

    # Pflichtfelder prüfen
    required_keys = ["ICS_URL"]

    for key in required_keys:
        if key not in config:
            print(f"Config Eintrag fehlt: {key}")
            sys.exit(1)

    return config


config = load_config()

def get_config(key, default=None, cast=str):
    value = config.get(key, default)

    if value is None:
        return default

    try:
        return cast(value)
    except Exception:
        logger.warning(f"Config Fehler bei {key}: '{value}' → nutze Default {default}")
        return default

# --- Externe Secrets ---
ICS_URL = config["ICS_URL"]

# OpenSky (neu)
OPENSKY_USER = config.get("OPENSKY_USER")
OPENSKY_PASS = config.get("OPENSKY_PASS")

MAIL_USER = get_config("MAIL_USER")
MAIL_PASSWORD = get_config("MAIL_PASSWORD")

# --- Feste Config ---
TZ = pytz.timezone("Europe/Berlin")

HOME_LAT = get_config("HOME_LAT", 0, float)
HOME_LON = get_config("HOME_LON", 0, float)
RADIUS_KM = get_config("RADIUS_KM", 25, float)
INFO_RADIUS_KM = get_config("INFO_RADIUS_KM", 50, float)

SONOS_IP = get_config("SONOS_IP")
PI_IP = get_config("PI_IP")
HTTP_PORT = get_config("HTTP_PORT", 8000, int)

ALARM_VOLUME_BOOST = 25

MP3_OVERFLIGHT = "/home/pi/overflight_alert.mp3"
MP3_LANDING_DAY = "/home/pi/landing_day.mp3"
MP3_LANDING_NIGHT = "/home/pi/landing_night.mp3"

AIRCRAFT_JSON = "/run/dump1090-mutability/aircraft.json"

LOGFILE = "/home/pi/flug_checker.log"

HOMEPILOT_URL = get_config("HOMEPILOT_URL")

MY_MAIL = get_config("MY_MAIL")
WIFE_MAIL = get_config("WIFE_MAIL")

# ================= LOGGER =================

logger = logging.getLogger("flugchecker")
logger.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")

import time
formatter.converter = time.localtime

file_handler = RotatingFileHandler(
    LOGFILE,
    maxBytes=2*1024*1024,   # 2 MB
    backupCount=2
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# ================= MAIL Benschrichtigung =================

def send_mail(subject, text, to):

    try:
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = MAIL_USER
        msg["To"] = ", ".join(to)

        with smtplib.SMTP_SSL("mail.gmx.net", 465) as server:

           server.login(MAIL_USER, MAIL_PASSWORD)
           server.send_message(msg)

        logger.info(f"MAIL GESENDET an {to}")

    except Exception as e:
        logger.error(f"MAIL ERROR {e}")

# ================= MAIL EINLESEN=================

import imaplib
import email
import re
from pdf2image import convert_from_path
import pytesseract

def test_mail_login():

    def extract_relevant_section(text):

        text = text.upper()

        start_match = re.search(r"FLIGHTS TODAY", text)

        if not start_match:
            logger.warning("⚠️ 'Flights today' nicht gefunden → kompletter Text wird genutzt")
            return text

        start_idx = start_match.start()

        end_match = re.search(
            r"(TOMORROW|CREW MEMBER)",
            text[start_idx:]
        )

        if end_match:
            end_idx = start_idx + end_match.start()
            section = text[start_idx:end_idx]
        else:
            section = text[start_idx:start_idx + 2000]

        logger.debug(f"SECTION (kurz): {section[:200]}")

        return section


    def extract_callsigns(text, is_ocr=False):

        logger.debug(f"RAW TEXT (kurz): {text[:200]}")

        # 🔥 NORMALISIERUNG
        text = text.upper()
        text = text.replace("\n", " ")
        text = text.replace("(", " ").replace(")", " ")

        # Sonderzeichen entfernen
        text = re.sub(r"[^A-Z0-9\s]", "", text)

        # 🔥 Unterschied PDF vs OCR
        if is_ocr:
            text = text.replace(" ", "")
            matches = re.findall(r"EWG[A-Z0-9]{2,4}", text)
        else:
            matches = re.findall(r"\bEWG\s?[A-Z0-9]{2,4}\b", text)

        logger.debug(f"CLEAN TEXT (kurz): {text[:200]}")
        logger.debug(f"RAW MATCHES: {matches}")

        callsigns = []

        for cs in matches:
            cs = cs.replace(" ", "")

            if is_ocr:
                cs = cs.rstrip("LI1]")
                cs = cs[:7]

            # harte Validierung
            if re.match(r"^EWG[A-Z0-9]{2,4}$", cs):
                callsigns.append(cs)

        # Duplikate entfernen (Reihenfolge behalten!)
        callsigns = list(dict.fromkeys(callsigns))

        logger.debug(f"CLEAN CALLSIGNS: {callsigns}")

        return callsigns


    try:
        mail = imaplib.IMAP4_SSL("imap.gmx.net", timeout=10)
        mail.login(MAIL_USER, MAIL_PASSWORD)
        mail.select("inbox")

        status, data = mail.search(None, '(UNSEEN SUBJECT "Daily")')

        if not data[0]:
            logger.info("KEINE PASSENDE MAIL")
            return

        latest_email_id = data[0].split()[-1]

        status, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        subject = msg.get("Subject", "")
        logger.info(f"MAIL SUBJECT: {subject}")

        for part in msg.walk():

            if part.get_content_type() == "application/pdf":

                filepath = "/home/pi/Document.pdf"

                with open(filepath, "wb") as f:
                    f.write(part.get_payload(decode=True))

                logger.info("PDF gespeichert")

                # =========================
                # 🔥 1. PDFTEXT FIRST
                # =========================

                text = ""

                try:
                    import subprocess

                    logger.info("PDFTEXT START")

                    text = subprocess.check_output(
                        ["pdftotext", filepath, "-"],
                        text=True
                    )

                    logger.info("PDFTEXT OK")

                except Exception as e:
                    logger.error(f"PDFTEXT ERROR {e}")

                section = extract_relevant_section(text)
                callsigns = extract_callsigns(section, is_ocr=False)

                # =========================
                # 🔥 2. OCR FALLBACK
                # =========================

                if not callsigns:

                    logger.info("KEINE CALLSIGNS VIA PDFTEXT → OCR FALLBACK")

                    try:
                        images = convert_from_path(filepath, first_page=1, last_page=1, dpi=300)

                        text_ocr = ""

                        for img in images:
                            text_ocr += pytesseract.image_to_string(img)

                        section = extract_relevant_section(text_ocr)
                        callsigns = extract_callsigns(section, is_ocr=True)

                    except Exception as e:
                        logger.error(f"OCR ERROR {e}")

                # =========================
                # 🔥 ERGEBNIS
                # =========================

                if callsigns:

                    logger.info(f"FINAL CALLSIGNS: {callsigns}")

                    send_mail(
                        "✅ Daily verarbeitet",
                        f"Heutige Fluege:\n{callsigns}",
                        [MY_MAIL]
                    )

                    return callsigns

                else:
                    logger.warning("❌ KEIN CALLSIGN GEFUNDEN")

        mail.logout()

    except Exception as e:
        logger.error(f"MAIL ERROR {e}")

# ================= DISTANCE =================

def distance_km(lat1, lon1, lat2, lon2):

    R = 6371

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat/2)**2 +
        math.cos(math.radians(lat1)) *
        math.cos(math.radians(lat2)) *
        math.sin(dlon/2)**2
    )

    return 2 * R * math.asin(math.sqrt(a))

# ================= SONOS =================

def sonos_play(mp3):

    try:

        sonos = SoCo(SONOS_IP)

        snap = Snapshot(sonos)
        snap.snapshot()

        old_volume = sonos.volume
        alarm_volume = min(old_volume + ALARM_VOLUME_BOOST, 70)

        sonos.volume = alarm_volume

        url = f"http://{PI_IP}:{HTTP_PORT}/{os.path.basename(mp3)}"

        logger.info(f"SONOS PLAY {mp3}")

        sonos.play_uri(url)

        # Sonos Zeit geben den Stream zu starten
        time.sleep(2)

        # warten bis Sound fertig ist
        while True:

            state = sonos.get_current_transport_info()['current_transport_state']

            if state != "PLAYING":
                break

            time.sleep(1)

        # kleinen Moment warten bevor restore
        time.sleep(0.5)

        snap.restore()

        sonos.volume = old_volume

    except Exception as e:

        logger.error(f"SONOS ERROR {e}")

# ================= LAMPE =================

lamp_state = {}

def lamp_eurowings():

    global lamp_state

    try:
        r = requests.get(HOMEPILOT_URL, timeout=5)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"Lampe nicht erreichbar: {e}")
        return

    try:
        data = r.json()
        caps = data["payload"]["device"]["capabilities"]

        # Zustand merken
        for c in caps:
            if c["name"] == "RGB_CFG":
                lamp_state["rgb"] = c["value"]
            if c["name"] == "COLOR_TEMP_CFG":
                lamp_state["temp"] = c["value"]

        logger.info(f"LAMPE ALT rgb={lamp_state.get('rgb')} temp={lamp_state.get('temp')}")

        # PULSIEREN

        logger.info("LAMPE PULSIERT (ORIGINAL STYLE)")

        start = time.time()

        while time.time() - start < 90:

            # Magenta AN
            requests.put(
                HOMEPILOT_URL,
                headers={"Content-Type": "application/json"},
                data='{"name":"SET_RGB_CMD","value":"0xE20074"}'
            )

            time.sleep(1.2)

            # AUS
            requests.put(
                HOMEPILOT_URL,
                headers={"Content-Type": "application/json"},
                data='{"name":"TURN_OFF_CMD"}'
            )

            time.sleep(0.6)

        # Restore

        lamp_restore()

    except Exception as e:
        logger.error(f"LAMP ERROR {e}")


def lamp_restore():

    global lamp_state

    try:
        logger.info("LAMPE RESTORE")

        if "rgb" in lamp_state:
            requests.put(
                HOMEPILOT_URL,
                headers={"Content-Type": "application/json"},
                data=f'{{"name":"SET_RGB_CMD","value":"{lamp_state["rgb"]}"}}'
            )

        if "temp" in lamp_state:
            requests.put(
                HOMEPILOT_URL,
                headers={"Content-Type": "application/json"},
                data=f'{{"name":"SET_COLOR_TEMP_CMD","value":"{lamp_state["temp"]}"}}'
            )

    except Exception as e:
        logger.error(f"LAMP RESTORE ERROR {e}")

# ================= OVERFLIGHT COOLDOWN =================

last_alert = {}
ALERT_COOLDOWN = 300

# ================= CALENDAR =================

def todays_flights():

    try:

        r = requests.get(ICS_URL, timeout=20)
        calendars = Calendar.parse_multiple(r.text)

    except Exception as e:
        logger.error(f"Kalender Fehler: {e}")
        return []

    flights = []

    now = datetime.now(TZ)

    start_window = now - timedelta(hours=8)
    end_window = now.replace(hour=23, minute=59, second=59)

    for cal in calendars:

        for ev in cal.events:

            if not ev.begin or not ev.name: continue

            t = ev.begin.astimezone(TZ)

            if not (start_window <= t <= end_window):
                continue

            m = re.search(r"EW\s*-?\s*(\d{1,4})", ev.name)

            # 🔥 Route extrahieren (z.B. "SKG-CGN")
            route_match = re.search(r"([A-Z]{3})\s*-\s*([A-Z]{3})", ev.name)

            if m:
                flight_number = f"EW{m.group(1)}"

                dep = None
                arr = None

                if route_match:
                    dep = route_match.group(1)
                    arr = route_match.group(2)

                flights.append((t, flight_number, dep, arr))

    flights.sort()

    flight_names = [f[1] for f in flights]
    logger.info("KALENDER FLUEGE: " + ", ".join(flight_names))

    return flights

# ================= AIRCRAFT =================

def read_aircraft():

    try:
        with open(AIRCRAFT_JSON, "r") as f:
            data = json.load(f)
    except:
        return []

    result = []

    for ac in data.get("aircraft", []):

        if "flight" not in ac:
            continue

        if "lat" not in ac or "lon" not in ac:
            continue

        flight = ac["flight"].strip()

        track = ac.get("track", 0)

        result.append((flight, ac["lat"], ac["lon"], track))

    return result

# ================= TESTMODUS =================

def test_mode():

    logger.info("========== TESTMODUS ==========")

    send_mail(
        "🧪 Testmodus",
        "Flugchecker Test wurde ausgelöst",
        [MY_MAIL]
    )

    threading.Thread(target=sonos_play, args=(MP3_OVERFLIGHT,)).start()
    threading.Thread(target=lamp_eurowings).start()

    logger.info("TESTMODUS ENDE")

# ================= MAIN =================

def main():

    flights = todays_flights()
    last_calendar_update = datetime.now()
    last_flight_time = None

    if not flights:
        logger.info("Keine Fluege im Zeitfenster")
        last_flight = None
        last_flight_time = None
        last_dep = None
        last_arr = None
    else:
        last_flight = flights[-1]
        last_flight_time = last_flight[0]
        last_dep = last_flight[2]
        last_arr = last_flight[3]

        flight_time = last_flight[0].strftime("%H:%M")
        flight_number = last_flight[1]

        logger.info(f"Letzter Flug heute: {flight_number} um {flight_time}")
        logger.info(f"Route: {last_dep} -> {last_arr}")

    overflight_triggered = set()

    tracked_icao = None
    tracked_callsign = None
    flight_landed = False

    min_distances = {}

    last_altitude = None

    # 🔥 Logging Steuerung
    last_no_callsign_log = 0

    # 🔥 NEU OpenSky Steuerung
    last_callsign_search = 0
    last_tracking_check = 0
    last_tracking_api_call = 0
    request_counter = 0
    CALLSIGN_SEARCH_INTERVAL = 900   # 15 min
    CALLSIGN_ACTIVE_INTERVAL = 180    # 3 min

    opensky_tracked_icao = None

    # 🔥 NEU: Tracking Zustand
    last_seen_timestamp = None
    was_airborne = False
    was_below_fl100 = False
    prev_groundspeed = None

    # 🔥 Mail / Callsign Handling
    MY_CALLSIGNS = []
    LAST_CALLSIGN = None

    try:
        with open("/home/pi/callsigns.json", "r") as f:
            MY_CALLSIGNS = json.load(f)

            if MY_CALLSIGNS:
                LAST_CALLSIGN = MY_CALLSIGNS[-1]
                logger.info(f"CALLSIGNS geladen: {MY_CALLSIGNS}")
                logger.info(f"LETZTER CALLSIGN (geladen): {LAST_CALLSIGN}")

    except Exception:
        logger.info("Keine gespeicherten Callsigns gefunden")

    if not MY_CALLSIGNS:
        logger.info("KEINE CALLSIGNS AKTIV")

    MAIL_INTERVAL = 300 # Mailabruf 5 Minuten
    last_mail_check = datetime.now() - timedelta(seconds=MAIL_INTERVAL)

    logger.info("TRACKING START")

    while True:

        try:

            # ================= MAIL CHECK =================

            diff = (datetime.now() - last_mail_check).total_seconds()
            # logger.info(f"MAIL TIMER: diff={diff:.1f}")

            if datetime.now() - last_mail_check > timedelta(seconds=MAIL_INTERVAL):

                logger.info("MAIL CHECK START")

                try:
                    callsigns = test_mail_login()
                except Exception as e:
                    logger.error(f"MAIL FETCH ERROR {e}")
                    callsigns = None

                last_mail_check = datetime.now()

                if callsigns:

                    MY_CALLSIGNS = list(dict.fromkeys(callsigns))
                    LAST_CALLSIGN = callsigns[-1]

                    logger.info(f"MEINE CALLSIGNS: {MY_CALLSIGNS}")
                    logger.info(f"LETZTER CALLSIGN: {LAST_CALLSIGN}")

                    try:
                        with open("/home/pi/callsigns.json", "w") as f:
                            json.dump(callsigns, f)
                    except Exception as e:
                        logger.error(f"CALLSIGN SAVE ERROR {e}")

                logger.info("MAIL CHECK DONE")

            # ================= KALENDER UPDATE =================

            if datetime.now() - last_calendar_update > timedelta(minutes=5): # iCloud 5 Min

                logger.info("Kalender wird neu eingelesen")

                flights = todays_flights()

                if flights:
                    last_flight = flights[-1]
                    last_flight_time = last_flight[0]
                    last_dep = last_flight[2]
                    last_arr = last_flight[3]

                    flight_time = last_flight[0].strftime("%H:%M")
                    flight_number = last_flight[1]

                    logger.info(f"Letzter Flug heute: {flight_number} um {flight_time}")
                    logger.info(f"Route: {last_dep} -> {last_arr}")

                last_calendar_update = datetime.now()

            # ================= AIRCRAFT =================

            aircraft = read_aircraft()

            for ac in aircraft:

                flight = ac[0].strip().upper()
                lat = ac[1]
                lon = ac[2]
                track = ac[3]

                if not MY_CALLSIGNS:
                    if time.time() - last_no_callsign_log > 60:
                        logger.info("KEINE CALLSIGNS → kein Tracking aktiv")
                        last_no_callsign_log = time.time()
                    continue

                if flight not in MY_CALLSIGNS:
                    continue

                dist = distance_km(HOME_LAT, HOME_LON, lat, lon)

                if dist > INFO_RADIUS_KM:
                    continue

                # ICAO24 holen
                icao24 = None
                try:
                    with open(AIRCRAFT_JSON, "r") as f:
                        data = json.load(f)
                        for a in data.get("aircraft", []):
                            if a.get("flight", "").strip().upper() == flight:
                                icao24 = a.get("hex")
                                break
                except:
                    pass

                ew = flight

                if ew in overflight_triggered:
                    continue

                # Distanz berechnen hast du schon:
                # dist = distance_km(...)

                # 🔥 INITIAL
                if ew not in min_distances:
                    min_distances[ew] = dist

                # 🔥 IMMER Minimum aktualisieren
                if dist < min_distances[ew]:
                    min_distances[ew] = dist

                # 🔥 CHECK: entfernt sich → Minimum erreicht
                if dist > min_distances[ew] + 1:

                    min_dist = min_distances[ew]

                    # 👉 nur reagieren, wenn innerhalb Info-Radius
                    if min_dist <= INFO_RADIUS_KM:

                        logger.info(
                            f"✈️ CLOSEST APPROACH: {flight} MIN_DIST {min_dist:.1f} km (jetzt {dist:.1f})"
                        )

                        now_ts = time.time()

                        if flight in last_alert:
                            if now_ts - last_alert[flight] < ALERT_COOLDOWN:
                                continue

                        last_alert[flight] = now_ts

                        # 🔥 ENTSCHEIDUNG: Nähe oder Überflug
                        is_overflight = min_dist <= RADIUS_KM

                        # 🔥 NUR EINE MAIL
                        if is_overflight:
                            subject = f"✈️ {ew} UEBERFLUG"
                            text = f"Min Distanz: {min_dist:.1f} km\n➡️ UEBERFLUG!"
                        else:
                            subject = f"✈️ {ew} Nähe"
                            text = f"Min Distanz: {min_dist:.1f} km\nJetzt: {dist:.1f} km"

                        send_mail(subject, text, [MY_MAIL])

                        # 🚨 Aktionen nur bei echtem Überflug
                        if is_overflight:

                            threading.Thread(target=sonos_play, args=(MP3_OVERFLIGHT,)).start()
                            threading.Thread(target=lamp_eurowings).start()

                            if icao24:
                                tracked_icao = icao24
                                tracked_callsign = flight
                                logger.info(f"TRACKING ICAO24: {tracked_icao} ({tracked_callsign})")

                        overflight_triggered.add(ew)

            # ================= OPENSKY LANDING =================

            if time.time() - last_tracking_check >= 10:
                last_tracking_check = time.time()

                if not last_flight_time:
                    opensky_tracked_icao = None

                elif LAST_CALLSIGN and last_flight_time:

                    now_dt = datetime.now(TZ)

                    time_to_departure = None
                    search_allowed = False

                    if last_flight_time:
                        time_to_departure = (last_flight_time - now_dt).total_seconds()

                        # 🔍 SEARCH nur ab 5 min vor Abflug
                        if time_to_departure <= 300:
                            search_allowed = True

                    now_ts = time.time()

                    # PHASE 1: ICAO suchen (alle 15  min)
                    if LAST_CALLSIGN and not opensky_tracked_icao and search_allowed:

                        if now_ts - last_callsign_search > CALLSIGN_SEARCH_INTERVAL:

                            last_callsign_search = now_ts

                            logger.info(f"OPENSKY SEARCH: {LAST_CALLSIGN}")

                            try:
                                request_counter += 1
                                logger.info(f"OPENSKY REQUEST #{request_counter} (SEARCH)")

                                r = requests.get(
                                    "https://opensky-network.org/api/states/all",
                                    auth=(OPENSKY_USER, OPENSKY_PASS),
                                    timeout=10
                                )
                                logger.info(f"RATE: {request_counter} requests total")

                                if r.status_code != 200:
                                    logger.warning(f"OPENSKY HTTP {r.status_code}")
                                    if r.status_code == 429:
                                        logger.warning("⏳ Rate Limit → Pause 10min")
                                        time.sleep(600)
                                    continue

                                if not r.text:
                                    logger.warning("OPENSKY EMPTY RESPONSE (SEARCH)")
                                    continue

                                try:
                                    data = r.json()
                                except Exception as e:
                                    logger.error(f"OPENSKY JSON ERROR (SEARCH) {e}")
                                    continue

                                states = data.get("states", [])

                                best_match = None
                                best_diff = None

                                for s in states:
                                    callsign = s[1].strip() if s[1] else ""

                                    if callsign != LAST_CALLSIGN:
                                        continue

                                    # OpenSky Zeitstempel
                                    if len(s) > 3 and s[3] and last_flight_time:

                                        state_time = datetime.fromtimestamp(s[3], tz=TZ)

                                        diff = abs((state_time - last_flight_time).total_seconds())

                                        logger.info(
                                            f"CANDIDATE: {callsign} "
                                            f"STATE {state_time.strftime('%H:%M:%S')} "
                                            f"STD {last_flight_time.strftime('%H:%M')} "
                                            f"DIFF {int(diff/60)} min"
                                        )

                                        if best_diff is None or diff < best_diff:
                                            best_diff = diff
                                            best_match = s

                                # ✅ FINAL MATCH (kommt NACH der Schleife!)
                                if best_match:

                                    opensky_tracked_icao = best_match[0]

                                    logger.info(f"OPENSKY FOUND ICAO: {opensky_tracked_icao}")
                                    logger.info(
                                        f"MATCH OK: {LAST_CALLSIGN} "
                                        f"DIFF {int(best_diff/60)} min"
                                    )
                                    logger.info(f"START TRACKING {LAST_CALLSIGN}")

                                    tracked_callsign = LAST_CALLSIGN

                                    was_airborne = False
                                    was_below_fl100 = False
                                    prev_groundspeed = None
                                    last_altitude = None

                            except Exception as e:
                                logger.warning(f"OPENSKY SEARCH ERROR {e}")

                    # PHASE 2: Tracking (alle 10 min)
                    if opensky_tracked_icao:

                        if now_ts - last_tracking_api_call > CALLSIGN_ACTIVE_INTERVAL:

                            last_tracking_api_call = now_ts

                            logger.info(f"OPENSKY TRACKING: {opensky_tracked_icao}")

                            try:
                                request_counter += 1
                                logger.info(f"OPENSKY REQUEST #{request_counter} (TRACK)")

                                r = requests.get(
                                    "https://opensky-network.org/api/states/all",
                                    auth=(OPENSKY_USER, OPENSKY_PASS),
                                    timeout=10
                                )
                                logger.info(f"RATE: {request_counter} requests total")
                                if r.status_code != 200:
                                    logger.warning(f"OPENSKY HTTP {r.status_code}")
                                    if r.status_code == 429:
                                        logger.warning("⏳ Rate Limit → Pause 120s")
                                        time.sleep(120)
                                    continue

                                if not r.text:
                                    logger.warning("OPENSKY EMPTY RESPONSE (TRACK)")
                                    continue

                                try:
                                    data = r.json()
                                except Exception as e:
                                    logger.error(f"OPENSKY JSON ERROR (TRACK) {e}")
                                    continue

                                states = data.get("states", [])

                                found_state = None

                                for s in states:
                                    if s[0] == opensky_tracked_icao:
                                        found_state = s
                                        break

                                if found_state:

                                    last_seen_timestamp = time.time()

                                    velocity = found_state[9] or 0
                                    altitude = found_state[13] or 0
                                    groundspeed = velocity * 1.94384  # knots

                                    logger.info(
                                        f"LANDING CHECK {tracked_callsign}: "
                                        f"GS={groundspeed:.1f} "
                                        f"prev={prev_groundspeed if prev_groundspeed is not None else 'None'} "
                                        f"alt={altitude:.0f}"
                                    )

                                    last_altitude = altitude

                                    logger.info(f"STATE: GS={groundspeed:.1f}kt alt={altitude:.0f}")

                                    # ✈️ war wirklich in der Luft?
                                    if altitude > 1000:
                                        was_airborne = True

                                    # 🔻 unter FL100 (ca. 10.000 ft = 3048 m)
                                    if altitude < 1000:
                                        was_below_fl100 = True

                                    # 🛬 PRIMARY: Landing via Groundspeed
                                    if (
                                        was_airborne
                                        and altitude < 1000
                                        and prev_groundspeed is not None
                                        and prev_groundspeed > 80
                                        and groundspeed < 40
                                    ):

                                        logger.info("🛬 GELANDET (GS erkannt)")

                                        send_mail(
                                            "🛬 Touchdown",
                                            "Ich bin gelandet ❤️",
                                            [MY_MAIL, WIFE_MAIL]
                                        )

                                        hour = datetime.now(TZ).hour

                                        if 8 <= hour < 20:
                                            threading.Thread(target=sonos_play, args=(MP3_LANDING_DAY,)).start()
                                        else:
                                            threading.Thread(target=sonos_play, args=(MP3_LANDING_NIGHT,)).start()

                                        threading.Thread(target=lamp_eurowings).start()

                                        # RESET
                                        opensky_tracked_icao = None
                                        LAST_CALLSIGN = None
                                        MY_CALLSIGNS = []
                                        was_airborne = False
                                        was_below_fl100 = False
                                        prev_groundspeed = None
                                        last_seen_timestamp = None

                                        try:
                                            os.remove("/home/pi/callsigns.json")
                                            logger.info("CALLSIGNS DATEI GELÖSCHT")
                                        except:
                                            pass

                                    prev_groundspeed = groundspeed

                                else:
                                    if was_airborne and was_below_fl100:
 
                                        logger.info("NICHT IM STATE GEFUNDEN")

                                        if last_seen_timestamp:

                                            time_missing = time.time() - last_seen_timestamp

                                            logger.info(f"STATE MISSING SEIT {int(time_missing)}s")

                                            # 🛬 FALLBACK (z.B. bei Signalverlust)
                                            if time_missing > 600 and last_altitude is not None and last_altitude < 1000:

                                                logger.info("🛬 GELANDET (Fallback)")

                                                send_mail(
                                                    "🛬 Touchdown",
                                                    "Ich bin gelandet ❤️",
                                                    [MY_MAIL, WIFE_MAIL]
                                                )

                                                hour = datetime.now(TZ).hour

                                                if 8 <= hour < 20:
                                                    threading.Thread(target=sonos_play, args=(MP3_LANDING_DAY,)).start()
                                                else:
                                                    threading.Thread(target=sonos_play, args=(MP3_LANDING_NIGHT,)).start()

                                                threading.Thread(target=lamp_eurowings).start()

                                                # RESET
                                                opensky_tracked_icao = None
                                                LAST_CALLSIGN = None
                                                MY_CALLSIGNS = []
                                                was_airborne = False
                                                was_below_fl100 = False
                                                prev_groundspeed = None
                                                last_seen_timestamp = None

                                                try:
                                                    os.remove("/home/pi/callsigns.json")
                                                    logger.info("CALLSIGNS DATEI GELÖSCHT")
                                                except:
                                                    pass

                            except Exception as e:
                                logger.warning(f"OPENSKY TRACK ERROR {e}")

            time.sleep(3)

        except Exception as e:
            logger.error(f"MAIN LOOP ERROR {e}")
            time.sleep(5)

# ================= START =================

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")

    args = parser.parse_args()

    try:

        if args.test:
            test_mode()
        else:
            main()

    except Exception as e:

        logger.error(f"FATAL ERROR: {e}")

        try:
            send_mail(
                "🚨 Flugchecker abgestuerzt!",
                f"Fehler:\n{e}",
                [MY_MAIL]
            )
        except Exception as mail_error:
            logger.error(f"Crash-Mail fehlgeschlagen: {mail_error}")

        sys.exit(1)
