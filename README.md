<div align="center">

# Biometric Integration for Frappe / ERPNext

**Real-time biometric attendance — ZKTeco & EBKN — wired directly into ERPNext. No middleware. No polling service. No Nginx rewrite rules.**

[![Frappe v15](https://img.shields.io/badge/Frappe-v15-blue?style=flat-square)](https://frappeframework.com)
[![ERPNext v15](https://img.shields.io/badge/ERPNext-v15-brightgreen?style=flat-square)](https://erpnext.com)
[![HRMS](https://img.shields.io/badge/HRMS-required-orange?style=flat-square)](https://github.com/frappe/hrms)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)

</div>

---

## Overview

**Biometric Integration** connects ZKTeco and EBKN biometric attendance devices directly to ERPNext. Punches become **Employee Checkin** records in real time, enrollment templates are captured and can be pushed to other devices, and employee lifecycle changes propagate to every assigned device — all through Frappe's request layer, with no separate service to run.

It works unchanged on **Frappe Cloud** and self-hosted servers because device traffic is intercepted inside Frappe itself (see [How It Works](#how-it-works)).

---

## Device Support

| Brand | Protocol | Attendance | Enrollment | Commands | Status |
|-------|----------|:----------:|:----------:|:--------:|--------|
| **ZKTeco** | ADMS Push (`/iclock/*`) | ✅ | ✅ | ✅ | **Stable** |
| **EBKN** | BS_FkWeb Push (`/ebkn`) | ✅ | ✅ | ✅ | **Stable** |
| Suprema | — | — | — | — | Planned |

> ZKTeco devices must support **ADMS / Cloud Server** push mode (most mid-range and enterprise models do).

### A note on ZKTeco firmware dialects

ZKTeco firmware comes in two protocol generations, and this app supports both:

- **Classic dialect** (the widely-deployed default): fingerprint templates are pushed with `DATA UPDATE FINGERTMP` and the profile is read with `DATA QUERY USERINFO` / `FPTMP`. This is what the app uses for **fingerprints**, because it works across old *and* new firmware.
- **Unified dialect** (newer firmware): the `biodata` / `templatev10` tables. Used only for **face / palm** modalities, which exist solely on newer devices.

Practical consequences you may see in the Attendance Device Log:

- On classic firmware, template **queries** (`DATA QUERY tablename=biodata,…`) return `Return=-1004` — the device doesn't implement them. That's expected: **fingerprint templates arrive by device push** (the device uploads them via `OPERLOG` when a finger is enrolled *on* the device), not by query.
- Pushing a **unified** `DATA UPDATE biodata` line to classic firmware returns `Return=-1`; the app avoids this by using `FINGERTMP` for fingerprints.

---

## How It Works

Device traffic is handled via Frappe's `page_renderer` hook. Requests to `/iclock/*` (ZKTeco) and `/ebkn` (EBKN) are intercepted at the WSGI layer **before** any template or page lookup — no Nginx rewrites, no `X-Original-Request-URI` tricks, no separate listener process. This is why it runs on Frappe Cloud with zero infrastructure changes.

```
┌─────────────────────┐
│   Biometric Device  │
│   (ZKTeco / EBKN)   │
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

**Devices speak plain HTTP; your site is HTTPS.** Two ways to bridge that:

- **Self-hosted:** enable the HTTP listener from Settings — the app writes and reloads an Nginx server block for you.
- **Frappe Cloud / manual:** Settings generates a ready-to-paste Nginx block; drop it into a small Nginx on your LAN that forwards to your HTTPS site.

---

## Installation

```bash
cd /path/to/frappe-bench

# HRMS is required (provides Employee / Employee Checkin)
bench get-app hrms
bench get-app https://github.com/KhaledBinAmir/biometric_integration --branch develop

bench --site your-site.com install-app biometric_integration
bench --site your-site.com migrate
```

> **Branch note:** active development and all current fixes live on `develop`. See [Migrating from v1](#migrating-from-v1) if you are on the old `main`/v1 architecture.

**Requirements:** Frappe **v15** / ERPNext **v15**, **HRMS**, Redis (used for EBKN multi-block buffering), Python ≥ 3.10. The HTTP listener additionally needs bench-managed Nginx (self-hosted only).

---

## Setup

### 1. Open Attendance Integration Settings

**Biometric Integration → Attendance Integration Settings.** The **Device Endpoint URLs** section shows exactly what to enter on the device (plain-HTTP addresses and detected public IP appear when the listener is active).

<img width="927" height="858" alt="Settings" src="https://github.com/user-attachments/assets/024b2096-f48e-4e6a-8cab-d85c4a28f5fe" />

### 2. Register each device

**Biometric Integration → Attendance Device → New.**

| Field | Description |
|-------|-------------|
| **Serial** | Must match the serial / device ID reported by the device's ADMS settings — this is the only identity the device sends. |
| **Device Name** | Friendly label. |
| **Brand** | ZKTeco or EBKN. |
| **Company / Project / Branch** | Optional assignment. |
| **Device Timezone** | The device's wall-clock zone. Punch timestamps carry no timezone; set this so they convert correctly (leave blank to treat as site time). |
| **Disable Employee Sync** | When checked, no Enroll/Delete/Update User commands are queued for this device (it still receives punches). |

Capability fields (**Firmware Version**, **MAC Address**, **Supported Biometrics**, **Max Users**, **FP Algorithm Version**) are populated automatically from what the device reports.

<img width="927" height="859" alt="Attendance Device" src="https://github.com/user-attachments/assets/b7159bd2-76ad-4638-a6c4-429cde79b1e8" />

### 3. Configure the physical device

| Brand | Where | What to enter |
|-------|-------|---------------|
| ZKTeco | ADMS / Cloud Server settings | Hostname only (e.g. `yoursite.com`) — firmware appends `/iclock/*`. |
| EBKN | Push server URL | Full URL with path (e.g. `https://yoursite.com/ebkn`). |

<img width="972" height="376" alt="Device config" src="https://github.com/user-attachments/assets/68024b92-8bbc-44dd-8b20-2396520760b7" />

Once the device connects, `Last Contact` updates and it's ready.

### 4. Map employees

When a PIN first appears, an **Attendance Device User** is created automatically. Link it to the matching ERPNext Employee — or, better, pre-fill each Employee's **Attendance Device ID** field with their device PIN and the app links them automatically on first contact.

To put employees on devices from ERPNext instead, set **Employee Sync Mode** in Attendance Integration Settings:

| Mode | An Active employee with an Attendance Device ID is put on |
|------|------|
| **Disabled** (default) | nothing automatically; keep the device list on the Attendance Device User by hand |
| **Company Devices** | every enabled device owned by the employee's company |
| **All Devices** | every enabled device |

Sync runs on every Employee save, daily, when a device is registered or re-enabled, and from the **Sync Employees** button (which previews before it changes anything). It only ever adds people to devices: an extra device on someone's Attendance Device User (a person who also clocks at another company's device) is kept, and people leave devices only by becoming Left or Inactive. Devices with *Disable Employee Sync* are skipped.

---

## Attendance Flow

1. Device pushes a punch (`ATTLOG` batch or `rtlog` / `realtime_glog` real-time).
2. The PIN is mapped to an Employee via `Employee.attendance_device_id` (standard HRMS field, indexed).
3. An **Employee Checkin** is created (idempotent — duplicate timestamps for the same employee are ignored).

The device IN/OUT flag is intentionally **not** recorded — most fingerprint terminals stamp a generic value on every scan, so it is unreliable. Attendance is derived from the punches themselves (see **Attendance Monitor** below).

Unmapped PINs are skipped by default (the punch is logged, no checkin is created). Set **Do Not Skip Unknown Employee Checkin** in Settings to create checkins for unmapped PINs anyway.

---

## Attendance Monitor

A desk page (**Attendance Monitor**, roles: *Site Supervisor / HR User / HR Manager / System Manager*) that shows, per employee per day, an interactive timeline of punches with net time on site:

- **Timeline per worker** — green work segments + amber break segments, each labelled with its duration; IN/OUT trim-handles; a "now" line on today; a hatched grey band for an unclassifiable stretch (odd punch count).
- **Net-time model** — punches are paired in chronological order (1st = IN, 2nd = OUT, 3rd = IN …); work = sum of IN→OUT pairs, break = the gaps between them. Repeat scans within a short window collapse (double-taps). An odd count flags the day for review instead of miscomputing.
- **Absent / leave / holiday** — employees with no punches show as *no punches*; an approved Leave Application that day shows *On leave*; a company holiday shows a banner.
- **Corrections** — supervisors add a punch (click the bar), move one (drag a handle), or delete one (click → ✕). Each correction runs through the validated doc API and then re-syncs Attendance via the **Attendance Processor** (below).
- **View-only mode** — set **Allow Check-in Corrections in Monitor** off to make the page strictly read-only (no add/move/delete), for sites that don't permit editing raw punches.

### Attendance Processor (optional)

Attendance can be (re)built from raw check-ins by a **Server Script you nominate** in Settings → *Attendance Processor*. It runs on every check-in insert and after every Monitor correction (add/edit/delete), so corrections stay in sync — the script receives a `doc` (Employee Checkin) carrying the employee + the day to rebuild.

**This field is optional.** Leave it **blank** to rely on Frappe's native **Shift Type** auto-attendance (or no attendance at all). Because it's a Server Script, the attendance logic is swappable per site without changing app code. On edit/delete the Monitor first releases the day's existing Attendance (so the punch isn't locked), performs the change, then invokes the processor to rebuild — HR-set statuses (leave / holiday / half-day) are preserved.

---

## Enrollment & User Sync

- Biometric templates are stored as **private File attachments** on the Attendance Device User (ZKTeco = a versioned JSON accumulating fingers/face/card; EBKN = the raw blob).
- **Propagation is command-queue based and modality-dependent:** a fingerprint is pushed to another device via the classic `FINGERTMP` command; face/palm templates require unified-`biodata` firmware on the target. Enrollment is *not* magically mirrored everywhere — it's queued per assigned device.
- **`Allow In All Devices`** on the Attendance Device User syncs that user to every non-disabled device instead of only the ones in its device list.
- **Employee lifecycle:** Inactive/Left → `Delete User` queued for assigned devices; reactivated → `Enroll User` re-pushes the stored template (`Update User` when there is none); name change → `Update User`. Which devices a person is put on in the first place follows **Employee Sync Mode** (see [Map employees](#4-map-employees)).

---

## Command Queue

Commands are delivered when the device next polls (`/iclock/getrequest` or EBKN `receive_cmd`) and marked Success/Failed from the device's result. All command traffic is gated on device registration and each result is checked against the owning device.

| Command | Purpose | Brands |
|---------|---------|--------|
| **Enroll User** | Push stored template(s) to the device | ZKTeco, EBKN |
| **Delete User** | Remove the user from the device | ZKTeco, EBKN |
| **Update User** | Sync name/profile (no biometrics) | ZKTeco, EBKN |
| **Get Enroll Data** | Read the user's profile/templates from the device | ZKTeco, EBKN |
| **Sync User List** | Pull the device's full user roster and create stubs | ZKTeco, EBKN |
| **Re-pull Attendance** | Ask the device to re-upload stored logs for a date range (recover missed punches) | ZKTeco |
| **Restart Device** | Reboot | ZKTeco, EBKN |
| **Unlock Door** | Trigger the door relay | ZKTeco, EBKN |
| **Set Device Time** | Sync the clock (EBKN explicit; ZKTeco auto-syncs) | ZKTeco, EBKN |

Stale commands are auto-failed after a configurable number of attempts / days.

---

## Configuration Reference

**Attendance Integration Settings** (Single):

| Setting | Meaning |
|---------|---------|
| `device_poll_delay` / `device_error_delay` | Poll and error-retry intervals sent to ZKTeco devices. |
| `trans_times` / `trans_interval` | ZKTeco transfer schedule. |
| `maximum_command_attempts` | Attempts before a command is marked Failed. |
| `force_close_after_days` | Age after which pending commands are force-closed. |
| `enable_device_log` | Turn on the full audit log (off by default to avoid DB bloat). |
| `do_not_skip_unknown_employee_checkin` | Create checkins for PINs not yet mapped to an Employee. |
| `push_timezone_to_device` | Push the site/device timezone to ZKTeco devices. |
| `proxy_enabled` / `proxy_port` | HTTP listener (self-hosted Nginx). |

---

## Doctypes

| Doctype | Purpose |
|---------|---------|
| **Attendance Device** | One per physical device — identity, capabilities, assignment, last contact, sync watermark. |
| **Attendance Device User** | PIN ↔ Employee mapping; stores biometric template blobs. |
| **Attendance Device Link** | Child table — which devices a user is enrolled on. |
| **Attendance Device Command** | The command queue. |
| **Attendance Device Log** | Audit log of device traffic (unknown-device events always logged). |
| **Attendance Integration Settings** | Global configuration. |

---

## Troubleshooting

- **Device shows in the log but no checkins appear** → the PIN isn't mapped to an Employee. Set the Employee's *Attendance Device ID*, or enable *Do Not Skip Unknown Employee Checkin*.
- **"Device not registered" in the log** → create an Attendance Device whose Serial exactly matches the device's reported serial (see [Discovering Unknown Devices](#discovering-unknown-devices)).
- **ZKTeco Return codes** (in a command's `device_response`): `0` = success; `-1` = command/format not supported by this firmware; `-1004` = query/table not supported (expected on classic firmware — see [dialects](#a-note-on-zkteco-firmware-dialects)).
- **Command stuck Pending** → the device hasn't polled, isn't registered, or has *Disable Employee Sync* set. Check `Last Contact` and the log.
- **Enrollment push fails on a new model** → fingerprint pushes use `FINGERTMP` (broadest support). If a device rejects it, capture the device's firmware/FP-version and open an issue.

### Discovering Unknown Devices

Any device without a matching Attendance Device record is accepted just enough to log its serial. Open **Attendance Device Log**, filter **Device** = *(blank)*, read the reported serial, and create the device.

---

## Security & Limitations

The ZKTeco ADMS and EBKN protocols are **unauthenticated by design** — the only device identity is a self-reported serial number, which is trivially spoofable, and requests are processed in an elevated (Administrator) server context. This app hardens what it can:

- Device endpoints (`getrequest` / `devicecmd` / `querydata`, and the whole `/ebkn` handler) are **gated on device registration**.
- Device-reported command results are **verified against the owning device** before being applied.
- UI/whitelisted management methods require the **System Manager** role.

But the transport itself cannot be trusted. **Do not expose `/iclock/*` or `/ebkn` to the open internet.** Restrict them to the devices' network via a source-IP allowlist, VPN, or a dedicated VLAN, and never expose the plain-HTTP listener publicly. ADMS payload encryption is not supported.

---

## Migrating from v1

The v2 architecture (this branch) replaces the v1 renderer/processor design. `bench migrate` runs `patches/v2_0/*` to move settings and device/user data forward. Review your Attendance Integration Settings after migrating. If you are pinned to v1, note that the default `main` branch may still carry v1-era code — use `--branch develop` for the current app.

---

## License

MIT — see [LICENSE](LICENSE).

## Contact & Support

Maintained by **Khaled Bin Amir**. Setup help, device compatibility, or sponsoring features: [t.me/khaledbinamir](https://t.me/khaledbinamir)
