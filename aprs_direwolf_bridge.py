#!/usr/bin/env python3
"""
APRS–Dire Wolf Bridge  ·  MQTT Subscriber → APRS Telemetry → APRS-IS / aprs.fi
================================================================================
Subscribe ke topic MQTT `raspberry_temperature` di broker.emqx.io, decode data
suhu Raspberry Pi, encode ke APRS Telemetry Packet, lalu:

  1. Cetak paket ke konsol dan simpan di aprs_packets.log
  2. Tulis wxnow.txt (dibaca Dire Wolf via PBEACON commentcmd)
  3. ★ Forward langsung ke APRS-IS (rotate.aprs2.net:14580) → tampil di aprs.fi

Jalankan:
    python aprs_direwolf_bridge.py                    # default YD3ADB-3
    python aprs_direwolf_bridge.py --no-aprsis        # tanpa APRS-IS (offline)
    python aprs_direwolf_bridge.py --callsign YD3ADB-3 --lat -7.2575 --lon 112.7521

Kebutuhan:
    pip install paho-mqtt
"""

import argparse
import json
import logging
import os
import random
import socket
import string
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

# ─── Default config ─────────────────────────────────────────────────────────────
BROKER_HOST = "broker.emqx.io"
BROKER_PORT = 1883
TOPIC       = "raspberry_temperature"
# Random suffix agar Client ID unik setiap run — mencegah konflik di broker publik
_SUFFIX     = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
CLIENT_ID   = f"aprs-bridge-uts-{_SUFFIX}"
KEEPALIVE   = 60

# APRS-IS
APRSIS_HOST     = "rotate.aprs2.net"
APRSIS_PORT     = 14580
APRSIS_PASSCODE = 23973       # hash dari callsign YD3ADB (tanpa SSID)
APRSIS_TIMEOUT  = 10          # detik untuk koneksi/send

WXNOW_FILE   = "wxnow.txt"
APRS_LOGFILE = "aprs_packets.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("aprs-bridge")


# ─── APRS Encoding ──────────────────────────────────────────────────────────────

HIGH_TEMP_THRESHOLD = 70.0  # °C — bit 1 alarm

PARM_STR = "CPUTemp,-,-,-,-,HiTemp,b2,b3,b4,b5,b6,b7,b8"
UNIT_STR = "C,-,-,-,-,yes,b2,b3,b4,b5,b6,b7,b8"
EQNS_STR = "0,0.5,0, 0,1,0, 0,1,0, 0,1,0, 0,1,0"


def to_raw(value: float, step: float = 0.5, offset: float = 0.0) -> int:
    return max(0, min(255, round((value - offset) / step)))


def definition_packets(callsign: str) -> list[str]:
    """4 paket definisi APRS: PARM/UNIT/EQNS/BITS."""
    head = f"{callsign}>APRS,TCPIP*::{callsign:<9}:"
    return [
        f"{head}PARM.{PARM_STR}",
        f"{head}UNIT.{UNIT_STR}",
        f"{head}EQNS.{EQNS_STR}",
        f"{head}BITS.11111111,RaspberryPi CPU Temp",
    ]


def data_packet(callsign: str, seq: int, temp_c: float) -> str:
    """Encode satu data telemetry APRS T# packet."""
    raw   = to_raw(temp_c, step=0.5, offset=0.0)
    alarm = 1 if temp_c > HIGH_TEMP_THRESHOLD else 0
    bits  = f"{alarm}0000000"
    return f"{callsign}>APRS,TCPIP*:T#{seq % 1000:03d},{raw:03d},000,000,000,000,{bits}"


def position_beacon(callsign: str, lat: float, lon: float, temp_c: float) -> str:
    """
    Buat APRS Position Beacon dengan komentar suhu.
    Format latitude: DDMM.MMN / longitude: DDDMM.MME
    Symbol '/' + '_' = weather/telemetry station.
    """
    # Konversi desimal → APRS DDmm.mm
    def to_aprs_lat(d: float) -> str:
        hemi = "N" if d >= 0 else "S"
        d = abs(d)
        deg = int(d)
        mins = (d - deg) * 60
        return f"{deg:02d}{mins:05.2f}{hemi}"

    def to_aprs_lon(d: float) -> str:
        hemi = "E" if d >= 0 else "W"
        d = abs(d)
        deg = int(d)
        mins = (d - deg) * 60
        return f"{deg:03d}{mins:05.2f}{hemi}"

    lat_str = to_aprs_lat(lat)
    lon_str = to_aprs_lon(lon)
    comment = f"RPi CPU Temp={temp_c:.1f}C IoT UTS YD3ADB"
    return f"{callsign}>APRS,TCPIP*:={lat_str}/{lon_str}_...{comment}"


# ─── wxnow.txt ──────────────────────────────────────────────────────────────────

def write_wxnow(temp_c: float, path: str = WXNOW_FILE):
    now  = datetime.now(timezone.utc)
    line = (
        f"{now.year} {now.month:02d} {now.day:02d} "
        f"{now.hour:02d} {now.minute:02d} {now.second:02d} "
        f"T={temp_c:.2f}C"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(line + "\n")


def log_packet(packet: str, path: str = APRS_LOGFILE):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{ts}  {packet}\n")


# ─── APRS-IS Connection ─────────────────────────────────────────────────────────

class APRSISClient:
    """
    Koneksi TCP ke server APRS-IS (rotate.aprs2.net:14580).
    Setelah login, paket yang dikirim langsung masuk ke jaringan APRS global
    dan dapat dilihat di https://aprs.fi
    """

    def __init__(self, callsign: str, passcode: int,
                 host: str = APRSIS_HOST, port: int = APRSIS_PORT,
                 filter_str: str = ""):
        self.callsign  = callsign
        self.passcode  = passcode
        self.host      = host
        self.port      = port
        self.filter    = filter_str
        self._sock: socket.socket | None = None
        self._lock     = threading.Lock()
        self._alive    = False
        self._thread: threading.Thread | None = None

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _login_str(self) -> str:
        base  = self.callsign.upper().split("-")[0]
        filt  = f" filter {self.filter}" if self.filter else ""
        return f"user {base} pass {self.passcode} vers IoT-UTS-Bridge 1.0{filt}\r\n"

    def _connect_once(self) -> bool:
        try:
            sock = socket.create_connection((self.host, self.port),
                                            timeout=APRSIS_TIMEOUT)
            # Baca banner
            banner = sock.recv(512).decode(errors="replace").strip()
            log.info("APRS-IS banner: %s", banner)
            # Login
            sock.sendall(self._login_str().encode())
            resp = sock.recv(512).decode(errors="replace").strip()
            log.info("APRS-IS login resp: %s", resp)
            if "unverified" in resp.lower():
                log.warning("⚠️  APRS-IS: login tidak terverifikasi (passcode salah?)")
            else:
                log.info("✅ APRS-IS: login berhasil sebagai %s", self.callsign)
            self._sock = sock
            return True
        except OSError as e:
            log.error("❌ APRS-IS koneksi gagal: %s", e)
            self._sock = None
            return False

    def _keepalive_loop(self):
        """Kirim komentar # setiap 60 detik agar server tidak menutup koneksi."""
        while self._alive:
            time.sleep(60)
            if self._alive:
                self._raw_send(f"# keepalive {self.callsign}\r\n")

    def _raw_send(self, data: str) -> bool:
        with self._lock:
            if self._sock is None:
                return False
            try:
                self._sock.sendall(data.encode())
                return True
            except OSError as e:
                log.warning("APRS-IS send error: %s — reconnecting…", e)
                self._sock = None
                return False

    # ── Public API ──────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        ok = self._connect_once()
        if ok:
            self._alive = True
            self._thread = threading.Thread(
                target=self._keepalive_loop, daemon=True, name="aprsis-keepalive"
            )
            self._thread.start()
        return ok

    def send_packet(self, packet: str) -> bool:
        """Kirim satu paket APRS ke server APRS-IS."""
        # Auto-reconnect jika koneksi putus
        if self._sock is None:
            log.info("APRS-IS: mencoba reconnect…")
            self._connect_once()

        ok = self._raw_send(packet + "\r\n")
        if ok:
            log.info("🌐 APRS-IS ← %s", packet)
        return ok

    def close(self):
        self._alive = False
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
        log.info("APRS-IS koneksi ditutup.")


# ─── Bridge Core ─────────────────────────────────────────────────────────────────

class Bridge:
    def __init__(self, callsign: str, lat: float, lon: float,
                 use_aprsis: bool, passcode: int):
        self.callsign = callsign.upper()
        self.lat      = lat
        self.lon      = lon
        self.seq      = 0

        self.aprsis: APRSISClient | None = None
        if use_aprsis:
            self.aprsis = APRSISClient(
                callsign=self.callsign,
                passcode=passcode,
                filter_str=f"r/{lat:.4f}/{lon:.4f}/100",
            )

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=CLIENT_ID,
        )
        self._client.reconnect_delay_set(min_delay=2, max_delay=30)
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message    = self._on_message

    # ── MQTT Callbacks ──────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("✅ MQTT terhubung ke %s:%s", BROKER_HOST, BROKER_PORT)
            client.subscribe(TOPIC, qos=1)
            log.info("   Subscribe topic: %s", TOPIC)
        else:
            log.error("❌ MQTT ditolak. Kode: %s", reason_code)
            # "Not authorized" → biasanya Client ID konflik di broker publik.
            # Hentikan reconnect otomatis, biarkan loop reconnect manual di bawah.
            client.loop_stop()

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        if reason_code != 0:
            log.warning("⚠️  MQTT terputus (kode %s). Auto-reconnect…", reason_code)

    def _on_message(self, client, userdata, msg):
        # ── Decode payload ───────────────────────────────────────────────────
        try:
            payload = json.loads(msg.payload.decode())
            temp_c  = float(payload["value"])
        except (json.JSONDecodeError, KeyError, ValueError):
            # Abaikan pesan dari publisher lain yang formatnya berbeda
            log.debug("Abaikan payload non-JSON: %s", msg.payload[:60])
            return

        log.info("📥 MQTT '%s': suhu CPU = %.2f °C", msg.topic, temp_c)

        # ── Kirim definisi setiap 20 paket data ─────────────────────────────
        if self.seq % 20 == 0:
            for pkt in definition_packets(self.callsign):
                print(f"[APRS-DEF]  {pkt}")
                log_packet(pkt)
                if self.aprsis:
                    self.aprsis.send_packet(pkt)

        # ── Encode data packet ───────────────────────────────────────────────
        pkt = data_packet(self.callsign, self.seq, temp_c)
        print(f"[APRS-DATA] {pkt}")
        log_packet(pkt)
        if self.aprsis:
            self.aprsis.send_packet(pkt)

        # ── Position beacon (setiap 10 paket) ───────────────────────────────
        if self.seq % 10 == 0:
            pos = position_beacon(self.callsign, self.lat, self.lon, temp_c)
            print(f"[APRS-POS]  {pos}")
            log_packet(pos)
            if self.aprsis:
                self.aprsis.send_packet(pos)

        # ── Update wxnow.txt untuk Dire Wolf ─────────────────────────────────
        write_wxnow(temp_c)

        self.seq += 1

    # ── Run ─────────────────────────────────────────────────────────────────────

    def run(self):
        log.info("══════════════════════════════════════════════════════════")
        log.info("  APRS–Dire Wolf Bridge  |  UTS IoT")
        log.info("  Callsign  : %s", self.callsign)
        log.info("  Lokasi    : lat=%.4f  lon=%.4f (Surabaya)", self.lat, self.lon)
        log.info("  MQTT      : %s:%s  →  topic: %s", BROKER_HOST, BROKER_PORT, TOPIC)
        log.info("  Client ID : %s", CLIENT_ID)
        log.info("  APRS-IS   : %s", "AKTIF → " + APRSIS_HOST if self.aprsis else "NONAKTIF (--no-aprsis)")
        if self.aprsis:
            log.info("  aprs.fi   : https://aprs.fi/#!call=%s", self.callsign)
        log.info("  Log file  : %s", os.path.abspath(APRS_LOGFILE))
        log.info("  wxnow     : %s", os.path.abspath(WXNOW_FILE))
        log.info("══════════════════════════════════════════════════════════")

        # Koneksi ke APRS-IS
        if self.aprsis:
            log.info("Menghubungkan ke APRS-IS %s:%s …", APRSIS_HOST, APRSIS_PORT)
            connected = self.aprsis.connect()
            if not connected:
                log.warning("APRS-IS gagal. Lanjut tanpa forward ke aprs.fi.")

        # Koneksi ke MQTT — connect() sinkron agar error terdeteksi sebelum loop.
        # Client ID sudah unik (random suffix) sehingga tidak konflik dengan sesi lama.
        log.info("Menghubungkan ke MQTT %s:%s (client_id=%s) …",
                 BROKER_HOST, BROKER_PORT, CLIENT_ID)
        try:
            self._client.connect(BROKER_HOST, BROKER_PORT, KEEPALIVE)
        except OSError as e:
            log.error("Gagal terhubung ke MQTT: %s", e)

        self._client.loop_start()

        log.info("Bridge aktif. Tekan Ctrl+C untuk berhenti.")
        log.info("")
        if self.aprsis:
            log.info("  ➤ Pantau di: https://aprs.fi/#!call=%s", self.callsign)
            log.info("  ➤ Telemetry : https://aprs.fi/telemetry/a/%s", self.callsign)
            log.info("")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Dihentikan oleh pengguna.")
        finally:
            self._client.loop_stop()
            self._client.disconnect()
            if self.aprsis:
                self.aprsis.close()
            log.info("Bridge ditutup.")


# ─── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="MQTT raspberry_temperature → APRS Telemetry → APRS-IS / aprs.fi"
    )
    ap.add_argument(
        "--callsign", default="YD3ADB-3",
        help="Callsign APRS (default: YD3ADB-3)"
    )
    ap.add_argument(
        "--lat", type=float, default=-7.2575,
        help="Latitude lokasi (desimal, - = Selatan). Default: -7.2575 (Surabaya)"
    )
    ap.add_argument(
        "--lon", type=float, default=112.7521,
        help="Longitude lokasi. Default: 112.7521 (Surabaya)"
    )
    ap.add_argument(
        "--passcode", type=int, default=APRSIS_PASSCODE,
        help=f"APRS-IS passcode (default: {APRSIS_PASSCODE} untuk YD3ADB)"
    )
    ap.add_argument(
        "--no-aprsis", action="store_true",
        help="Nonaktifkan forward ke APRS-IS (mode offline/demo saja)"
    )
    args = ap.parse_args()

    bridge = Bridge(
        callsign=args.callsign,
        lat=args.lat,
        lon=args.lon,
        use_aprsis=not args.no_aprsis,
        passcode=args.passcode,
    )
    bridge.run()


if __name__ == "__main__":
    main()
