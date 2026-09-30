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

# Home Assistant / FlightRadar24
HA_URL = get_config("HA_URL")
HA_TOKEN = get_config("HA_TOKEN")
HA_FR24_ADD_ENTITY = "text.flightradar24_add_to_track"
HA_FR24_SENSOR = "sensor.flightradar24_additional_tracked"

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

# ================= HOME ASSISTANT / FLIGHTRADAR24 =================

def ha_headers():
    if not HA_TOKEN:
        return {}
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }


def ha_fr24_add_flight(flight_number):
    """Flugnummer einmalig an die FR24-Integration in Home Assistant übergeben."""
    if not HA_URL or not HA_TOKEN:
        logger.error("HA_URL oder HA_TOKEN fehlt in /home/pi/.flugchecker_config")
        return False

    try:
        r = requests.post(
            f"{HA_URL.rstrip('/')}/api/services/text/set_value",
            headers=ha_headers(),
            json={"entity_id": HA_FR24_ADD_ENTITY, "value": flight_number},
            timeout=10,
        )
        r.raise_for_status()
        logger.info(f"HA/FR24 TRACKING ANGEFORDERT: {flight_number}")
        return True
    except Exception as e:
        logger.warning(f"HA/FR24 ADD ERROR {flight_number}: {e}")
        return False


def ha_fr24_get_flights():
    """Von HA bereits aufgelöste FR24-Flüge lesen."""
    if not HA_URL or not HA_TOKEN:
        return []

    try:
        r = requests.get(
            f"{HA_URL.rstrip('/')}/api/states/{HA_FR24_SENSOR}",
            headers=ha_headers(),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        flights = data.get("attributes", {}).get("flights", [])
        return flights if isinstance(flights, list) else []
    except Exception as e:
        logger.warning(f"HA/FR24 READ ERROR: {e}")
        return []


def resolve_callsigns_via_ha(flights, requested_flights, known_mapping):
    """
    Kalender-Flugnummern -> EWG-Callsigns.
    Neue Flugnummern werden nur einmal pro Programmlauf an HA übergeben.
    Danach wird bei jedem Kalenderzyklus nur der HA-Sensor gelesen.
    """
    flight_numbers = [f[1].upper() for f in flights]
    if not flight_numbers:
        return {}, requested_flights

    logger.info("HA/FR24 AUFLOESUNG: " + ", ".join(flight_numbers))

    added = False
    for flight_number in flight_numbers:
        if flight_number not in requested_flights:
            if ha_fr24_add_flight(flight_number):
                requested_flights.add(flight_number)
                added = True

    # Direkt nach neuen Einträgen kurz Zeit für die HA-Integration lassen.
    if added:
        time.sleep(8)

    tracked = ha_fr24_get_flights()
    current = set(flight_numbers)
    mapping = {k: v for k, v in known_mapping.items() if k in current}

    for item in tracked:
        if not isinstance(item, dict):
            continue

        flight_number = str(item.get("flight_number") or "").strip().upper()
        callsign = str(item.get("callsign") or "").strip().upper()
        icao24 = item.get("icao24") or item.get("aircraft_icao24")
        if icao24:
            icao24 = str(icao24).strip().upper()
            if not re.fullmatch(r"[0-9A-F]{6}", icao24):
                icao24 = None

        if flight_number not in current:
            continue
        if not re.fullmatch(r"EWG[A-Z0-9]{2,4}", callsign):
            continue

        mapping[flight_number] = callsign
        logger.info(f"HA/FR24 MATCH: {flight_number} -> {callsign} (ICAO24={icao24})")

    missing = [n for n in flight_numbers if n not in mapping]
    if missing:
        logger.info("HA/FR24 NOCH OHNE MATCH: " + ", ".join(missing))

    return mapping, requested_flights


def save_callsign_cache_if_changed(callsigns):
    """callsigns.json nur schreiben, wenn sich der Inhalt wirklich geändert hat."""
    path = "/home/pi/callsigns.json"
    old = None
    try:
        with open(path, "r") as f:
            old = json.load(f)
    except Exception:
        pass

    if old == callsigns:
        logger.info("CALLSIGN CACHE unverändert")
        return

    try:
        with open(path, "w") as f:
            json.dump(callsigns, f)
        logger.info(f"CALLSIGN CACHE gespeichert: {callsigns}")
    except Exception as e:
        logger.error(f"CALLSIGN SAVE ERROR {e}")


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
    last_flight = None
    last_dep = None
    last_arr = None
    last_flight_number = None

    if not flights:
        logger.info("Keine Fluege im Zeitfenster")
    else:
        last_flight = flights[-1]
        last_flight_time = last_flight[0]
        last_dep = last_flight[2]
        last_arr = last_flight[3]
        last_flight_number = last_flight[1]

        logger.info(f"Letzter Flug heute: {last_flight_number} um {last_flight_time.strftime('%H:%M')}")
        logger.info(f"Route: {last_dep} -> {last_arr}")

    overflight_triggered = set()

    tracked_icao = None
    tracked_callsign = None
    flight_landed = False

    min_distances = {}

    # Dasselbe ATC-Callsign kann am selben Tag spaeter erneut verwendet werden
    # (z.B. Hin- und Rueckflug). Nach 30 Minuten lokaler Abwesenheit wird der
    # Ueberflug-Zustand fuer dieses Callsign neu freigegeben.
    callsign_last_seen = {}
    CALLSIGN_REARM_SECONDS = 1800

    last_altitude = None

    # Logging Steuerung
    last_no_callsign_log = 0

    # OpenSky Steuerung
    last_callsign_search = 0
    last_tracking_check = 0
    last_tracking_api_call = 0
    request_counter = 0
    CALLSIGN_SEARCH_INTERVAL = 900   # 15 min
    CALLSIGN_ACTIVE_INTERVAL = 180   # 3 min

    opensky_tracked_icao = None

    # OpenSky Tracking-Zustand
    last_seen_timestamp = None
    was_airborne = False
    was_below_1000m = False
    prev_groundspeed = None

    # Callsign-Cache ist nur Fallback für Überflüge.
    MY_CALLSIGNS = []
    LAST_CALLSIGN = None
    flight_callsigns = {}
    ha_requested_flights = set()

    try:
        with open("/home/pi/callsigns.json", "r") as f:
            cached = json.load(f)
            if isinstance(cached, list):
                MY_CALLSIGNS = list(dict.fromkeys(cached))
                if MY_CALLSIGNS:
                    logger.info(f"CALLSIGNS CACHE geladen: {MY_CALLSIGNS}")
    except Exception:
        logger.info("Keine gespeicherten Callsigns gefunden")

    # Sofortige Auflösung beim Start. Der letzte Callsign wird ausdrücklich
    # über die letzte Kalender-Flugnummer bestimmt, nicht über Listenpositionen.
    if flights:
        flight_callsigns, ha_requested_flights = resolve_callsigns_via_ha(
            flights, ha_requested_flights, flight_callsigns
        )

        resolved = [flight_callsigns[f[1]] for f in flights if f[1] in flight_callsigns]
        if resolved:
            MY_CALLSIGNS = list(dict.fromkeys(resolved))
            save_callsign_cache_if_changed(MY_CALLSIGNS)
            logger.info(f"MEINE CALLSIGNS (HA/FR24): {MY_CALLSIGNS}")
        elif MY_CALLSIGNS:
            logger.info("HA/FR24 aktuell ohne neue Callsign-Matches; Cache bleibt aktiv")

        LAST_CALLSIGN = flight_callsigns.get(last_flight_number)
        if LAST_CALLSIGN:
            logger.info(f"LETZTER CALLSIGN: {LAST_CALLSIGN}")
        else:
            logger.info(f"LETZTER CALLSIGN für {last_flight_number} noch nicht bekannt")

    if not MY_CALLSIGNS:
        logger.info("KEINE CALLSIGNS AKTIV")

    logger.info("TRACKING START")

    while True:

        try:

            # ================= KALENDER + HA/FR24 UPDATE =================

            if datetime.now() - last_calendar_update > timedelta(minutes=5): # iCloud 5 Min

                logger.info("Kalender wird neu eingelesen")

                previous_last_flight_number = last_flight_number
                flights = todays_flights()

                if flights:
                    last_flight = flights[-1]
                    last_flight_time = last_flight[0]
                    last_dep = last_flight[2]
                    last_arr = last_flight[3]
                    last_flight_number = last_flight[1]

                    logger.info(f"Letzter Flug heute: {last_flight_number} um {last_flight_time.strftime('%H:%M')}")
                    logger.info(f"Route: {last_dep} -> {last_arr}")
                else:
                    last_flight = None
                    last_flight_time = None
                    last_dep = None
                    last_arr = None
                    last_flight_number = None

                    # Keine EW-Flüge mehr im Kalender:
                    # altes Tracking und Callsign-Cache vollständig verwerfen.
                    if MY_CALLSIGNS or LAST_CALLSIGN or opensky_tracked_icao:
                        logger.info("KEINE KALENDERFLUEGE -> altes Tracking wird beendet")

                    MY_CALLSIGNS = []
                    LAST_CALLSIGN = None
                    flight_callsigns = {}

                    opensky_tracked_icao = None
                    tracked_icao = None
                    tracked_callsign = None

                    last_callsign_search = 0
                    last_tracking_api_call = 0
                    last_seen_timestamp = None
                    was_airborne = False
                    was_below_1000m = False
                    prev_groundspeed = None
                    last_altitude = None
                    flight_landed = False

                    try:
                        os.remove("/home/pi/callsigns.json")
                        logger.info("CALLSIGNS CACHE geloescht - keine Kalenderfluege")
                    except FileNotFoundError:
                        pass
                    except Exception as e:
                        logger.warning(f"CALLSIGN CACHE konnte nicht geloescht werden: {e}")

                # Wenn der Kalender einen anderen letzten Flug nennt, darf ein
                # alter OpenSky-Tracker nicht auf dem vorherigen Flug weiterlaufen.
                if previous_last_flight_number != last_flight_number:
                    logger.info(
                        f"LETZTER FLUG GEÄNDERT: {previous_last_flight_number} -> {last_flight_number}; "
                        "OpenSky-Tracking wird zurückgesetzt"
                    )
                    opensky_tracked_icao = None
                    tracked_callsign = None
                    last_callsign_search = 0
                    last_tracking_api_call = 0
                    last_seen_timestamp = None
                    was_airborne = False
                    was_below_1000m = False
                    prev_groundspeed = None
                    last_altitude = None
                    flight_landed = False

                # Nach der letzten erkannten Landung nicht wieder aus dem Kalender
                # Callsigns nachladen und die gelöschte Cache-Datei neu erzeugen.
                if flights and not flight_landed:
                    flight_callsigns, ha_requested_flights = resolve_callsigns_via_ha(
                        flights, ha_requested_flights, flight_callsigns
                    )

                    resolved = [flight_callsigns[f[1]] for f in flights if f[1] in flight_callsigns]
                    if resolved:
                        new_callsigns = list(dict.fromkeys(resolved))
                        if new_callsigns != MY_CALLSIGNS:
                            MY_CALLSIGNS = new_callsigns
                            logger.info(f"MEINE CALLSIGNS (HA/FR24): {MY_CALLSIGNS}")
                        save_callsign_cache_if_changed(MY_CALLSIGNS)
                    elif MY_CALLSIGNS:
                        logger.info("HA/FR24 aktuell ohne neue Callsign-Matches; Cache bleibt aktiv")

                    new_last_callsign = flight_callsigns.get(last_flight_number)

                    # Auch ein geändertes Callsign des letzten Fluges setzt einen
                    # eventuell noch laufenden Tracker des alten Callsigns zurück.
                    if LAST_CALLSIGN and new_last_callsign and LAST_CALLSIGN != new_last_callsign:
                        logger.info(
                            f"LETZTER CALLSIGN GEÄNDERT: {LAST_CALLSIGN} -> {new_last_callsign}; "
                            "OpenSky-Tracking wird zurückgesetzt"
                        )
                        opensky_tracked_icao = None
                        tracked_callsign = None
                        last_callsign_search = 0
                        last_tracking_api_call = 0
                        last_seen_timestamp = None
                        was_airborne = False
                        was_below_1000m = False
                        prev_groundspeed = None
                        last_altitude = None

                    LAST_CALLSIGN = new_last_callsign
                    if LAST_CALLSIGN:
                        logger.info(f"LETZTER CALLSIGN: {LAST_CALLSIGN}")
                    elif last_flight_number:
                        logger.info(f"LETZTER CALLSIGN für {last_flight_number} noch nicht bekannt")

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

                # Ein Callsign kann spaeter am Tag zu einem neuen Kalenderflug
                # gehoeren. War es mindestens 30 Minuten nicht lokal sichtbar,
                # darf es wieder einen eigenen Closest Approach ausloesen.
                now_seen = time.time()
                previous_seen = callsign_last_seen.get(flight)

                if (
                    previous_seen is not None
                    and now_seen - previous_seen >= CALLSIGN_REARM_SECONDS
                ):
                    if (
                        flight in overflight_triggered
                        or flight in min_distances
                        or flight in last_alert
                    ):
                        logger.info(
                            f"CALLSIGN RE-ARM: {flight} nach "
                            f"{(now_seen - previous_seen) / 60:.0f} Min Abwesenheit"
                        )

                    overflight_triggered.discard(flight)
                    min_distances.pop(flight, None)
                    last_alert.pop(flight, None)

                callsign_last_seen[flight] = now_seen

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
                if dist > min_distances[ew] + 0.1:

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

                elif not flight_landed and LAST_CALLSIGN and last_flight_time:

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
                                    was_below_1000m = False
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

                                    # 🔻 unter 1000 m (ca. 3280 ft)
                                    if altitude < 1000:
                                        was_below_1000m = True

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
                                        flight_landed = True
                                        opensky_tracked_icao = None
                                        LAST_CALLSIGN = None
                                        MY_CALLSIGNS = []
                                        was_airborne = False
                                        was_below_1000m = False
                                        prev_groundspeed = None
                                        last_seen_timestamp = None

                                        try:
                                            os.remove("/home/pi/callsigns.json")
                                            logger.info("CALLSIGNS DATEI GELÖSCHT")
                                        except:
                                            pass

                                    prev_groundspeed = groundspeed

                                else:
                                    if was_airborne and was_below_1000m:
 
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
                                                flight_landed = True
                                                opensky_tracked_icao = None
                                                LAST_CALLSIGN = None
                                                MY_CALLSIGNS = []
                                                was_airborne = False
                                                was_below_1000m = False
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
