# Eurowings Flight Tracker

Raspberry-Pi flight tracker for Eurowings flights with local ADS-B reception, event e-mails, Sonos announcements and HomePilot light control.

The repository contains **two versions**:

## 1. Current version - Home Assistant / FlightRadar24

`flug_checker.py`

This is the current production version. The Eurowings calendar provides the day's `EWxxxx` flight numbers and routes. A configured FlightRadar24 integration in Home Assistant resolves the flight number to the operational `EWG...` callsign. The local dump1090 receiver then detects relevant aircraft around the home location.

The last flight of the day is determined by the calendar. In the current Phase 1 implementation, its landing is still tracked with OpenSky. OpenSky resolves the ICAO24 from the `EWG...` callsign itself.

Flow:

```text
Eurowings ICS calendar
        |
        v
Home Assistant + FlightRadar24
EWxxxx -> EWG callsign
        |
        +----> callsigns.json (cache)
        |
        v
local dump1090 / ADS-B
        |
        +----> overflight event -> e-mail + Sonos + HomePilot
        |
        v
last calendar flight -> OpenSky -> landing event -> e-mail + Sonos + HomePilot
```

### Requirements

- Raspberry Pi with Python 3
- dump1090-mutability and an RTL-SDR receiver
- Home Assistant with the FlightRadar24 integration configured
- Home Assistant Long-Lived Access Token
- OpenSky account for the current landing tracker
- optional: Sonos and Rademacher/HomePilot
- an ICS calendar containing Eurowings flight entries such as `EW754: CGN-VIE`

Python packages:

```bash
python3 -m venv /home/pi/flug_env
source /home/pi/flug_env/bin/activate
pip install -r requirements.txt
```

### Configuration

Create `/home/pi/.flugchecker_config` from `.flugchecker_config.example` and protect it:

```bash
chmod 600 /home/pi/.flugchecker_config
```

Never commit the real config or Home Assistant token.

### Run

```bash
/home/pi/flug_env/bin/python /home/pi/flug_checker.py
```

Test mode:

```bash
/home/pi/flug_env/bin/python /home/pi/flug_checker.py --test
```

For permanent operation use the systemd units in `systemd/`.

---

## 2. Legacy version - Daily e-mail / PDF / OCR

`legacy/flug_checker_daily_pdf.py`

This version does **not require Home Assistant**. It obtains the `EWG...` callsigns from the Daily roster e-mail and its PDF attachment. PDF text extraction/OCR is therefore required.

Additional packages/tools for the legacy version:

```bash
sudo apt install tesseract-ocr poppler-utils
source /home/pi/flug_env/bin/activate
pip install pdf2image pytesseract pillow
```

The legacy version is kept as a standalone fallback and for users who do not run Home Assistant.

---

## Important files

| File | Purpose |
|---|---|
| `flug_checker.py` | Current HA/FlightRadar24 version |
| `legacy/flug_checker_daily_pdf.py` | Classic Daily/PDF/OCR version |
| `.flugchecker_config.example` | Configuration template without secrets |
| `requirements.txt` | Python dependencies for current version |
| `systemd/flugchecker.service` | Main service |
| `systemd/flug-http.service` | Static HTTP server for Sonos MP3 files |
| `docs/Flug-Checker-Handbuch.pdf` | Setup and recovery documentation |

## Logs

```bash
tail -f /home/pi/flug_checker.log
journalctl -u flugchecker.service -f
```

## Status

Current architecture: **ICS + Home Assistant/FlightRadar24 for callsign resolution; local ADS-B for overflight detection; OpenSky for the last landing.**

Planned Phase 2: replace the OpenSky landing tracker with FlightRadar24 data after the current HA/FR24 version has proven stable in production.

## Security

Do not publish `.flugchecker_config`, Home Assistant tokens, mail passwords or other credentials.
