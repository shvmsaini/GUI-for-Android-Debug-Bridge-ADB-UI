ADB Tool is a free, cross-platform GUI for Android Debug Bridge (ADB) that transforms seat and device management from command-line complexity into simple, visual operations.

If you have been using Vysor for connecting mulitple seats you might have faced problem of recognizing which device is which seat as vysor shows generic device lists with no context.

ADB Tool labels every device with its seat name and gateway — so you always know which device is which.

Also it helps in connecting to seats in single click, whereas earlier you had to manually do tunnel and connection.

It also integrates ADB tools for the device + Seat managment + port forward management into a single tool.

ADB Tool uses our custom seat.sh and portforwardRack.sh scripts to provide this workflow.

ADB Tool will show all of the previous seats you have connected to or racks you have portforwarded. So, in the starting it will be empty. Connect a seat from your favorite terminal using our script and the seat will show up in the seat management section.

🎥 Demo Recording



seat.sh Improvements

We have modified the existing seat.sh scripts into a better workflow.

seat.sh auto-connect

Traditionally this is what you used to

# Connect to a seat
seat.sh tunnel seat008D user@pdc-ite79 5555
seat.sh connect 5555

Now, ports and connection are automatically managed

# Connect to a seat
seat.sh auto-connect seat008D user@pdc-ite79

✅ One command instead of two
✅ No manual tunnel management
✅ Cleaner, less error-prone

seat.sh devices

Before

seat.sh list # only delegates to adb devices
# Shows:
# emulator-5555 device
# But no context — which seat? What gateway?

After

seat.sh devices
# Shows:
DEVICE               STATUS       GATEWAY              SEAT
emulator-5555        device       -                    -
seat008B-5555        device       user@ABC             seat008B
seat024A-5555        device       user@DEF             seat024A

✅ Always know which device is which
✅ Gateway and seat name clearly labelled
✅ No more guessing

seat.sh history

Before

Must remember all gateways, symbols, ports manually

No way to quickly reconnect to a seat used yesterday

After

seat.sh history
# Shows:
seat024H=51926|user@ite51a|1789487703
seat008D=51239|user@pdc-ite79|1789711240
seat007G=51503|user@pdc-dev-ek|1789711271

✅ Reconnect to any past seat instantly
✅ No need to look up gateway or port
✅ Timestamp shows last used time

seat.sh disconnect

Before

Looping over all devices and manually selecting which devices to disconnect.

After

# Disconnect by seat name
seat.sh disconnect seat008D

# Or by port
seat.sh disconnect 51239

✅ Clean tunnel and connection teardown
✅ Clear device list

Integration with ADB Tool GUI

ADB Tool provides a unified graphical interface that automates:

Seat connections: seat.sh auto-connect → one click

Device labeling: seat + gateway always visible

Port forwarding: Rack/seat/user parameters auto-filled

Multi-seat management: Multiple active seats, no SSH conflicts

Screen mirroring: via embedded py-scrcpy-sdk or external scrcpy

File browser: drag-and-drop upload/download

ADB tools: logcat, screenshot, shell, packages

Why ADB Tool Beats Vysor (and it's Free)

Feature

Vysor

ADB Tool

Cost

Freemium

Free & open-source

Seat management

❌ No concept

✅ Labels every device with seat + gateway

Device distinction

❌ All devices identical

✅ Clear seat names and icons

Port forwarding

❌ No concept

✅ One-click rack/seat/user setup

Customization

❌ Limited (Propriety Software)

✅ Open source, extend as needed

Quick Start

Ensure seat.sh and portForwardRack.sh are in PATH or configure paths in ADB Tool Settings

Launch ADB Tool → Seat Management shows history of previously used seats

Click any seat → automatically connects (auto-connect) and sets up port forwarding

Device appears in dropdown, clearly labelled with seat name + gateway

Select device → mirror screen, browse files, run ADB commands

# Run from source
python3 adb_gui.py

# Or build standalone (mac/win/linux)
pyinstaller --name "ADB Tool" --windowed --onefile --clean adb_gui.py

Source Code

<attach-zip-here>

Summary

Free, seat-aware, automation-oriented ADB GUI for developers working with Android seats.

It is in beta stage. For any issues or feature request, please reach out or comment here.

