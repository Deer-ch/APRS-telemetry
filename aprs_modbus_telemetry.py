#!/usr/bin/env python3
"""
Modbus sensor -> APRS telemetry packets.

Reads temperature and humidity from the lab Modbus sensor (same registers and
config.json as the Modbus -> MQTT script), encodes them as APRS telemetry
packets, prints them, and optionally publishes the packet text to MQTT.

  python aprs_modbus_telemetry.py --simulate     # no hardware needed
  python aprs_modbus_telemetry.py                # real sensor (config.json)
  python aprs_modbus_telemetry.py --mqtt         # also publish to MQTT

Needs for real mode:  pip install pymodbus paho-mqtt
"""

import argparse
import json
import logging
import os
import random
import time

CONFIG_FILE = "config.json"

DEFAULT_CONFIG = {
    "modbus": {
        "host": "10.10.0.100",
        "port": 502,
        "device_id": 1,
        "register_address": 0,
        "register_count": 2,
        "timeout_seconds": 3,
        "scale": 10,
    },
    "mqtt": {
        "host": "broker.emqx.io",
        "port": 1883,
        "keepalive_seconds": 60,
        "topic": "raspberry_temperature",
        "qos": 1,
        "client_id": "modbus-aprs-sensor",
    },
    "aprs": {
        "callsign": "YD3ADB-3",
        "mqtt_topic": "aprs/telemetry",
        "definitions_every": 20,       # resend PARM/UNIT/EQNS/BITS every N data packets
        "temp_alarm": 35.0,            # digital bit 1 = temperature above this
        "humidity_alarm": 80.0,        # digital bit 2 = humidity above this
    },
    "poll_interval_seconds": 5,
    "retry_interval_seconds": 5,
}

# Analog channels. Real value = a*x^2 + b*x + c, x = raw byte 0..255.
# Temp: 0.5 C steps, range -20..107.5 C.  Humidity: 0.4 % steps, range 0..102 %.
# Channels 3-5 are unused (APRS always carries 5 analog slots).
CHANNELS = [
    ("Temp", "C", 0, 0.5, -20),
    ("Hum",  "%", 0, 0.4, 0),
    ("-",    "-", 0, 1, 0),
    ("-",    "-", 0, 1, 0),
    ("-",    "-", 0, 1, 0),
]
DIGITAL = [("HiTmp", "yes"), ("HiHum", "yes"), ("B3", "b3"), ("B4", "b4"),
           ("B5", "b5"), ("B6", "b6"), ("B7", "b7"), ("B8", "b8")]

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("aprs-modbus")


# ------------------------------------------------------------------ config

def load_config():
    if not os.path.exists(CONFIG_FILE):
        log.warning("config.json not found, using built-in example settings.")
        return DEFAULT_CONFIG
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # An existing config.json from the MQTT script has no "aprs" section: add defaults.
    cfg.setdefault("aprs", DEFAULT_CONFIG["aprs"])
    for key, value in DEFAULT_CONFIG["aprs"].items():
        cfg["aprs"].setdefault(key, value)
    return cfg


# ------------------------------------------------------------ APRS encoding

def fmt(x):
    return f"{x:g}"


def definition_packets(call):
    """PARM / UNIT / EQNS / BITS: tell the receiver how to read the data."""
    head = f"{call}>APRS,TCPIP*::{call:<9}:"
    parm = ",".join([c[0] for c in CHANNELS] + [d[0] for d in DIGITAL])
    unit = ",".join([c[1] for c in CHANNELS] + [d[1] for d in DIGITAL])
    eqns = ",".join(f"{fmt(c[2])},{fmt(c[3])},{fmt(c[4])}" for c in CHANNELS)
    return [
        f"{head}PARM.{parm}",
        f"{head}UNIT.{unit}",
        f"{head}EQNS.{eqns}",
        f"{head}BITS.11111111,Lab sensor",
    ]


def to_raw(value, b, c):
    return max(0, min(255, round((value - c) / b)))


def encode_data(call, seq, temp, hum, bits):
    raws = [
        to_raw(temp, CHANNELS[0][3], CHANNELS[0][4]),
        to_raw(hum, CHANNELS[1][3], CHANNELS[1][4]),
        0, 0, 0,
    ]
    bit_str = "".join("1" if b else "0" for b in bits)
    return f"{call}>APRS,TCPIP*:T#{seq:03d}," + ",".join(f"{r:03d}" for r in raws) + f",{bit_str}"


def decode_data(line):
    """Packet text -> (seq, temp, humidity, bits). Used as a self-check."""
    parts = line.split(":", 1)[1][2:].split(",")
    raws = [int(x) for x in parts[1:6]]
    vals = [a * x * x + b * x + c for x, (_, _, a, b, c) in zip(raws, CHANNELS)]
    return int(parts[0]), vals[0], vals[1], parts[6]


# ----------------------------------------------------------------- sensors

def read_registers(client, address, count, device_id):
    try:
        return client.read_holding_registers(address=address, count=count, device_id=device_id)
    except TypeError:
        # Older pymodbus versions use "slave" instead of "device_id".
        return client.read_holding_registers(address=address, count=count, slave=device_id)


def make_modbus(modbus_cfg):
    from pymodbus.client import ModbusTcpClient  # imported here so --simulate needs no install
    return ModbusTcpClient(
        host=modbus_cfg["host"],
        port=int(modbus_cfg["port"]),
        timeout=float(modbus_cfg.get("timeout_seconds", 3)),
    )


def read_real(sensor, modbus_cfg):
    """Returns (temp, humidity). Raises ConnectionError on any Modbus problem."""
    if not sensor.connected:
        log.info("Connecting to Modbus sensor %s:%s", modbus_cfg["host"], modbus_cfg["port"])
        if not sensor.connect():
            raise ConnectionError("Modbus sensor unreachable")
    result = read_registers(
        sensor,
        int(modbus_cfg["register_address"]),
        int(modbus_cfg["register_count"]),
        int(modbus_cfg["device_id"]),
    )
    if result.isError():
        sensor.close()
        raise ConnectionError(f"Modbus read failed: {result}")
    regs = result.registers
    if len(regs) < 2:
        raise ConnectionError("Incomplete sensor data: need two registers")
    scale = float(modbus_cfg.get("scale", 10))
    # Register 0 = humidity (0.1 %RH), register 1 = temperature (0.1 C)
    return regs[1] / scale, regs[0] / scale


class Simulator:
    """Fake sensor for testing without hardware."""

    def __init__(self):
        self.temp, self.hum = 28.0, 65.0

    def read(self):
        self.temp = min(40.0, max(20.0, self.temp + random.uniform(-0.6, 0.6)))
        self.hum = min(95.0, max(40.0, self.hum + random.uniform(-1.5, 1.5)))
        return self.temp, self.hum


# -------------------------------------------------------------------- MQTT

def make_mqtt(mqtt_cfg):
    import paho.mqtt.client as mqtt
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=mqtt_cfg.get("client_id", "modbus-aprs-sensor"),
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(mqtt_cfg["host"], int(mqtt_cfg["port"]), int(mqtt_cfg.get("keepalive_seconds", 60)))
    client.loop_start()
    return client


def publish(client, topic, text, qos):
    if client is None:
        return
    if client.is_connected():
        client.publish(topic, payload=text, qos=qos)
    else:
        log.warning("MQTT not connected yet, packet not published.")


# -------------------------------------------------------------------- MAIN

def main():
    ap = argparse.ArgumentParser(description="Modbus sensor -> APRS telemetry")
    ap.add_argument("--simulate", action="store_true", help="use fake sensor values")
    ap.add_argument("--mqtt", action="store_true", help="also publish packets to MQTT")
    ap.add_argument("--count", type=int, default=0, help="stop after N packets (0 = forever)")
    ap.add_argument("--interval", type=float, default=None, help="seconds between readings")
    args = ap.parse_args()

    cfg = load_config()
    aprs = cfg["aprs"]
    call = aprs["callsign"]
    poll = args.interval if args.interval is not None else max(1, int(cfg.get("poll_interval_seconds", 5)))
    retry = max(1, int(cfg.get("retry_interval_seconds", 5)))

    sim = Simulator() if args.simulate else None
    sensor = None if args.simulate else make_modbus(cfg["modbus"])
    mqtt_client = make_mqtt(cfg["mqtt"]) if args.mqtt else None
    qos = int(cfg["mqtt"].get("qos", 1))

    seq = sent = 0
    try:
        while args.count == 0 or sent < args.count:
            try:
                temp, hum = sim.read() if sim else read_real(sensor, cfg["modbus"])
            except (OSError, TimeoutError, ConnectionError) as exc:
                log.error("%s. Retrying in %s s.", exc, retry)
                time.sleep(retry)
                continue

            if sent % int(aprs["definitions_every"]) == 0:
                for pkt in definition_packets(call):
                    print(pkt)
                    publish(mqtt_client, aprs["mqtt_topic"], pkt, qos)

            bits = [temp > float(aprs["temp_alarm"]), hum > float(aprs["humidity_alarm"]), 0, 0, 0, 0, 0, 0]
            pkt = encode_data(call, seq, temp, hum, bits)

            # Self-check: decoded values must match the readings within rounding error.
            d_seq, d_temp, d_hum, d_bits = decode_data(pkt)
            assert abs(d_temp - temp) <= CHANNELS[0][3] / 2 + 1e-9, "temperature round-trip mismatch"
            assert abs(d_hum - hum) <= CHANNELS[1][3] / 2 + 1e-9, "humidity round-trip mismatch"

            print(pkt)
            log.info("#%03d  temp=%.1f C  humidity=%.1f %%RH  bits=%s", d_seq, d_temp, d_hum, d_bits)
            publish(mqtt_client, aprs["mqtt_topic"], pkt, qos)

            seq = (seq + 1) % 1000
            sent += 1
            if poll and (args.count == 0 or sent < args.count):
                time.sleep(poll)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        if sensor:
            sensor.close()
        if mqtt_client:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()


if __name__ == "__main__":
    main()
