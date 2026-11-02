#!/usr/bin/env python3
"""
Raspberry Pi Temperature Simulator → MQTT Publisher
====================================================
Mensimulasikan pembacaan suhu CPU Raspberry Pi dan mempublikasikannya
ke MQTT broker publik (broker.emqx.io) pada topic: raspberry_temperature

Jalankan:
    python raspberry_pi_temp.py               # simulasi (tanpa hardware)
    python raspberry_pi_temp.py --real        # baca /sys/class/thermal (Linux RPi)
    python raspberry_pi_temp.py --interval 3  # ubah interval (detik)

Kebutuhan:
    pip install paho-mqtt
"""

import argparse
import json
import logging
import math
import random
import string
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# ─── Konfigurasi ───────────────────────────────────────────────────────────────
BROKER_HOST = "broker.emqx.io"
BROKER_PORT = 1883
TOPIC       = "raspberry_temperature"
# Random suffix agar Client ID unik setiap run — mencegah konflik di broker publik
_SUFFIX     = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
CLIENT_ID   = f"rpi-temp-uts-yd3adb-{_SUFFIX}"
QOS         = 1
KEEPALIVE   = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("rpi-temp")


# ─── Sensor (Simulasi & Real) ──────────────────────────────────────────────────

class SimulatedSensor:
    """
    Mensimulasikan suhu CPU Raspberry Pi.
    Suhu berfluktuasi antara 38–72 °C dengan pola sinusoidal + noise,
    meniru pemanasan/pendinginan CPU secara realistis.
    """
    def __init__(self):
        self._t = 0.0
        self._base = 52.0

    def read_celsius(self) -> float:
        self._t += 0.15
        wave  = 8.0 * math.sin(self._t)
        noise = random.gauss(0, 1.2)
        temp  = self._base + wave + noise
        return round(min(85.0, max(35.0, temp)), 2)


class RealSensor:
    """Membaca suhu CPU Raspberry Pi dari /sys/class/thermal/thermal_zone0/temp"""
    THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"

    def read_celsius(self) -> float:
        with open(self.THERMAL_PATH, "r") as f:
            raw = int(f.read().strip())
        return round(raw / 1000.0, 2)


# ─── MQTT ──────────────────────────────────────────────────────────────────────

connected_event = threading.Event()

def build_mqtt_client() -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
    )
    client.reconnect_delay_set(min_delay=2, max_delay=30)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("✅ Terhubung ke MQTT broker %s:%s", BROKER_HOST, BROKER_PORT)
            log.info("   Topic publish: %s", TOPIC)
            connected_event.set()
        else:
            log.error("❌ Koneksi ditolak broker. Kode: %s", reason_code)

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        connected_event.clear()
        if reason_code != 0:
            log.warning("⚠️  Koneksi MQTT terputus (kode %s). Mencoba reconnect…", reason_code)

    def on_publish(client, userdata, mid, reason_code, properties):
        log.debug("   Paket MID=%s terkirim (rc=%s)", mid, reason_code)

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_publish    = on_publish
    return client


# ─── Format Payload ───────────────────────────────────────────────────────────

def build_payload(temp_c: float, seq: int) -> str:
    """
    Payload JSON yang dikirim ke topic raspberry_temperature:
    {
        "device": "RaspberryPi",
        "sensor": "CPU_Temperature",
        "value": 52.34,
        "unit": "C",
        "seq": 1,
        "timestamp": "2026-11-02T02:10:00+00:00"
    }
    """
    data = {
        "device":    "RaspberryPi",
        "sensor":    "CPU_Temperature",
        "value":     temp_c,
        "unit":      "C",
        "seq":       seq,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return json.dumps(data, ensure_ascii=False)


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Raspberry Pi CPU Temperature → MQTT (broker.emqx.io)"
    )
    ap.add_argument(
        "--real", action="store_true",
        help="Baca suhu nyata dari /sys/class/thermal (hanya di Raspberry Pi Linux)"
    )
    ap.add_argument(
        "--interval", type=float, default=5.0,
        metavar="DETIK",
        help="Interval pengiriman data dalam detik (default: 5)"
    )
    ap.add_argument(
        "--count", type=int, default=0,
        help="Hentikan setelah N pengiriman (0 = terus selamanya)"
    )
    args = ap.parse_args()

    sensor = RealSensor() if args.real else SimulatedSensor()
    mode   = "REAL (Raspberry Pi)" if args.real else "SIMULASI"
    log.info("══════════════════════════════════════════════════")
    log.info("  UTS IoT – APRS Telemetry Demo")
    log.info("  Sensor Mode : %s", mode)
    log.info("  Broker      : %s:%s", BROKER_HOST, BROKER_PORT)
    log.info("  Topic       : %s", TOPIC)
    log.info("  Interval    : %.1f detik", args.interval)
    log.info("══════════════════════════════════════════════════")

    client = build_mqtt_client()
    client.connect(BROKER_HOST, BROKER_PORT, KEEPALIVE)
    client.loop_start()

    log.info("Menghubungkan ke broker MQTT…")
    if not connected_event.wait(timeout=10.0):
        log.error("Gagal terhubung ke MQTT broker dalam 10 detik.")
        client.loop_stop()
        return

    seq  = 1
    sent = 0
    try:
        while args.count == 0 or sent < args.count:
            try:
                temp = sensor.read_celsius()
            except FileNotFoundError:
                log.error(
                    "File thermal tidak ditemukan. "
                    "Jalankan di Raspberry Pi atau gunakan mode simulasi (tanpa --real)."
                )
                break
            except Exception as exc:
                log.error("Gagal membaca sensor: %s", exc)
                time.sleep(args.interval)
                continue

            payload = build_payload(temp, seq)

            if client.is_connected():
                info = client.publish(TOPIC, payload=payload, qos=QOS)
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    log.info(
                        "[#%04d] 🌡  Suhu CPU = %.2f °C  → dikirim ke '%s'",
                        seq, temp, TOPIC
                    )
                else:
                    log.warning("[#%04d] Gagal publish (rc=%s)", seq, info.rc)
            else:
                log.warning("[#%04d] MQTT belum terhubung, data dilewati.", seq)

            seq  += 1
            sent += 1

            if args.count == 0 or sent < args.count:
                time.sleep(args.interval)

    except KeyboardInterrupt:
        log.info("Program dihentikan oleh pengguna (Ctrl+C).")
    finally:
        client.loop_stop()
        client.disconnect()
        log.info("Koneksi MQTT ditutup.")


if __name__ == "__main__":
    main()

