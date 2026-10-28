# -*- coding: utf-8 -*-
"""Theming functionality for ADB GUI."""

import os
import sys
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication


class ThemeMixin:
    """Mixin class providing theming functionality."""

    def is_system_dark_mode(self):
        """Detect if the system is in dark mode."""
        try:
            if sys.platform == 'darwin':
                # macOS dark mode detection using AppleScript
                import subprocess
                result = subprocess.run(
                    ['osascript', '-e', 'tell application "System Events" to tell appearance preferences to get dark mode'],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0:
                    return result.stdout.strip().lower() == 'true'
                return False
            elif sys.platform == 'win32':
                # Windows dark mode detection via registry
                import winreg
                try:
                    registry = winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)
                    key = winreg.OpenKey(registry, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize')
                    value, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
                    return value == 0
                except:
                    return False
            else:
                # Linux - try to detect via gsettings
                result = os.popen('gsettings get org.gnome.desktop.interface gtk-theme 2>/dev/null').read().strip()
                return 'dark' in result.lower()
        except:
            return False

    def apply_theme(self):
        """Apply light or dark theme based on current settings."""
        # Determine if dark mode should be active
        if self.theme_mode == 'system':
            self.dark_mode = self.is_system_dark_mode()
        elif self.theme_mode == 'dark':
            self.dark_mode = True
        else:  # 'light'
            self.dark_mode = False

        if self.dark_mode:
            self.colors = self.dark_colors.copy()
        else:
            self.colors = self.light_colors.copy()

        # Build stylesheet
        bg = self.colors['bg']
        fg = self.colors['fg']
        card_bg = self.colors['card_bg']
        border_color = self.colors['border']
        accent = self.colors['accent']
        accent_hover = self.colors['accent_hover']
        success = self.colors['success']

        # Conditional colors based on theme
        button_hover_color = '#3e3e42' if self.dark_mode else '#f0f0f0'
        button_pressed_color = '#2d2d30' if self.dark_mode else '#e0e0e0'
        device_group_bg = '#1a3a1a' if self.dark_mode else '#e8f5e9'

        # Build stylesheet as a list of strings, then join
        stylesheet = ""
        stylesheet += "QMainWindow { background-color: " + bg + "; color: " + fg + "; } "
        stylesheet += "QWidget { background-color: " + bg + "; color: " + fg + "; } "
        stylesheet += "QPushButton { background-color: " + card_bg + "; color: " + fg + "; "
        stylesheet += "border: 1px solid " + border_color + "; border-radius: 4px; padding: 8px; "
        stylesheet += "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; "
        stylesheet += "font-size: 11pt; } "
        stylesheet += "QPushButton:hover { background-color: " + button_hover_color + "; } "
        stylesheet += "QPushButton:pressed { background-color: " + button_pressed_color + "; } "
        stylesheet += 'QPushButton[accent="true"] { background-color: ' + accent + '; color: white; } '
        stylesheet += 'QPushButton[accent="true"]:hover { background-color: ' + accent_hover + '; } '
        stylesheet += "QGroupBox { border: 1px solid " + border_color + "; border-radius: 4px; "
        stylesheet += "margin-top: 10px; padding-top: 10px; background-color: " + card_bg + "; "
        stylesheet += "color: " + fg + "; font-weight: bold; } "
        stylesheet += "QGroupBox#deviceGroup { border: 2px solid " + success + "; "
        stylesheet += "background-color: " + device_group_bg + "; } "
        stylesheet += "QGroupBox::title { subcontrol-origin: margin; left: 10px; "
        stylesheet += "padding: 0 5px; color: " + fg + "; } "
        stylesheet += "QLineEdit, QComboBox { border: 1px solid " + border_color + "; border-radius: 4px; "
        stylesheet += "padding: 5px; background-color: " + card_bg + "; color: " + fg + "; font-size: 11pt; } "
        stylesheet += "QTextEdit { border: 1px solid " + border_color + "; border-radius: 4px; "
        stylesheet += "background-color: #1e1e1e; color: #d4d4d4; "
        stylesheet += "font-family: Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace; "
        stylesheet += "font-size: 10pt; } "
        stylesheet += "QLabel { color: " + fg + "; font-size: 11pt; } "
        stylesheet += "QListWidget { background-color: " + card_bg + "; color: " + fg + "; "
        stylesheet += "border: 1px solid " + border_color + "; font-size: 11pt; } "
        stylesheet += "QListWidget::item:hover { background-color: " + button_hover_color + "; } "
        stylesheet += "QTableWidget { background-color: " + card_bg + "; color: " + fg + "; "
        stylesheet += "border: 1px solid " + border_color + "; font-size: 11pt; } "
        stylesheet += "QTableWidget::item:hover { background-color: " + button_hover_color + "; } "
        stylesheet += "QCheckBox { color: " + fg + "; font-size: 11pt; } "
        stylesheet += "QRadioButton { color: " + fg + "; font-size: 11pt; } "
        stylesheet += "QTabWidget::pane { border: 1px solid " + border_color + "; background-color: " + card_bg + "; } "
        stylesheet += "QTabBar::tab { background-color: " + bg + "; color: " + fg + "; "
        stylesheet += "border: 1px solid " + border_color + "; padding: 8px; font-size: 11pt; } "
        stylesheet += "QTabBar::tab:selected { background-color: " + card_bg + "; } "
        stylesheet += "QScrollArea { background-color: " + card_bg + "; border: 1px solid " + border_color + "; } "
        stylesheet += "QDialog { background-color: " + bg + "; color: " + fg + "; }"
        stylesheet += "QListWidget::item { min-height: 25px; padding: 4px; }"
        stylesheet += "QListWidget::item:selected { background: " + accent + "; color: white; }"
        stylesheet += "QListWidget::item:selected:hover { background: " + accent_hover + "; color: white; }"
        # Make selected hover work properly with item background color - override button_hover_color
        stylesheet += "QListWidget::item:hover { background-color: " + accent_hover + "; }"
        # Alternating row colors - use darker bg in dark mode for better contrast
        if self.dark_mode:
            alt_row_color = '#2a2a2a'
            stylesheet += "QListWidget::item:alternate { background-color: " + alt_row_color + "; color: " + fg + "; }"
        else:
            stylesheet += "QListWidget::item:alternate { background-color: " + bg + "; color: " + fg + "; }"

        # Scrollbar handle color enhancement - hide arrow buttons
        # Darker handle in dark mode for better contrast
        if self.dark_mode:
            scrollbar_handle = '#888888'
            scrollbar_hover = accent
        else:
            scrollbar_handle = '#555555'
            scrollbar_hover = accent
        stylesheet += """
        QScrollBar:vertical { width: 14px; background: """ + bg + """; }
        QScrollBar:horizontal { height: 14px; background: """ + bg + """; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
        QScrollBar::sub-line:vertical { height: 0px; background: none; subcontrol-position: top; subcontrol-origin: margin; }
        QScrollBar::add-line:vertical { height: 0px; background: none; subcontrol-position: bottom; subcontrol-origin: margin; }
        QScrollBar::sub-line:horizontal { width: 0px; background: none; subcontrol-position: left; subcontrol-origin: margin; }
        QScrollBar::add-line:horizontal { width: 0px; background: none; subcontrol-position: right; subcontrol-origin: margin; }
        QScrollBar::handle:vertical {
            background: """ + scrollbar_handle + """;
            min-height: 30px;
            border-radius: 6px;
            margin: 2px;
        }
        QScrollBar::handle:vertical:hover {
            background: """ + scrollbar_hover + """;
        }
        QScrollBar::handle:horizontal {
            background: """ + scrollbar_handle + """;
            min-width: 30px;
            border-radius: 6px;
            margin: 2px;
        }
        QScrollBar::handle:horizontal:hover {
            background: """ + scrollbar_hover + """;
        }
        """
        stylesheet += "QStatusBar { background-color: " + card_bg + "; color: " + self.colors.get('text_secondary', fg) + "; } "
        stylesheet += "QStatusBar QLabel { color: " + self.colors.get('text_secondary', fg) + "; font-size: 10pt; } "

        self.setStyleSheet(stylesheet)

        # Update existing UI elements if they exist
        if hasattr(self, 'device_info_label'):
            self.device_info_label.setStyleSheet(f"color: {self.colors['text_secondary']};")
        if hasattr(self, 'adb_path_label'):
            self.adb_path_label.setStyleSheet(f"color: {self.colors['text_tertiary']};")

        # Update status bar items
        if hasattr(self, 'status_bar'):
            self.status_bar.setStyleSheet(
                f"QStatusBar {{ background-color: {card_bg}; color: {self.colors.get('text_secondary', fg)}; }}"
            )

    def update_theme_button_text(self):
        """Update the theme button text based on current theme mode."""
        if hasattr(self, 'theme_btn'):
            if self.theme_mode == 'system':
                mode_name = 'System'
            elif self.theme_mode == 'light':
                mode_name = 'Light'
            else:
                mode_name = 'Dark'
            self.theme_btn.setText(f"🎨 {mode_name} Theme")

    def cycle_theme(self):
        """Cycle through theme modes: system -> light -> dark -> system."""
        modes = ['system', 'light', 'dark']
        current_index = modes.index(self.theme_mode)
        next_index = (current_index + 1) % len(modes)
        self.theme_mode = modes[next_index]

        # Save to settings
        if not hasattr(self, 'settings'):
            self.settings = {}
        self.settings['theme_mode'] = self.theme_mode
        if hasattr(self, 'save_settings'):
            self.save_settings()

        # Apply
        self.apply_theme()
        self.update_theme_button_text()

        # Update all UI elements that have custom styles
        if hasattr(self, 'update_widget_styles'):
            self.update_widget_styles()
