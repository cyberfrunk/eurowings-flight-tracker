# ✈️ Flight Checker (Raspberry Pi)

Personal flight tracking system for pilots using ADS-B, OpenSky and roster emails.

---

## 🧠 What it does

This project automatically tracks **your own flights** and notifies you when:

* ✈️ your aircraft is flying near your home
* 🛫 your flight is active
* 🛬 you have landed

It combines multiple data sources:

* 📡 ADS-B (dump1090)
* 🌍 OpenSky Network
* 📅 Calendar (ICS)
* ✉️ Daily roster email (PDF + OCR)

---

## 🔥 Features

* Callsign extraction from email (PDF + OCR)
* Live tracking via ADS-B + OpenSky
* Overflight detection (distance-based)
* Smart landing detection (speed + altitude logic)
* Sonos audio alerts
* Smart light (HomePilot) integration
* Automatic daily operation via systemd

---

## 🏗️ System Overview

```
Mail (PDF) → OCR → Callsigns
                     ↓
            ADS-B (dump1090)
                     ↓
               Flight Checker
                     ↓
        OpenSky (landing detection)
                     ↓
        🔊 Sonos + 💡 Smart Light
```

---

## ⚙️ Setup

### 1. Clone repository

```bash
git clone https://github.com/cyberfrunk/raspi-flight-checker.git
cd raspi-flight-checker
```

---

### 2. Create config

```bash
cp .flugchecker_config.example .flugchecker_config
nano .flugchecker_config
```

Fill in your credentials (mail, OpenSky, location, etc.)

---

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

### 4. Install system dependencies

```bash
sudo apt install dump1090-mutability tesseract-ocr poppler-utils
```

---

### 5. Start manually (test)

```bash
python3 flug_checker.py
```

---

## 🔧 Autostart (systemd)

Service files are included in `/systemd`.

Install:

```bash
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable flugchecker
sudo systemctl enable flug-http
sudo systemctl start flugchecker
```

---

## 🔊 Audio Setup

Create your own audio files:

```plaintext
/home/pi/overflight_alert.mp3
/home/pi/landing_day.mp3
/home/pi/landing_night.mp3
```

---

## ✉️ Mail Requirement

The system depends on a **daily email**:

* Subject: `Daily`
* Contains PDF attachment (`Document.pdf`)
* Includes flight information

⚠️ Without this email → no tracking

---

## 📂 Project Structure

```
.
├── flug_checker.py
├── requirements.txt
├── systemd/
├── docs/
└── README.md
```

---

## 📖 Documentation

Full setup guide:

👉 docs/setup.md

---

## 🔐 Security Notes

* Config file is excluded (`.flugchecker_config`)
* No credentials are stored in the repository
* Audio files are not included

---

## 👨‍✈️ Author

Built for personal flight awareness.
