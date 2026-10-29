#!/bin/bash
# Launch adb_gui.py under lldb with sensible defaults for catching
# the Qt text-engine NULL-deref and py_scrcpy_sdk dylib SIGSEGVs.
#
# Usage:  ./adb_gui_debug.sh
#         # interact with the app, click the mirror button
#         # when it crashes, run `bt all` at the (lldb) prompt
#
# Key lldb commands you'll want:
#   bt                 — backtrace of current thread
#   bt all             — backtraces of ALL threads
#   thread list        — list threads
#   thread select N    — switch to thread N
#   frame variable     — locals in current frame
#   register read      — CPU registers
#   image lookup -a <addr>  — what library is at this address?
#   continue           — resume
#   quit               — exit
#
# When SIGSEGV / SIGABRT fires, lldb stops automatically. Run `bt all`
# first, then `image lookup -a <addr>` on any address in the backtrace
# to identify which library (Qt, PyAV, libsystem, etc.) is at fault.

cd "$(dirname "$0")"

lldb \
  -o "process handle SIGPIPE -p false -s false" \
  -o "process handle SIGABRT -s true -p true" \
  -o "process handle SIGSEGV -s true -p true" \
  -o "breakpoint set --name QTextEngine::validate --skip-prologue true" \
  -o "breakpoint set --name QMessageBox::exec" \
  -o "breakpoint set --name ScrcpyClient::start" \
  -o "continue" \
  -- python3 -X faulthandler adb_gui.py
