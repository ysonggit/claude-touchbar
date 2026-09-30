"""ctb command line: bridge | bar | install | uninstall | doctor | probe | preview"""

import sys

USAGE = """usage: ctb <command>
  bridge                 run the bridge (normally a systemd user service)
  bar [--png F] [--sim]  run the Touch Bar renderer (root, or --png/--sim for development)
  install [--dry-run] [--enable] [--no-systemd]   wire hooks + statusLine into ~/.claude/settings.json
  uninstall              reverse `install`
  doctor                 check the installation
  probe pattern|touch DEV    hardware probes for milestone M0
"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "bridge":
        from .bridge import main as m
        return m()
    if cmd == "bar":
        from .bar.main import run
        return run(rest)
    if cmd == "install":
        from .install import install
        return install(rest)
    if cmd == "uninstall":
        from .install import uninstall
        return uninstall(rest)
    if cmd == "doctor":
        from .install import doctor
        return doctor(rest)
    if cmd == "probe":
        from .bar.main import probe
        return probe(rest)
    print(USAGE)
    return 2
