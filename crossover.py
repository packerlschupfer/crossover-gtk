#!/usr/bin/env python3
"""
CrossOver GTK - Crosshair Overlay for Linux

A lightweight, transparent, always-on-top crosshair overlay.

Requirements: python3-gi, python3-gi-cairo, gir1.2-ayatanaappindicator3
  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1

Usage:
  crossover.py                  Start the overlay
  crossover.py --install-keys   Register GNOME keyboard shortcuts
  crossover.py --uninstall-keys Remove GNOME keyboard shortcuts
  crossover.py CMD              Send command to running instance
                                (lock, hide, center, quit,
                                 up, down, left, right,
                                 up10, down10, left10, right10)

Keyboard shortcuts (after --install-keys):
  Ctrl+Shift+Alt+X          Toggle lock (click-through, hide UI)
  Ctrl+Shift+Alt+H          Hide/show crosshair
  Ctrl+Shift+Alt+C          Center on current monitor
  Ctrl+Shift+Alt+Q          Quit
  Ctrl+Alt+Numpad 8/2/4/6   Nudge 1px (up/down/left/right)
  Ctrl+Shift+Alt+Numpad      Nudge 10px
"""

import cairo
import errno
import json
import math
import os
import signal
import stat
import subprocess
import sys

# Detect display backend (before importing Gtk — GDK picks its backend then)
IS_WAYLAND = os.environ.get('XDG_SESSION_TYPE') == 'wayland' or 'WAYLAND_DISPLAY' in os.environ


def _force_xwayland():
    """GNOME's compositor has neither always-on-top nor layer-shell, so a native
    Wayland overlay sinks behind whatever you Alt+Tab to. XWayland honours
    _NET_WM_STATE_ABOVE, so run there instead — that also restores exact
    positioning and drag-to-move. Compositors with layer-shell (wlroots, KWin)
    are left alone; set CROSSOVER_NO_XWAYLAND=1 to opt out."""
    if not IS_WAYLAND or os.environ.get('CROSSOVER_NO_XWAYLAND'):
        return False
    if 'GNOME' not in os.environ.get('XDG_CURRENT_DESKTOP', '').upper():
        return False
    return 'DISPLAY' in os.environ      # no XWayland running -> stay on Wayland


USING_XWAYLAND = _force_xwayland()
if USING_XWAYLAND:
    os.environ['GDK_BACKEND'] = 'x11'

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gdk, GLib, GdkPixbuf

# Suppress harmless libayatana-appindicator deprecation warning
GLib.log_set_handler('libayatana-appindicator', GLib.LogLevelFlags.LEVEL_WARNING, lambda *a: None)

try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
    HAS_INDICATOR = True
except (ValueError, ImportError):
    HAS_INDICATOR = False

HAS_LAYER_SHELL = False
GtkLayerShell = None

def _load_layer_shell():
    global HAS_LAYER_SHELL, GtkLayerShell
    if GtkLayerShell is not None:
        return
    try:
        gi.require_version('GtkLayerShell', '0.1')
        from gi.repository import GtkLayerShell as _GLS
        GtkLayerShell = _GLS
        HAS_LAYER_SHELL = True
    except (ValueError, ImportError):
        HAS_LAYER_SHELL = False

# Layer shell support checked lazily (needs GTK init, triggers warnings)
_layer_shell_checked = None

def layer_shell_supported():
    global _layer_shell_checked
    if _layer_shell_checked is None:
        _load_layer_shell()
        if not IS_WAYLAND or not HAS_LAYER_SHELL:
            _layer_shell_checked = False
        else:
            # Suppress C-level warnings from gtk-layer-shell on unsupported compositors
            old_stderr = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2)
            try:
                _layer_shell_checked = GtkLayerShell.is_supported()
            except Exception:
                _layer_shell_checked = False
            finally:
                os.dup2(old_stderr, 2)
                os.close(old_stderr)
                os.close(devnull)
    return _layer_shell_checked

VERSION = '0.1.2'

CONFIG_DIR = os.path.expanduser('~/.config/crossover-gtk')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
FIFO_PATH = os.path.join(CONFIG_DIR, 'ctl.fifo')
AUTOSTART_DIR = os.path.expanduser('~/.config/autostart')
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, 'crossover-gtk.desktop')
SCRIPT_PATH = os.path.realpath(__file__)

APP_ID = 'crossover-gtk'
ICON_FILES = [
    os.path.join(os.path.dirname(SCRIPT_PATH), 'data', 'crossover-gtk.png'),
    '/usr/share/icons/hicolor/48x48/apps/crossover-gtk.png',
]

CROSSHAIR_IMAGE_DIRS = [
    os.path.join(CONFIG_DIR, 'crosshairs'),
    os.path.join(os.path.dirname(SCRIPT_PATH), 'crosshairs'),
    '/usr/share/crossover-gtk/crosshairs',
]

# The overlay window is a fixed square; it grows past this only when a large
# crosshair would otherwise be clipped by its own window (see _sync_window_size).
BASE_WINDOW_SIZE = 101

# Up to 0.1.1 the window carried no size request, and GTK ignores
# set_default_size() on a non-resizable window with no child — so the overlay
# was really 200x200 no matter what window_size said. Saved positions were
# tuned against that, and need re-anchoring once the window honours its size.
LEGACY_WINDOW_SIZE = 200
GEOMETRY_VERSION = 2

DEFAULT_CONFIG = {
    'color': [0.0, 1.0, 0.0, 1.0],
    'outline_color': [0.0, 0.0, 0.0, 0.5],
    'outline': False,
    'size': 10,
    'thickness': 1,
    'gap': 4,
    'dot': False,
    'dot_size': 2,
    'shape': 'cross',
    'image_path': None,
    'image_size': 40,
    'window_size': BASE_WINDOW_SIZE,
    'position_x': None,
    'position_y': None,
    'opacity': 1.0,
    'autostart': False,
    'profiles': {},
    'active_profile': None,
}

SHAPES = ['cross', 'circle', 'square', 'circle+cross', 'square+cross',
          'pixel 1x1', 'pixel 2x2', 'pixel 3x3', 'pixel 4x4', 'pixel 5x5',
          'image']

PRESET_COLORS = {
    'Green':   [0.0, 1.0, 0.0, 1.0],
    'Red':     [1.0, 0.0, 0.0, 1.0],
    'Cyan':    [0.0, 1.0, 1.0, 1.0],
    'Yellow':  [1.0, 1.0, 0.0, 1.0],
    'Magenta': [1.0, 0.0, 1.0, 1.0],
    'White':   [1.0, 1.0, 1.0, 1.0],
    'Orange':  [1.0, 0.5, 0.0, 1.0],
}

# GNOME shortcut definitions: (name, binding, command)
GNOME_SHORTCUTS = [
    ('CrossOver Lock',       '<Ctrl><Shift><Alt>x',         f'python3 {SCRIPT_PATH} lock'),
    ('CrossOver Hide',       '<Ctrl><Shift><Alt>h',         f'python3 {SCRIPT_PATH} hide'),
    ('CrossOver Center',     '<Ctrl><Shift><Alt>c',         f'python3 {SCRIPT_PATH} center'),
    ('CrossOver Quit',       '<Ctrl><Shift><Alt>q',         f'python3 {SCRIPT_PATH} quit'),
    ('CrossOver Up',         '<Ctrl><Alt>KP_8',             f'python3 {SCRIPT_PATH} up'),
    ('CrossOver Down',       '<Ctrl><Alt>KP_2',             f'python3 {SCRIPT_PATH} down'),
    ('CrossOver Left',       '<Ctrl><Alt>KP_4',             f'python3 {SCRIPT_PATH} left'),
    ('CrossOver Right',      '<Ctrl><Alt>KP_6',             f'python3 {SCRIPT_PATH} right'),
    ('CrossOver Up 10',      '<Ctrl><Shift><Alt>KP_8',     f'python3 {SCRIPT_PATH} up10'),
    ('CrossOver Down 10',    '<Ctrl><Shift><Alt>KP_2',     f'python3 {SCRIPT_PATH} down10'),
    ('CrossOver Left 10',    '<Ctrl><Shift><Alt>KP_4',     f'python3 {SCRIPT_PATH} left10'),
    ('CrossOver Right 10',   '<Ctrl><Shift><Alt>KP_6',     f'python3 {SCRIPT_PATH} right10'),
]

GNOME_KEYBIND_BASE = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings'
GNOME_KEYBIND_SCHEMA = 'org.gnome.settings-daemon.plugins.media-keys'


# ── Config ──────────────────────────────────────────────────────────

def _validate_config(config):
    """Coerce a hand-edited or outdated config back into usable types.

    Every value reaches Cairo eventually, so a stray string here is a crash in
    the draw handler rather than a visible error.
    """
    for key in ('color', 'outline_color'):
        v = config.get(key)
        if (isinstance(v, (list, tuple)) and len(v) == 4
                and all(isinstance(c, (int, float)) for c in v)):
            config[key] = [min(1.0, max(0.0, float(c))) for c in v]
        else:
            config[key] = list(DEFAULT_CONFIG[key])

    for key in ('size', 'thickness', 'gap', 'dot_size', 'image_size', 'window_size'):
        try:
            config[key] = int(config[key])
        except (TypeError, ValueError):
            config[key] = DEFAULT_CONFIG[key]

    for key in ('outline', 'dot', 'autostart'):
        config[key] = bool(config.get(key))

    try:
        config['opacity'] = min(1.0, max(0.1, float(config['opacity'])))
    except (TypeError, ValueError):
        config['opacity'] = DEFAULT_CONFIG['opacity']

    if config.get('shape') not in SHAPES:
        config['shape'] = DEFAULT_CONFIG['shape']

    for key in ('position_x', 'position_y'):
        if config.get(key) is not None:
            try:
                config[key] = int(config[key])
            except (TypeError, ValueError):
                config[key] = None

    if not isinstance(config.get('image_path'), str):
        config['image_path'] = None

    profiles = config.get('profiles')
    if not isinstance(profiles, dict):
        profiles = {}
    clean = {}
    for name, p in profiles.items():
        if not isinstance(p, dict):
            continue
        try:
            clean[str(name)] = {'position_x': int(p['position_x']),
                                'position_y': int(p['position_y'])}
        except (KeyError, TypeError, ValueError):
            continue
    config['profiles'] = clean
    if config.get('active_profile') not in clean:
        config['active_profile'] = None

    return config


def crosshair_extent(cfg):
    """Half-width in px of the drawn crosshair, outline and line caps included."""
    shape = cfg['shape']
    if shape == 'image':
        # With no loadable image the draw falls through to the centre dot,
        # so the window still has to be big enough for it.
        reach = cfg['image_size'] // 2 + 1
        if cfg['dot']:
            reach = max(reach, cfg['dot_size'] + 2)
        return reach
    if shape.startswith('pixel'):
        px = int(shape.split()[1].split('x')[0])
        return px + 2
    line_width = cfg['thickness'] + (2 if cfg['outline'] else 0)
    ext = cfg['size']
    if 'cross' in shape and ('circle' in shape or 'square' in shape):
        ext += 4                      # matches the arm length in _draw_crosshair
    reach = ext + line_width          # LINE_CAP_SQUARE overshoots by lw/2
    if cfg['dot']:
        reach = max(reach, cfg['dot_size'] + 1 + line_width)
    return reach


def window_size_for(cfg):
    """Smallest odd window that draws this crosshair without clipping it."""
    return max(BASE_WINDOW_SIZE, 2 * crosshair_extent(cfg) + 1)


def shift_saved_positions(config, delta):
    """Move the live origin and every saved profile by delta on both axes."""
    if not delta:
        return
    if config.get('position_x') is not None:
        config['position_x'] += delta
        config['position_y'] += delta
    for p in config.get('profiles', {}).values():
        p['position_x'] += delta
        p['position_y'] += delta


def _migrate_geometry(config, existing):
    """Re-anchor positions saved while the window was really LEGACY_WINDOW_SIZE.

    The crosshair is drawn at the centre of the window's *actual* allocation, so
    a saved origin only means what it did if the window is the same size. Up to
    0.1.1 that size was always 200 regardless of window_size, which puts every
    position tuned back then half the difference away from where its owner aimed.
    """
    try:
        version = int(config.pop('geometry_version', 1))
    except (TypeError, ValueError):
        version = 1
    if version >= GEOMETRY_VERSION:
        config['geometry_version'] = version
        return False
    config['geometry_version'] = GEOMETRY_VERSION
    if not existing:
        return False        # fresh config — nothing was ever tuned against 200px
    ws = window_size_for(config)
    config['window_size'] = ws
    shift_saved_positions(config, LEGACY_WINDOW_SIZE // 2 - ws // 2)
    return True


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            loaded = json.load(f)
    except (OSError, json.JSONDecodeError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    if loaded:
        # Pre-0.1 configs called it 'style'
        if 'style' in loaded and 'shape' not in loaded:
            loaded['shape'] = loaded.pop('style')
        loaded.pop('style', None)
        config.update(loaded)
    _validate_config(config)
    if _migrate_geometry(config, loaded):
        save_config(config)
    return config


def save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


# ── FIFO control ────────────────────────────────────────────────────

def _not_running(detail):
    print(f'CrossOver is not running ({detail}).')
    sys.exit(1)


def send_command(cmd):
    """Send a command to the running instance via FIFO.

    Opened non-blocking: a plain open() on a FIFO with no reader blocks forever,
    which would wedge every keyboard shortcut after a stale FIFO is left behind.
    """
    try:
        if not stat.S_ISFIFO(os.stat(FIFO_PATH).st_mode):
            _not_running(f'{FIFO_PATH} is not a FIFO')
        fd = os.open(FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        _not_running(f'no FIFO at {FIFO_PATH}')
    except OSError as e:
        if e.errno != errno.ENXIO:
            raise
        # FIFO exists but nobody is reading it — a leftover from a crashed run.
        try:
            os.unlink(FIFO_PATH)
        except OSError:
            pass
        _not_running('stale control FIFO removed')
    try:
        os.write(fd, (cmd + '\n').encode())
    except BrokenPipeError:
        _not_running('the instance exited')
    finally:
        os.close(fd)


def setup_fifo():
    """Create the control FIFO."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        os.unlink(FIFO_PATH)
    except FileNotFoundError:
        pass
    os.mkfifo(FIFO_PATH)


class FifoListener:
    """Listen for commands on a FIFO, integrated with GLib main loop."""

    def __init__(self, handler):
        self.handler = handler
        self.fd = None
        self.source_id = None
        self._open()

    def _open(self):
        # Open FIFO non-blocking for reading
        self.fd = os.open(FIFO_PATH, os.O_RDONLY | os.O_NONBLOCK)
        self.source_id = GLib.io_add_watch(
            self.fd, GLib.IO_IN | GLib.IO_HUP, self._on_data
        )

    def _on_data(self, fd, condition):
        if condition & GLib.IO_IN:
            data = os.read(fd, 4096).decode('utf-8', errors='ignore')
            for line in data.strip().split('\n'):
                cmd = line.strip()
                if cmd:
                    self.handler(cmd)
        if condition & GLib.IO_HUP:
            # Writer closed, reopen FIFO for next writer
            GLib.source_remove(self.source_id)
            os.close(self.fd)
            self._open()
            return False
        return True

    def cleanup(self):
        if self.source_id is not None:
            GLib.source_remove(self.source_id)
            self.source_id = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            os.unlink(FIFO_PATH)
        except FileNotFoundError:
            pass


# ── GNOME shortcuts ─────────────────────────────────────────────────

def install_gnome_shortcuts():
    """Register custom keyboard shortcuts in GNOME."""
    # Remove any existing CrossOver shortcuts first, then read what is left
    uninstall_gnome_shortcuts(quiet=True)

    result = subprocess.run(
        ['gsettings', 'get', GNOME_KEYBIND_SCHEMA, 'custom-keybindings'],
        capture_output=True, text=True
    )
    existing = result.stdout.strip()
    if existing in ('@as []', '[]'):
        paths = []
    else:
        paths = [p.strip().strip("'") for p in existing.strip('[]').split(',') if p.strip()]

    schema = 'org.gnome.settings-daemon.plugins.media-keys.custom-keybinding'

    for i, (name, binding, command) in enumerate(GNOME_SHORTCUTS):
        path = f'{GNOME_KEYBIND_BASE}/crossover{i}/'
        subprocess.run(['gsettings', 'set', f'{schema}:{path}', 'name', name])
        subprocess.run(['gsettings', 'set', f'{schema}:{path}', 'binding', binding])
        subprocess.run(['gsettings', 'set', f'{schema}:{path}', 'command', command])
        if path not in paths:
            paths.append(path)

    # Update the list of custom keybindings
    paths_str = '[' + ', '.join(f"'{p}'" for p in paths) + ']'
    subprocess.run(['gsettings', 'set', GNOME_KEYBIND_SCHEMA, 'custom-keybindings', paths_str])

    print(f'Installed {len(GNOME_SHORTCUTS)} GNOME keyboard shortcuts:')
    for name, binding, _ in GNOME_SHORTCUTS:
        print(f'  {binding:30s} {name}')


def uninstall_gnome_shortcuts(quiet=False):
    """Remove CrossOver keyboard shortcuts from GNOME."""
    result = subprocess.run(
        ['gsettings', 'get', GNOME_KEYBIND_SCHEMA, 'custom-keybindings'],
        capture_output=True, text=True
    )
    existing = result.stdout.strip()
    if existing in ('@as []', '[]'):
        if not quiet:
            print('No custom keybindings found.')
        return

    paths = [p.strip().strip("'") for p in existing.strip('[]').split(',') if p.strip()]
    remaining = [p for p in paths if '/crossover' not in p]

    # Reset each crossover keybinding
    schema = 'org.gnome.settings-daemon.plugins.media-keys.custom-keybinding'
    for p in paths:
        if '/crossover' in p:
            subprocess.run(['gsettings', 'reset', f'{schema}:{p}', 'name'],
                           capture_output=True)
            subprocess.run(['gsettings', 'reset', f'{schema}:{p}', 'binding'],
                           capture_output=True)
            subprocess.run(['gsettings', 'reset', f'{schema}:{p}', 'command'],
                           capture_output=True)

    if remaining:
        paths_str = '[' + ', '.join(f"'{p}'" for p in remaining) + ']'
    else:
        paths_str = '@as []'
    subprocess.run(['gsettings', 'set', GNOME_KEYBIND_SCHEMA, 'custom-keybindings', paths_str])

    if not quiet:
        removed = len(paths) - len(remaining)
        print(f'Removed {removed} CrossOver shortcuts.')


# ── Autostart ───────────────────────────────────────────────────────

def setup_autostart(enable):
    if enable:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        with open(AUTOSTART_FILE, 'w') as f:
            f.write(f"""[Desktop Entry]
Type=Application
Name=CrossOver GTK
Comment=Crosshair Overlay
Exec=python3 {SCRIPT_PATH}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
""")
    else:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass


# ── Helpers ─────────────────────────────────────────────────────────

def find_crosshair_images():
    images = []
    seen = set()
    for d in CROSSHAIR_IMAGE_DIRS:
        # An installed package puts the script in the same prefix as the
        # system-wide directory, so two entries resolve to one directory.
        d = os.path.realpath(d)
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        for root, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith(('.png', '.svg')):
                    images.append(os.path.join(root, f))
    images.sort()
    return images


def set_app_icon():
    """Give our windows a real icon instead of the generic fallback.

    On Wayland the icon comes from the app-id -> .desktop file lookup, and GTK
    takes the app-id from the program name — which is 'python3' when the script
    is run directly. On X11 the window icon is set explicitly.
    """
    GLib.set_prgname(APP_ID)
    Gdk.set_program_class('CrossOver')

    if Gtk.IconTheme.get_default().has_icon(APP_ID):
        Gtk.Window.set_default_icon_name(APP_ID)
        return
    for path in ICON_FILES:
        if os.path.exists(path):
            try:
                Gtk.Window.set_default_icon_from_file(path)
                return
            except GLib.Error:
                pass
    Gtk.Window.set_default_icon_from_file(create_tray_icon_file(64))


def create_tray_icon_file(size=22):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.set_source_rgba(0, 0, 0, 0)
    cr.paint()
    cx, cy = size / 2, size / 2
    s = size // 3
    g = 2
    cr.set_source_rgba(0, 1, 0, 1)
    cr.set_line_width(2)
    cr.move_to(cx, cy - s)
    cr.line_to(cx, cy - g)
    cr.move_to(cx, cy + g)
    cr.line_to(cx, cy + s)
    cr.move_to(cx - s, cy)
    cr.line_to(cx - g, cy)
    cr.move_to(cx + g, cy)
    cr.line_to(cx + s, cy)
    cr.stroke()
    cr.arc(cx, cy, 1, 0, 2 * math.pi)
    cr.fill()
    icon_path = os.path.join(CONFIG_DIR, 'tray-icon.png')
    os.makedirs(CONFIG_DIR, exist_ok=True)
    surface.write_to_png(icon_path)
    return icon_path


# ── Window ──────────────────────────────────────────────────────────

class CrosshairWindow(Gtk.Window):

    def __init__(self, config):
        super().__init__(title='CrossOver')
        self.config = config
        self.locked = False
        self.crosshair_visible = True
        self.custom_image_pixbuf = None
        self.use_layer_shell = layer_shell_supported()

        self._load_custom_image()

        # Wayland: init layer-shell before anything else
        if self.use_layer_shell:
            self._setup_wayland()
        else:
            self._setup_x11()

        resized = self._sync_window_size(apply=False)
        ws = config['window_size']
        self.set_default_size(ws, ws)
        self.set_size_request(ws, ws)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        self.connect('draw', self.on_draw)

        # Drag support (X11 only — Wayland uses nudge keys)
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.dragging = False
        if not self.use_layer_shell:
            self.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK
                | Gdk.EventMask.BUTTON_RELEASE_MASK
                | Gdk.EventMask.POINTER_MOTION_MASK
            )
            self.connect('button-press-event', self.on_button_press)
            self.connect('button-release-event', self.on_button_release)
            self.connect('motion-notify-event', self.on_motion)

        if self.use_layer_shell:
            self._apply_wayland_position()
        elif config['position_x'] is not None and config['position_y'] is not None:
            self.move(config['position_x'], config['position_y'])
        else:
            self.center_on_screen()

        self.show_all()

        if resized:
            # Persist the new size and the matching origin, so the next start
            # sees them already reconciled and does not shift the window again.
            save_config(self.config)

    def _setup_x11(self):
        """Configure window for X11 or GNOME Wayland fallback."""
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_app_paintable(True)
        if not IS_WAYLAND:
            self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_keep_above(True)
        self.stick()
        self.connect('map-event', self._on_map_reapply_above)

    def _on_map_reapply_above(self, widget, event):
        """Re-apply keep-above after window is mapped (needed on some Wayland compositors)."""
        self.set_keep_above(True)
        return False

    def _setup_wayland(self):
        """Configure window for Wayland using gtk-layer-shell."""
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_exclusive_zone(self, -1)  # don't push other windows
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_app_paintable(True)

    def _apply_wayland_position(self):
        """Position window on Wayland via layer-shell margins from top-left."""
        cfg = self.config
        if cfg['position_x'] is not None and cfg['position_y'] is not None:
            px, py = cfg['position_x'], cfg['position_y']
        else:
            # Center on screen
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0)
            geom = monitor.get_geometry()
            alloc = self.get_allocation()
            ws = alloc.width if alloc.width > 1 else cfg['window_size']
            px = geom.x + (geom.width // 2) - (ws // 2)
            py = geom.y + (geom.height // 2) - (ws // 2)
        # Anchor to top-left and use margins for positioning
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, False)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, False)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, py)
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, px)

    def _load_custom_image(self):
        path = self.config.get('image_path')
        if path and os.path.isfile(path):
            try:
                size = self.config['image_size']
                self.custom_image_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                    path, size, size, True
                )
            except Exception as e:
                print(f'Failed to load image {path}: {e}')
                self.custom_image_pixbuf = None

    def _move_to(self, x, y):
        """Move the window, whichever positioning mechanism this backend uses."""
        if self.use_layer_shell:
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, x)
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.TOP, y)
        else:
            self.move(x, y)

    def _current_position(self):
        """Window origin. get_position() is meaningless under layer-shell, so
        there the config is the only record of where we put ourselves."""
        if self.use_layer_shell:
            return (self.config.get('position_x') or 0,
                    self.config.get('position_y') or 0)
        return tuple(self.get_position())

    def _sync_window_size(self, apply=True):
        """Grow the window when the crosshair would outgrow it.

        window_size is fixed at BASE_WINDOW_SIZE for ordinary crosshairs; a
        50px cross needs 107 and was previously drawn clipped. Every saved
        origin moves by half the growth so its crosshair centre stays on the
        same pixel — profiles included, or loading one would shift your aim.
        """
        ws = window_size_for(self.config)
        old = self.config.get('window_size') or BASE_WINDOW_SIZE
        if ws == old:
            return False
        self.config['window_size'] = ws
        shift_saved_positions(self.config, (old - ws) // 2)
        if apply:
            self.set_size_request(ws, ws)
            self.resize(ws, ws)
            if self.config.get('position_x') is not None:
                self._move_to(self.config['position_x'], self.config['position_y'])
        return True

    def center_on_screen(self):
        display = Gdk.Display.get_default()
        if self.use_layer_shell:
            monitor = display.get_primary_monitor() or display.get_monitor(0)
        else:
            seat = display.get_default_seat()
            pointer = seat.get_pointer()
            _, x, y = pointer.get_position()
            monitor = display.get_monitor_at_point(x, y)
        geom = monitor.get_geometry()
        alloc = self.get_allocation()
        ws = alloc.width if alloc.width > 1 else self.config['window_size']
        cx = geom.x + (geom.width // 2) - (ws // 2)
        cy = geom.y + (geom.height // 2) - (ws // 2)
        self._move_to(cx, cy)
        self.config['position_x'] = cx
        self.config['position_y'] = cy
        save_config(self.config)

    def nudge(self, dx, dy):
        x, y = self._current_position()
        self._move_to(x + dx, y + dy)
        self.config['position_x'] = x + dx
        self.config['position_y'] = y + dy
        save_config(self.config)

    def on_draw(self, widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        alloc = self.get_allocation()
        w, h = alloc.width, alloc.height
        cx = int(w / 2) + 0.5
        cy = int(h / 2) + 0.5

        if not self.locked:
            self._draw_ui_chrome(cr, w, h)

        if self.crosshair_visible:
            opacity = self.config.get('opacity', 1.0)
            if opacity < 1.0:
                cr.push_group()
                self._draw_crosshair(cr, cx, cy)
                cr.pop_group_to_source()
                cr.paint_with_alpha(opacity)
            else:
                self._draw_crosshair(cr, cx, cy)

    def _draw_ui_chrome(self, cr, w, h):
        cr.set_source_rgba(0.2, 0.2, 0.2, 0.3)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        cr.set_source_rgba(0.5, 0.5, 0.5, 0.5)
        cr.set_line_width(1)
        cr.rectangle(0.5, 0.5, w - 1, h - 1)
        cr.stroke()

        cr.set_source_rgba(1, 1, 1, 0.6)
        cr.select_font_face('Sans', cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(9)
        hint = 'drag to move' if not self.use_layer_shell else 'numpad to nudge'
        ext = cr.text_extents(hint)
        cr.move_to((w - ext.width) / 2, h - 6)
        cr.show_text(hint)

        ext = cr.text_extents('Ctrl+Shift+Alt+X to lock')
        cr.move_to((w - ext.width) / 2, 12)
        cr.show_text('Ctrl+Shift+Alt+X to lock')

    def _draw_crosshair(self, cr, cx, cy):
        cfg = self.config
        shape = cfg['shape']

        # Image mode
        if shape == 'image' and self.custom_image_pixbuf:
            iw = self.custom_image_pixbuf.get_width()
            ih = self.custom_image_pixbuf.get_height()
            Gdk.cairo_set_source_pixbuf(cr, self.custom_image_pixbuf,
                                        cx - iw / 2, cy - ih / 2)
            cr.paint()
            return

        # Pixel mode — exact NxN square, no outline/dot
        if shape.startswith('pixel'):
            parts = shape.split()
            px = int(parts[1].split('x')[0]) if len(parts) > 1 else 1
            ix = int(cx)
            iy = int(cy)
            half = (px - 1) // 2
            if cfg['outline']:
                cr.set_source_rgba(*cfg['outline_color'])
                cr.rectangle(ix - half - 1, iy - half - 1, px + 2, px + 2)
                cr.fill()
            cr.set_source_rgba(*cfg['color'])
            cr.rectangle(ix - half, iy - half, px, px)
            cr.fill()
            return

        size = cfg['size']
        gap = cfg['gap']
        thickness = cfg['thickness']
        color = cfg['color']

        # Draw each component with outline pass then color pass
        for is_outline in ([True, False] if cfg['outline'] else [False]):
            if is_outline:
                cr.set_source_rgba(*cfg['outline_color'])
                cr.set_line_width(thickness + 2)
            else:
                cr.set_source_rgba(*color)
                cr.set_line_width(thickness)
            cr.set_line_cap(cairo.LINE_CAP_SQUARE)
            cr.set_line_join(cairo.LINE_JOIN_MITER)

            # Cross lines
            if 'cross' in shape:
                cr.new_path()
                ext = size + 4 if ('circle' in shape or 'square' in shape) else size
                cr.move_to(cx, cy - ext)
                cr.line_to(cx, cy - gap)
                cr.move_to(cx, cy + gap)
                cr.line_to(cx, cy + ext)
                cr.move_to(cx - ext, cy)
                cr.line_to(cx - gap, cy)
                cr.move_to(cx + gap, cy)
                cr.line_to(cx + ext, cy)
                cr.stroke()

            # Circle
            if 'circle' in shape:
                cr.new_path()
                cr.arc(cx, cy, size, 0, 2 * math.pi)
                cr.close_path()
                cr.stroke()

            # Square
            if 'square' in shape:
                cr.new_path()
                cr.rectangle(cx - size, cy - size, size * 2, size * 2)
                cr.stroke()

            # Center dot
            if cfg['dot']:
                cr.new_path()
                ds = cfg['dot_size'] + 1 if is_outline else cfg['dot_size']
                cr.rectangle(cx - ds, cy - ds, ds * 2, ds * 2)
                cr.fill()

    def toggle_lock(self):
        self.locked = not self.locked
        if self.locked:
            self.input_shape_combine_region(cairo.Region())
        else:
            self.input_shape_combine_region(None)
        self.queue_draw()

    def toggle_visibility(self):
        self.crosshair_visible = not self.crosshair_visible
        self.queue_draw()

    def set_shape(self, shape):
        self.config['shape'] = shape
        if shape == 'image':
            self._load_custom_image()
        self._sync_window_size()
        save_config(self.config)
        self.queue_draw()

    def set_dot(self, enabled):
        self.config['dot'] = enabled
        self._sync_window_size()
        save_config(self.config)
        self.queue_draw()

    def set_size(self, size):
        self.config['size'] = max(2, min(100, size))
        self._sync_window_size()
        save_config(self.config)
        self.queue_draw()

    def set_thickness(self, thickness):
        self.config['thickness'] = max(1, min(10, thickness))
        self._sync_window_size()
        save_config(self.config)
        self.queue_draw()

    def set_gap(self, gap):
        self.config['gap'] = max(0, min(20, gap))
        save_config(self.config)
        self.queue_draw()

    def set_opacity(self, opacity):
        self.config['opacity'] = max(0.1, min(1.0, opacity))
        save_config(self.config)
        self.queue_draw()

    def set_color(self, rgba):
        self.config['color'] = list(rgba)
        save_config(self.config)
        self.queue_draw()

    def set_image(self, path):
        self.config['image_path'] = path
        self.config['shape'] = 'image'
        self._load_custom_image()
        self._sync_window_size()
        save_config(self.config)
        self.queue_draw()

    def save_position(self):
        x, y = self._current_position()
        self.config['position_x'] = x
        self.config['position_y'] = y
        save_config(self.config)

    def save_profile(self, name):
        x, y = self._current_position()
        self.config.setdefault('profiles', {})[name] = {
            'position_x': x,
            'position_y': y,
        }
        self.config['active_profile'] = name
        save_config(self.config)

    def load_profile(self, name):
        profiles = self.config.get('profiles', {})
        if name in profiles:
            p = profiles[name]
            self.config['position_x'] = p['position_x']
            self.config['position_y'] = p['position_y']
            self.config['active_profile'] = name
            self._move_to(p['position_x'], p['position_y'])
            save_config(self.config)

    def delete_profile(self, name):
        profiles = self.config.get('profiles', {})
        profiles.pop(name, None)
        if self.config.get('active_profile') == name:
            self.config['active_profile'] = None
        save_config(self.config)

    # Drag handling
    def on_button_press(self, widget, event):
        if event.button == 1 and not self.locked:
            self.dragging = True
            self.drag_start_x = event.x_root
            self.drag_start_y = event.y_root

    def on_button_release(self, widget, event):
        if event.button == 1:
            self.dragging = False
            self.save_position()

    def on_motion(self, widget, event):
        if self.dragging and not self.locked:
            dx = event.x_root - self.drag_start_x
            dy = event.y_root - self.drag_start_y
            pos = self.get_position()
            self.move(pos[0] + int(dx), pos[1] + int(dy))
            self.drag_start_x = event.x_root
            self.drag_start_y = event.y_root


# ── Tray ────────────────────────────────────────────────────────────

class TrayIcon:

    def __init__(self, app):
        self.app = app
        self.indicator = None

        if not HAS_INDICATOR:
            print('Tray icon not available (install gir1.2-ayatanaappindicator3-0.1)')
            return

        icon_path = create_tray_icon_file()
        self.indicator = AppIndicator.Indicator.new(
            'crossover-gtk',
            icon_path,
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_menu(self._build_menu())

    def _build_menu(self):
        menu = Gtk.Menu()

        item = Gtk.MenuItem(label='Lock/Unlock (Ctrl+Shift+Alt+X)')
        item.connect('activate', lambda _: self.app.window.toggle_lock())
        menu.append(item)

        item = Gtk.MenuItem(label='Hide/Show (Ctrl+Shift+Alt+H)')
        item.connect('activate', lambda _: self.app.window.toggle_visibility())
        menu.append(item)

        item = Gtk.MenuItem(label='Center (Ctrl+Shift+Alt+C)')
        item.connect('activate', lambda _: self.app.window.center_on_screen())
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        # Shape
        current_shape = self.app.config['shape']
        shape_item = Gtk.MenuItem(label='Shape')
        shape_menu = Gtk.Menu()
        for shape in SHAPES:
            if shape == 'image':
                continue
            s_item = Gtk.CheckMenuItem(label=shape)
            s_item.set_active(current_shape == shape)
            s_item.connect('activate', self._on_shape_change, shape)
            shape_menu.append(s_item)
        shape_item.set_submenu(shape_menu)
        menu.append(shape_item)

        # Center dot toggle
        dot_item = Gtk.CheckMenuItem(label='Center dot')
        dot_item.set_active(self.app.config.get('dot', False))
        dot_item.connect('toggled', lambda w: self.app.window.set_dot(w.get_active()))
        menu.append(dot_item)

        # Size
        size_item = Gtk.MenuItem(label=f'Size ({self.app.config["size"]}px)')
        size_menu = Gtk.Menu()
        for s in [5, 8, 10, 15, 20, 25, 30, 40, 50]:
            si = Gtk.CheckMenuItem(label=f'{s}px')
            si.set_active(self.app.config['size'] == s)
            si.connect('activate', self._on_size_change, s)
            size_menu.append(si)
        size_item.set_submenu(size_menu)
        menu.append(size_item)

        # Thickness
        thick_item = Gtk.MenuItem(label=f'Thickness ({self.app.config["thickness"]}px)')
        thick_menu = Gtk.Menu()
        for t in [1, 2, 3, 4, 5, 6]:
            ti = Gtk.CheckMenuItem(label=f'{t}px')
            ti.set_active(self.app.config['thickness'] == t)
            ti.connect('activate', self._on_thickness_change, t)
            thick_menu.append(ti)
        thick_item.set_submenu(thick_menu)
        menu.append(thick_item)

        # Gap
        gap_item = Gtk.MenuItem(label=f'Gap ({self.app.config["gap"]}px)')
        gap_menu = Gtk.Menu()
        for g in [0, 2, 4, 6, 8, 10]:
            gi_item = Gtk.CheckMenuItem(label=f'{g}px')
            gi_item.set_active(self.app.config['gap'] == g)
            gi_item.connect('activate', self._on_gap_change, g)
            gap_menu.append(gi_item)
        gap_item.set_submenu(gap_menu)
        menu.append(gap_item)

        # Color
        color_item = Gtk.MenuItem(label='Color')
        color_menu = Gtk.Menu()
        for name, rgba in PRESET_COLORS.items():
            c_item = Gtk.MenuItem(label=name)
            c_item.connect('activate', self._on_color_change, rgba)
            color_menu.append(c_item)
        c_item = Gtk.MenuItem(label='Custom...')
        c_item.connect('activate', self._on_custom_color)
        color_menu.append(c_item)
        color_item.set_submenu(color_menu)
        menu.append(color_item)

        # Opacity
        opacity_item = Gtk.MenuItem(label='Opacity')
        opacity_menu = Gtk.Menu()
        current_opacity = self.app.config.get('opacity', 1.0)
        for pct in [100, 90, 80, 70, 60, 50, 40, 30, 20, 10]:
            val = pct / 100.0
            o_item = Gtk.CheckMenuItem(label=f'{pct}%')
            o_item.set_active(abs(current_opacity - val) < 0.05)
            o_item.connect('activate', self._on_opacity_change, val)
            opacity_menu.append(o_item)
        opacity_item.set_submenu(opacity_menu)
        menu.append(opacity_item)

        # Custom image
        img_item = Gtk.MenuItem(label='Custom Image...')
        img_item.connect('activate', self._on_choose_image)
        menu.append(img_item)

        # Image library
        images = find_crosshair_images()
        if images:
            lib_item = Gtk.MenuItem(label='Image Library')
            lib_menu = Gtk.Menu()
            groups = {}
            for img in images:
                group = os.path.basename(os.path.dirname(img))
                groups.setdefault(group, []).append(img)
            for group in sorted(groups.keys()):
                grp_item = Gtk.MenuItem(label=group)
                grp_menu = Gtk.Menu()
                for img in groups[group][:20]:
                    name = os.path.splitext(os.path.basename(img))[0]
                    i_item = Gtk.MenuItem(label=name)
                    i_item.connect('activate', self._on_select_image, img)
                    grp_menu.append(i_item)
                grp_item.set_submenu(grp_menu)
                lib_menu.append(grp_item)
            lib_item.set_submenu(lib_menu)
            menu.append(lib_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Profiles
        prof_item = Gtk.MenuItem(label='Profiles')
        prof_menu = Gtk.Menu()
        profiles = self.app.config.get('profiles', {})
        active = self.app.config.get('active_profile')

        save_item = Gtk.MenuItem(label='Save current position as...')
        save_item.connect('activate', self._on_save_profile)
        prof_menu.append(save_item)

        if profiles:
            prof_menu.append(Gtk.SeparatorMenuItem())
            for name in sorted(profiles.keys()):
                p = profiles[name]
                label = f'{name}  ({p["position_x"]}, {p["position_y"]})'
                if name == active:
                    label = f'* {label}'
                p_item = Gtk.MenuItem(label=label)
                p_item.connect('activate', self._on_load_profile, name)
                prof_menu.append(p_item)

            prof_menu.append(Gtk.SeparatorMenuItem())
            del_item = Gtk.MenuItem(label='Delete profile...')
            del_sub = Gtk.Menu()
            for name in sorted(profiles.keys()):
                d_item = Gtk.MenuItem(label=name)
                d_item.connect('activate', self._on_delete_profile, name)
                del_sub.append(d_item)
            del_item.set_submenu(del_sub)
            prof_menu.append(del_item)

        prof_item.set_submenu(prof_menu)
        menu.append(prof_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Autostart
        autostart_item = Gtk.CheckMenuItem(label='Start on boot')
        autostart_item.set_active(os.path.isfile(AUTOSTART_FILE))
        autostart_item.connect('toggled', self._on_autostart_toggle)
        menu.append(autostart_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Help
        help_item = Gtk.MenuItem(label='Keyboard Shortcuts')
        help_item.connect('activate', self._on_show_help)
        menu.append(help_item)

        menu.append(Gtk.SeparatorMenuItem())

        item = Gtk.MenuItem(label='Quit (Ctrl+Shift+Alt+Q)')
        item.connect('activate', lambda _: Gtk.main_quit())
        menu.append(item)

        menu.show_all()
        return menu

    def _on_shape_change(self, widget, shape):
        if widget.get_active():
            self.app.window.set_shape(shape)
            self.indicator.set_menu(self._build_menu())

    def _on_size_change(self, widget, size):
        if widget.get_active():
            self.app.window.set_size(size)
            self.indicator.set_menu(self._build_menu())

    def _on_thickness_change(self, widget, thickness):
        if widget.get_active():
            self.app.window.set_thickness(thickness)
            self.indicator.set_menu(self._build_menu())

    def _on_gap_change(self, widget, gap):
        if widget.get_active():
            self.app.window.set_gap(gap)
            self.indicator.set_menu(self._build_menu())

    def _on_show_help(self, widget):
        dialog = Gtk.MessageDialog(
            transient_for=self.app.window,
            modal=True,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text='CrossOver GTK - Keyboard Shortcuts',
        )
        dialog.format_secondary_markup(
            '<tt>'
            'Ctrl+Shift+Alt+X         Lock/Unlock\n'
            '                          (click-through, hide UI)\n\n'
            'Ctrl+Shift+Alt+H         Hide/Show crosshair\n\n'
            'Ctrl+Shift+Alt+C         Center on screen\n\n'
            'Ctrl+Shift+Alt+Q         Quit\n\n'
            'Ctrl+Alt+Numpad 8        Nudge up 1px\n'
            'Ctrl+Alt+Numpad 2        Nudge down 1px\n'
            'Ctrl+Alt+Numpad 4        Nudge left 1px\n'
            'Ctrl+Alt+Numpad 6        Nudge right 1px\n\n'
            'Ctrl+Shift+Alt+Numpad 8  Nudge up 10px\n'
            'Ctrl+Shift+Alt+Numpad 2  Nudge down 10px\n'
            'Ctrl+Shift+Alt+Numpad 4  Nudge left 10px\n'
            'Ctrl+Shift+Alt+Numpad 6  Nudge right 10px\n'
            '</tt>\n\n'
            'Run <tt>crossover.py --install-keys</tt> to register shortcuts.\n'
            'Run <tt>crossover.py --uninstall-keys</tt> to remove them.'
        )
        dialog.run()
        dialog.destroy()

    def _on_opacity_change(self, widget, val):
        if widget.get_active():
            self.app.window.set_opacity(val)
            self.indicator.set_menu(self._build_menu())

    def _on_color_change(self, widget, rgba):
        self.app.window.set_color(rgba)

    def _on_custom_color(self, widget):
        dialog = Gtk.ColorChooserDialog(title='Choose Crosshair Color',
                                        transient_for=self.app.window, modal=True)
        current = self.app.config['color']
        dialog.set_rgba(Gdk.RGBA(*current))
        dialog.set_use_alpha(True)
        if dialog.run() == Gtk.ResponseType.OK:
            rgba = dialog.get_rgba()
            self.app.window.set_color([rgba.red, rgba.green, rgba.blue, rgba.alpha])
        dialog.destroy()

    def _on_choose_image(self, widget):
        dialog = Gtk.FileChooserDialog(
            title='Choose Crosshair Image',
            transient_for=self.app.window,
            modal=True,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        ff = Gtk.FileFilter()
        ff.set_name('Images')
        ff.add_mime_type('image/png')
        ff.add_mime_type('image/svg+xml')
        ff.add_pattern('*.png')
        ff.add_pattern('*.svg')
        dialog.add_filter(ff)
        if dialog.run() == Gtk.ResponseType.OK:
            self.app.window.set_image(dialog.get_filename())
            self.indicator.set_menu(self._build_menu())
        dialog.destroy()

    def _on_select_image(self, widget, path):
        self.app.window.set_image(path)
        self.indicator.set_menu(self._build_menu())

    def _on_save_profile(self, widget):
        dialog = Gtk.Dialog(title='Save Profile',
                            transient_for=self.app.window, modal=True)
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
        )
        box = dialog.get_content_area()
        label = Gtk.Label(label='Profile name (e.g. CS2, Valorant):')
        box.add(label)
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        box.add(entry)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            name = entry.get_text().strip()
            if name:
                self.app.window.save_profile(name)
                self.indicator.set_menu(self._build_menu())
        dialog.destroy()

    def _on_load_profile(self, widget, name):
        self.app.window.load_profile(name)
        self.indicator.set_menu(self._build_menu())

    def _on_delete_profile(self, widget, name):
        self.app.window.delete_profile(name)
        self.indicator.set_menu(self._build_menu())

    def _on_autostart_toggle(self, widget):
        enable = widget.get_active()
        setup_autostart(enable)
        self.app.config['autostart'] = enable
        save_config(self.app.config)


# ── App ─────────────────────────────────────────────────────────────

class CrosshairApp:

    COMMANDS = {
        'lock':    lambda self: self.window.toggle_lock(),
        'hide':    lambda self: self.window.toggle_visibility(),
        'center':  lambda self: self.window.center_on_screen(),
        'quit':    lambda self: Gtk.main_quit(),
        'up':      lambda self: self.window.nudge(0, -1),
        'down':    lambda self: self.window.nudge(0, 1),
        'left':    lambda self: self.window.nudge(-1, 0),
        'right':   lambda self: self.window.nudge(1, 0),
        'up10':    lambda self: self.window.nudge(0, -10),
        'down10':  lambda self: self.window.nudge(0, 10),
        'left10':  lambda self: self.window.nudge(-10, 0),
        'right10': lambda self: self.window.nudge(10, 0),
    }

    def __init__(self):
        self.config = load_config()
        self.window = None
        self.tray = None
        self.fifo = None

    def handle_command(self, cmd):
        handler = self.COMMANDS.get(cmd)
        if handler:
            handler(self)

    def run(self):
        # Check if already running. A live instance holds the FIFO open for
        # reading, so O_WRONLY succeeds; ENXIO means the file is a leftover.
        try:
            is_fifo = stat.S_ISFIFO(os.stat(FIFO_PATH).st_mode)
        except OSError:
            is_fifo = False
        if is_fifo:
            try:
                fd = os.open(FIFO_PATH, os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
                print('CrossOver GTK is already running.')
                print('Use tray icon menu or send commands: python3 crossover.py lock')
                sys.exit(0)
            except OSError:
                pass  # stale, we can take over

        for sig in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)

        setup_fifo()
        set_app_icon()
        self.window = CrosshairWindow(self.config)
        self.window.connect('destroy', self._on_quit)

        self.tray = TrayIcon(self)
        self.fifo = FifoListener(self.handle_command)

        if layer_shell_supported():
            backend = 'layer-shell'
        elif USING_XWAYLAND:
            backend = 'XWayland'
        elif IS_WAYLAND:
            backend = 'Wayland/GTK'
        else:
            backend = 'X11'
        print(f'CrossOver GTK {VERSION} ({backend})')
        if USING_XWAYLAND:
            print('  Note: GNOME has no layer-shell and ignores always-on-top for')
            print('  Wayland windows, so the overlay runs on XWayland instead.')
            print('  Set CROSSOVER_NO_XWAYLAND=1 to force a native Wayland window.')
        elif IS_WAYLAND and not layer_shell_supported():
            print('  Note: layer-shell not supported. Using GTK fallback.')
            print('  Positioning: use numpad nudge keys (drag not available).')
        print('  Ctrl+Shift+Alt+X             lock/unlock')
        print('  Ctrl+Shift+Alt+H             hide/show')
        print('  Ctrl+Shift+Alt+C             center')
        print('  Ctrl+Shift+Alt+Q             quit')
        print('  Ctrl+Alt+Numpad 8/2/4/6      nudge 1px')
        print('  Ctrl+Shift+Alt+Numpad        nudge 10px')
        print()
        print('Run with --install-keys to register GNOME shortcuts.')

        try:
            Gtk.main()
        finally:
            self.cleanup()

    def _on_signal(self):
        Gtk.main_quit()
        return GLib.SOURCE_REMOVE

    def _on_quit(self, *args):
        Gtk.main_quit()

    def cleanup(self):
        """Remove the control FIFO. Reached from every exit path — the tray's
        Quit and the 'quit' command call Gtk.main_quit() without ever destroying
        the window, so hanging this off the window's destroy signal missed both
        and left a FIFO that made the next `crossover.py lock` block forever."""
        if self.fifo:
            self.fifo.cleanup()
            self.fifo = None


# ── Main ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) > 1:
        arg = sys.argv[1]

        if arg == '--version':
            print(f'CrossOver GTK {VERSION}')
            sys.exit(0)

        if arg == '--install-keys':
            install_gnome_shortcuts()
            sys.exit(0)

        if arg == '--uninstall-keys':
            uninstall_gnome_shortcuts()
            sys.exit(0)

        if arg in CrosshairApp.COMMANDS:
            send_command(arg)
            sys.exit(0)

        print(f'Unknown argument: {arg}')
        print('Commands: lock, hide, center, quit, up, down, left, right,')
        print('          up10, down10, left10, right10')
        print('Flags:    --install-keys, --uninstall-keys')
        sys.exit(1)

    app = CrosshairApp()
    app.run()
