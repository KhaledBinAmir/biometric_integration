<div align="center">

# Biometric Integration for Frappe / ERPNext

**Real-time biometric attendance — ZKTeco & EBKN — wired directly into ERPNext. No middleware. No polling. No separate service.**

[![Frappe v15](https://img.shields.io/badge/Frappe-v15-blue?style=flat-square)](https://frappeframework.com)
[![ERPNext v15](https://img.shields.io/badge/ERPNext-v15-brightgreen?style=flat-square)](https://erpnext.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

</div>

---

## Overview

The **Biometric Integration** app connects ZKTeco and EBKN biometric attendance devices directly to your ERPNext instance. Punches create Employee Checkin records in real time. Enrollment is captured automatically. Employee status changes (left, inactive) propagate delete commands back to every assigned device — all without a single line of manual intervention after initial setup.

---

## Device Support

| Brand | Protocol | Attendance | Enrollment Sync | Commands | Status |
|-------|----------|:----------:|:---------------:|:--------:|--------|
| **ZKTeco** | ADMS Push (`iclock`) | ✅ | ✅ | ✅ | **Stable** |
| **EBKN** | FkWeb Push | ✅ | ✅ | ✅ | **Stable** |
| Suprema | — | — | — | — | Planned |

> ZKTeco devices must support the **ADMS / Cloud Server** push mode. Most mid-range and enterprise ZKTeco models support this.

---

## How It Works

Device traffic is handled via Frappe's `page_renderer` hook. Requests to `/iclock/*` (ZKTeco) and `/ebkn` (EBKN) are intercepted at the WSGI layer before any template or page lookup — no Nginx rewrite rules, no `X-Original-Request-URI` header tricks, no separate listener process.

```
┌─────────────────────┐
│   Biometric Device  │
│  (ZKTeco / EBKN)    │
└────────┬────────────┘
         │  HTTP push (real-time)
         ▼
┌─────────────────────────────────────┐
│      Frappe WSGI  (page_renderer)   │
│                                     │
│  /iclock/*  ──►  ZKTecoAdapter      │
│  /ebkn      ──►  EBKNAdapter        │
└────────┬──────────────┬─────────────┘
         │              │
         ▼              ▼
  Employee Checkin   Attendance Device
  (ERPNext HRMS)     Command Queue
```

**Frappe Cloud / HTTPS-only servers:** Biometric devices speak plain HTTP. If your server is HTTPS-only, the app generates a ready-to-use Nginx server block you paste into a local proxy on your network — or on self-hosted servers it injects and activates the config automatically from the Settings UI.

---

## Features

### Attendance
- Punch data pushed by devices is immediately converted to **Employee Checkin** records in ERPNext
- Supports both batch upload (`ATTLOG`) and real-time push (`rtlog` / `realtime_glog`)
- Duplicate-safe: ERPNext's own deduplication logic applies

### Enrollment & User Sync
- When a user enrolls on any device, an **Attendance Device User** record is created automatically, capturing the PIN and biometric template
- Templates are propagated to all other devices the user is assigned to — enroll once, sync everywhere
- Matched to an ERPNext Employee automatically via the `attendance_device_id` field (standard ERPNext HRMS field)

### Employee Lifecycle
- **Employee goes Inactive / Left** → `Delete User` commands are queued for every assigned device
- **Employee reactivated** → `Enroll User` commands re-push the stored template back to all devices
- **Employee name changes** → `Update User` synced to ZKTeco devices

### Command Queue
Commands are delivered to devices the next time they poll (`/iclock/getrequest` or `receive_cmd`):

| Command | Purpose |
|---------|---------|
| Enroll User | Push stored biometric template to device |
| Delete User | Remove user from device |
| Get Enroll Data | Fetch template from device (EBKN) |
| Update User | Sync updated name/info to device (ZKTeco) |

### Device Discovery
Unknown devices that connect are **always logged** — even with device logging disabled — so you can find the serial number and register the device without hunting through server logs.

### HTTP Listener
- **Self-hosted:** Enable from the Settings UI. The app injects an Nginx server block and reloads Nginx automatically.
- **Frappe Cloud / manual:** A ready-to-paste Nginx config block is generated in Settings. Drop it into a local Nginx on your network.
- When the listener is active, the Settings page shows all available addresses: HTTPS URL, plain-HTTP URL, and detected public IP — ready to copy into device config.

### Audit Log
An optional **Attendance Device Log** captures all device communication — attendance events, commands, enrollment, handshakes, and errors — with raw data. Controlled by a single checkbox in Settings; disabled by default to avoid DB bloat. Auto-purges after 30 days.

---

## Installation

```bash
cd /path/to/frappe-bench

# Get the app (v2 branch)
bench get-app https://github.com/KhaledBinAmir/biometric_integration --branch v2

# Install on your site
bench --site your-site.com install-app biometric_integration
bench --site your-site.com migrate
```

---

## Setup

### 1. Open Attendance Integration Settings

Navigate to **Biometric Integration → Attendance Integration Settings**.

The **Device Endpoint URLs** section shows exactly what to enter on the physical device. When the HTTP listener is active, plain-HTTP addresses (including the server's public IP) appear automatically.

### 2. Register each device

Go to **Biometric Integration → Attendance Device** → New:

| Field | Description |
|-------|-------------|
| **Serial** | Must match the Serial Number shown in the device's ADMS / network settings |
| **Device Name** | Friendly label |
| **Brand** | ZKTeco or EBKN |

### 3. Configure the physical device

| Brand | Where to configure | What to enter |
|-------|--------------------|---------------|
| ZKTeco | ADMS / Cloud Server settings | Hostname only (e.g. `yoursite.com`) — firmware appends `/iclock/*` automatically |
| EBKN | Push server URL | Full URL with path (e.g. `https://yoursite.com/ebkn`) |

Once the device connects, `Last Contact` updates on the device record and the device is ready.

### 4. Map employees

When a user enrolls on a device, an **Attendance Device User** record is created automatically. Open it and link it to the matching ERPNext Employee.

**Tip:** Pre-fill the **Attendance Device ID** field on each Employee record (in ERPNext HR → Employee) with the device PIN. The app will link them automatically on first contact — no manual linking needed.

---

## Doctypes

| Doctype | Purpose |
|---------|---------|
| **Attendance Device** | One record per physical device. Tracks last contact, sync watermark, and pending commands. |
| **Attendance Device User** | Maps a device PIN ↔ ERPNext Employee. Stores biometric templates for ZKTeco and EBKN. |
| **Attendance Device Link** | Child table on Attendance Device User — lists which devices a user is enrolled on. |
| **Attendance Device Command** | Pending command queue. Each record is delivered to the device and marked Success / Failed. |
| **Attendance Device Log** | Audit log of all device communication. Unknown-device events always logged. |
| **Attendance Integration Settings** | Global configuration (Single doctype). |

---

## Discovering Unknown Devices

Any device that contacts the server without a matching **Attendance Device** record is silently accepted (returns `OK` so the device doesn't disconnect) and its serial is written to **Attendance Device Log** with log type `Error`.

To find it: open **Attendance Device Log**, filter **Device** = *(blank)*. The **Serial / ID** column shows the raw serial reported by the device. Copy it, create the matching Attendance Device, and the device is registered.

---

## Requirements

- Frappe **v15** / ERPNext **v15** or later
- Redis (standard Frappe requirement — used for EBKN multi-block buffering)
- For the HTTP Listener: Nginx managed by bench (self-hosted only)

---

## License

MIT — see [LICENSE](LICENSE).

---

## Contact & Support

Maintained by **Khaled Bin Amir**.
For setup help, device compatibility questions, or to sponsor new features: [t.me/khaledbinamir](https://t.me/khaledbinamir)
