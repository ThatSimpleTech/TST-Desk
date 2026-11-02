"""A small GTK 3 form for the live accessibility test (TD-5220).

Every change is appended to the log file named in argv[1], so the test can
check what really reached the app. The password field logs only a length.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

LOG = open(sys.argv[1], "a", buffering=1)  # noqa: SIM115


def log(msg):
    LOG.write(msg + "\n")


win = Gtk.Window(title="TST Desk Test Form")
win.set_default_size(420, 260)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
win.add(box)

box.pack_start(Gtk.Label(label="Username"), False, False, 0)
user = Gtk.Entry()
user.get_accessible().set_name("Username")
user.connect("changed", lambda e: log(f"username={e.get_text()!r}"))
box.pack_start(user, False, False, 0)

box.pack_start(Gtk.Label(label="Password"), False, False, 0)
pw = Gtk.Entry(visibility=False)
pw.get_accessible().set_name("Password")
pw.connect("changed", lambda e: log(f"password_len={len(e.get_text())}"))
box.pack_start(pw, False, False, 0)

chk = Gtk.CheckButton(label="Remember me")
chk.connect("toggled", lambda c: log(f"remember={c.get_active()}"))
box.pack_start(chk, False, False, 0)

btn = Gtk.Button(label="Sign in")
btn.connect("clicked", lambda b: log("clicked=Sign in"))
box.pack_start(btn, False, False, 0)

win.connect("destroy", Gtk.main_quit)
win.show_all()
log("ready")
Gtk.main()
