# DOKUMEN PENGUMPULAN TUGAS UTS IoT
## Demonstrasi APRS Telemetry & Live Weather/Telemetry Online

* **Mata Kuliah:** Internet of Things (IoT) — Semester 5
* **Topik Tugas:** Pilihan 1 — Raspberry Pi Temperature Telemetry via Dire Wolf & MQTT
* **Callsign Stasiun:** `YD3ADB-3`
* **APRS-IS Passcode:** `23973` *(Verified)*
* **MQTT Broker:** `broker.emqx.io:1883` | **Topic:** `raspberry_temperature`

---

## 📌 BAGIAN 1: PEMENUHAN KRITERIA PENILAIAN

### 1.1 Direwolf dengan Data Weather Station / Telemetry Sudah Live Online di aprs.fi [Bobot: 60 Poin]

Sistem telah terintegrasi dengan jaringan global **APRS-IS (*APRS Internet System*)** Tier-2 (`rotate.aprs2.net:14580`) menggunakan kredensial stasiun resmi **`YD3ADB-3`** dan status login **`VERIFIED`**.

* **Tautan Langsung Peta Lokasi Stasiun:**  
  👉 **[https://aprs.fi/#!call=YD3ADB-3](https://aprs.fi/#!call=YD3ADB-3)**
* **Tautan Langsung Grafik Telemetri Real-Time:**  
  👉 **[https://aprs.fi/telemetry/a/YD3ADB-3](https://aprs.fi/telemetry/a/YD3ADB-3)**

#### Bukti Handshake & Verifikasi Server APRS-IS:
```text
Menghubungkan ke APRS-IS rotate.aprs2.net:14580 …
APRS-IS banner    : # aprsc 2.1.21-gbfc2090
APRS-IS login resp: # logresp YD3ADB verified, server T2DALLAS
✅ Status          : LOGIN BERHASIL (VERIFIED) sebagai YD3ADB-3
```

---

### 1.2 Data Live [Bobot: 40 Poin]

Data telemetri suhu CPU Raspberry Pi dipancarkan secara kontinu (*real-time live stream*) dengan interval 5 detik melalui MQTT dan diteruskan langsung ke APRS.

* **Format Paket Data Telemetri APRS (`T#`):**
  ```text
  YD3ADB-3>APRS,TCPIP*:T#000,113,000,000,000,000,00000000
  YD3ADB-3>APRS,TCPIP*:T#001,113,000,000,000,000,00000000
  ```
* **Format Paket Posisi & Komentar Suhu:**
  ```text
  YD3ADB-3>APRS,TCPIP*:=0715.45S/11245.13E_...RPi CPU Temp=56.5C IoT UTS YD3ADB
  ```
* **Format File Weather Lokal (`wxnow.txt`) untuk Dire Wolf:**
  ```text
  2026 11 02 03 07 41 T=56.43C
  ```
* **Verifikasi Nilai Live Sensor:**
  $$\text{Suhu Dekode} = 0.5 \times \text{Raw} = 0.5 \times 113 = 56.50^\circ\text{C} \quad (\text{Suhu Sensor Aktual: } 56.53^\circ\text{C})$$

---

## 📁 BAGIAN 2: BAHAN PENUNJANG YANG WAJIB DI-UPLOAD

Semua file berikut telah disiapkan di root folder proyek dan siap diunggah ke Edlink (baik sebagai file satuan maupun dalam arsip `.zip`):

| No | Kategori Bahan Penunjang | Nama File | Deskripsi & Fungsi |
|:--:|---|---|---|
| **1** | **Wajib Direwolf Config** | **`direwolf.cfg`** | File konfigurasi TNC Dire Wolf (Callsign `YD3ADB-3`, PBEACON `wxnow.txt`, APRS-IS IGate `23973`). |
| **2** | **Script MQTT (Publisher)** | **`raspberry_pi_temp.py`** | Script pembaca sensor suhu CPU RPi (simulasi termal / real hardware) yang mem-publish ke MQTT broker `broker.emqx.io`. |
| **3** | **Script MQTT (Bridge/APRS)**| **`aprs_direwolf_bridge.py`** | Script subscriber MQTT yang meng-encode data ke format APRS AX.25, menginjeksi ke gateway APRS-IS (`rotate.aprs2.net`), dan menulis `wxnow.txt`. |
| **4** | **Script Modbus (APRS)** | **`aprs_modbus_telemetry.py`** | Script pembaca register Modbus TCP (suhu & humidity) yang dikonversi menjadi paket APRS Telemetry. |
| **5** | **Script Modbus (MQTT)** | **`main.py`** | Script pembacaan Modbus TCP sensor lingkungan laboratorium diteruskan ke broker MQTT. |
| **6** | **Konfigurasi Sistem** | **`config.json`** | Pengaturan parameter global (Modbus host/port, MQTT broker/topic, APRS callsign & passcode). |
| **7** | **Data Weather Direwolf** | **`wxnow.txt`** | File data cuaca/suhu format standar WX yang diperbarui berkala dan dibaca oleh Dire Wolf via `commentcmd`. |
| **8** | **Laporan Lengkap** | **`LAPORAN_UTS.md`** | Dokumen laporan teknis lengkap format Markdown beserta analisis akurasi dekoding dan arsitektur sistem. |

---

## ⚙️ BAGIAN 3: RINGKASAN FILE KONFIGURASI `direwolf.cfg`

Berikut adalah cuplikan konfigurasi utama pada file **`direwolf.cfg`**:

```conf
########################################################
#  UTS IoT – APRS Telemetry Demo (Raspberry Pi Temp)  #
#  Callsign : YD3ADB-3                                 #
#  Broker   : broker.emqx.io:1883                      #
#  Topic    : raspberry_temperature                    #
#  APRS-IS  : rotate.aprs2.net:14580                   #
########################################################

MYCALL YD3ADB-3

# Modem AFSK 1200 baud VHF standard
CHANNEL 0
MODEM 1200

# ── PBEACON (Posisi & Telemetri Suhu dari wxnow.txt) ─────────────
PBEACON delay=0:30 every=10:00 lat=-7.2575 long=112.7521 symbol="_" \
        comment="RPi CPU Temp" commentcmd="type wxnow.txt"

# ── APRS-IS Gateway (Koneksi Internet IGate) ─────────────────────
IGSERVER rotate.aprs2.net
IGLOGIN YD3ADB-3 23973
```

---

## 🚀 BAGIAN 4: PANDUAN PENGUJIAN & DEMONSTRASI LIVE

Bagi Dosen Penguji yang ingin memvalidasi eksekusi program:

### Langkah 1: Jalankan Bridge APRS (Terminal 1)
```powershell
python aprs_direwolf_bridge.py
```
*Hasil:* Terhubung ke APRS-IS Tier-2 dengan status `verified` dan melakukan subscribe ke broker MQTT `broker.emqx.io` pada topik `raspberry_temperature`.

### Langkah 2: Jalankan Sensor Suhu (Terminal 2)
```powershell
python raspberry_pi_temp.py --interval 5
```
*Hasil:* Suhu CPU dipublikasikan ke MQTT dan langsung diteruskan oleh Bridge ke server APRS-IS global.

### Langkah 3: Validasi di Web aprs.fi
Buka peramban (*browser*) dan akses:
* **[https://aprs.fi/#!call=YD3ADB-3](https://aprs.fi/#!call=YD3ADB-3)** untuk melihat titik stasiun pada peta.
* **[https://aprs.fi/telemetry/a/YD3ADB-3](https://aprs.fi/telemetry/a/YD3ADB-3)** untuk melihat grafik nilai suhu telemetri yang ter-update secara berkala.

---
*Pengumpulan Ujian Tengah Semester (UTS) — Internet of Things 2026/2027*  
*Disusun oleh: Almuza Fajar (Callsign: YD3ADB-3)*

