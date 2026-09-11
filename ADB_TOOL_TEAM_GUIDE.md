# ADB Tool - Team User Guide

> **GUI for Android Debug Bridge** | **v1.0+**

---

## 📖 Table of Contents

1. [What is ADB Tool?](#what-is-adb-tool)
2. [How It Works](#how-it-works)
3. [Colors & Status Indicators](#colors--status-indicators)
4. [Seat Connections](#seat-connections)
5. [Port Forward Racks](#port-forward-racks)
6. [Understood Commands](#understood-commands)
7. [Settings & Configuration](#settings--configuration)
8. [Common Workflows](#common-workflows)
9. [Troubleshooting](#troubleshooting)

---

## What is ADB Tool?

**ADB Tool** is a visual interface for managing Android devices remotely. Instead of typing complex commands, simply **click** to connect to test devices, set up port forwards, mirror screens, and more.

> "Think of it as browser bookmarks — but for your Android test devices!"

---

## How It Works

The tool uses companion scripts that handle the heavy lifting:

| Task | Done By |
|------|---------|
| Connect to Android devices (seats) | `/opt/homebrew/bin/seat.sh` |
| Manage port forward racks | `/opt/homebrew/bin/portforwardRack.sh` |
| Track past connections | `~/.cache/adb-seat-ports` & `~/.cache/portforward-racks` |
| Visual click-to-connect UI | ADB Tool GUI (PyQt6 app) |

> These scripts are **enhanced versions** with new commands (`auto-connect`, `history`, `devices`, `status`) that make automation possible.

---

## Colors & Status Indicators

| Color | Meaning |
|-------|---------|
| 🟢 Green | Connected/Active |
| ⚪ Gray | Disconnected/Inactive |
| 🔴 Red (on hover) | Click to disconnect this 
| 🔵 Blue (on hover) | Click to connect this |

---

## seat.sh 2.0

### What Changed from Original

**Before:** Required 2 commands + manual port
```bash
./seat.sh tunnel seat006D user@pdc-dev-ek 5561
./seat.sh connect 5561
```

**Now:** One command
```bash
./seat.sh auto-connect seat006D user@pdc-dev-ek
```

### New v2.0 Commands

| Command | v1 Status | v2.0 Usage |
|---------|-----------|------------|
| `list` | ✅ | List raw adb devices
| `devices` | ❌ | List formatted table with gateway/rack
| `connect 5561` | ✅ | Connect using explicit port
| `auto-connect seat006D user@host` | ❌ | Auto-assign free port & connect
| `disconnect seat006D` | ❌ | Disconnect by seat name (not port)
| `history` | ❌ | List all past connections in cache

---

## portforwardRack.sh 2.0

### New v2.0 Commands

| Command | Sense |
|---------|--------|
| `status` | Is port forwarding active? Which ports?
| `stop` | Clean graceful shutdown of all forwards
| `history` | List all historical racks from cache
| `gateway-rack <head-n> user` | Apply forwarding to a rack server

---

## Cache Files

The tool keeps these auto-populated:

- `~/.cache/adb-seat-ports` — all seats ever connected
- `~/.cache/portforward-racks` — all racks ever touched

↓ Auto-fill dropdowns → one-click reconnection every time

---

## Settings & Configuration

### ADB Tool Settings (GUI)

| Setting | What It Does |
|---------|--------------|
| ADB Path | Configure `adb` executable location |
| Seat Script Path | Path to `seat.sh` |
| PortForward Script Path | Path to `portforwardRack.sh` |
| Theme | Light/Dark/System |

> Default paths are pre-configured for most users.

---

## Troubleshooting

| Issue | Check |
|-------|-------|
| Gateways not appearing | Run `./portforwardRack.sh status` |
| Seats empty | Run `./seat.sh devices` |
| Port already in use | Try a different port (auto-assigns most of the time) |
| SSH connection refused | Verify gateway access via SSH terminal first |

---

_[Quick toeganian your team!]_ 🧙

---

*Document version: Team:elite-styled ADB Tool, 2026-09-11*