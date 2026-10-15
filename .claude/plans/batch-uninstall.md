# Batch Selection & Uninstall — Installed Apps Window

## Context

`show_app_list_window()` (~5093) builds a modal `QDialog` listing installed apps in a
`QListWidget` (~5119). Today `QListWidget` is single-select — `uninstall_selected()`
(~5254) reads one package via `get_selected_package()` (~5237) and runs
`adb uninstall`, with a fallback to `pm uninstall --user 0` for system apps.

The goal: let the user select multiple apps and uninstall them in one batch,
with progress feedback and a summary result.

## Scope (what changes)

All edits are inside `show_app_list_window()` in `adb_gui.py`. No new files, no
new imports, no changes to `scrcpy_mirror.py`.

### 1. Enable multi-selection on the list

- After the `listbox` is created (~5119), set:
  - `listbox.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)`
  - `listbox.setSelectionBehavior(QListWidget.SelectionBehavior.SelectRows)`

`ExtendedSelection` (Ctrl/Cmd-click, Shift-click, and Ctrl-A already works via
the list's built-in key handling) gives the familiar multi-select UX without
breaking single-click selection.

### 2. Refactor `get_selected_package()` → `get_selected_packages()`

- Add a new helper `get_selected_packages()` that returns a `list[str]` of
  package names for **all** selected rows, reusing the same parsing logic
  (strip ` [DISABLED]`, strip ` v...`, extract from `(...)`).
- Keep `get_selected_package()` returning the single current item (used by
  Reinstall / Disable / Enable / Start / Stop / Force-Stop / Extract, which
  stay single-app). Implement it as:
  ```python
  def get_selected_package():
      pkgs = get_selected_packages()
      return pkgs[0] if pkgs else None
  ```
  This avoids duplicating the parsing logic.

### 3. Rewrite `uninstall_selected()` for batch

- Gather `packages = get_selected_packages()`; if empty, warn.
- Confirm dialog: `QMessageBox.question(..., f"Uninstall {n} selected app(s)?\n\n{bulleted list}")`.
- Run the uninstall loop on a background thread (`threading.Thread(daemon=True)`,
  matching the existing pattern — **no** `QThread`).
- Track per-package results: `succeeded`, `failed`, `user_uninstalled`
  (for the `--user 0` fallback path).
- For each package, run the existing two-step logic (plain `uninstall`, then
  fall back to `pm uninstall --user 0` on failure). Reuse the same success
  detection (`'Success' in output` / empty stdout).
- After each package: `self.log(...)` the outcome.
- After the whole batch: remove succeeded/user-uninstalled packages from
  `app_window.original_apps`, call `QTimer.singleShot(0, update_list)` to
  refresh the list, and show a summary `QMessageBox`:
  `Uninstalled X/Y. Failed: pkg1, pkg2.` (only list failures; if all
  succeeded, just say "Uninstalled N app(s) successfully").
- Keep the **Disable Selected** option in mind: the summary should mention
  how many were uninstalled only for the current user (system-app fallback),
  since those aren't fully gone.

### 4. Button label

- Change the button text from `"Uninstall Selected"` → `"Uninstall Selected"`
  (no change) but, to signal batch support, update to
  `"🗑️ Uninstall Selected"`. This matches the emoji already used on the
  main-window uninstall button (~1842).
- No new button is added; the existing button just gains batch capability.

## Non-goals (kept single-select)

- Reinstall, Disable, Enable, Start, Stop, Force-Stop, Extract — these stay
  single-app. They already call `get_selected_package()` which now returns
  the first selected row. No changes needed there.

## Threading safety (per CLAUDE.md)

- The batch runs on a `threading.Thread(daemon=True)`.
- All Qt widget touches (status messages, message boxes, `update_list()`)
  go through `QTimer.singleShot(0, lambda: ...)`.
- `self.log()` / `self.update_status()` are already thread-safe.
- No new shared mutable state: `succeeded`/`failed`/`user_uninstalled` are
  local lists captured by the closure.

## Testing

There's no test suite (`adb_gui.py` is a monolith with no tests; CI only
builds). Verification is manual:

1. Connect a device with several installed apps.
2. Open "Installed Apps", select 3 apps with Ctrl/Cmd-click.
3. Click "Uninstall Selected" → confirm → observe batch progress in the log.
4. Verify the list refreshes and a summary dialog appears.
5. Verify selecting 0 apps shows the "No Selection" warning.
6. Verify Shift-click range selection and Cmd-A select-all both work.
