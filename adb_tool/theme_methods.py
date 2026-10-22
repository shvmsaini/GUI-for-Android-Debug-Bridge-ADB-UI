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
                # macOS dark mode detection
                result = os.popen('defaults read -g AppleInterfaceStyle 2>/dev/null').read().strip()
                return result == 'Dark'
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
