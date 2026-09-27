"""Build hook: ship the fast native front-end as part of a normal install.

The Python CLI always installs as ``chromectl-py``. When a Go toolchain is
available and the ``client/cx`` source is present (a source or ``git+`` install,
or a wheel built on a machine with Go), we compile the tiny Go front-end at build
time and install it as ``chromectl`` on PATH — so a plain ``pipx install`` sets
up the daemon-backed fast path by default, with no extra step. ``chromectl``
forwards commands to a running ``chromectl daemon`` (~5ms vs ~100ms of Python
boot) and falls back to ``chromectl-py`` automatically when no daemon is up.

If Go isn't available at build time, only ``chromectl-py`` is installed; run
``chromectl-py client install`` to fetch or build the native ``chromectl`` later.

Packaging notes (learned the hard way):
  * The native binary is delivered via setuptools ``scripts=`` so it lands on
    PATH with the exec bit — but the stock ``build_scripts`` command tokenizes
    every script as Python to rewrite shebangs, which errors on a compiled
    binary. We override it to copy bytes verbatim.
  * Console scripts stay STATIC in pyproject (``[project.scripts]``). Declaring
    them ``dynamic`` and setting them here trips a setuptools crash
    (``MinimalDistribution`` has no ``set_defaults``) during the build-requires
    phase, so we only add the native binary via ``scripts=`` here.
"""
import os
import platform
import shutil
import subprocess

from setuptools import setup  # noqa: F401  (imported first so the distutils shim is active)

# Use the distutils shim setuptools installs — importing setuptools._distutils
# directly here trips a setuptools build-requires crash (MinimalDistribution has
# no set_defaults). The shim resolves to the vendored command all the same.
from distutils.command.build_scripts import build_scripts as _build_scripts

HERE = os.path.dirname(os.path.abspath(__file__))
STAGING = "_cxbuild"          # relative, build-time only (see .gitignore / MANIFEST)


class build_scripts(_build_scripts):
    """Copy scripts verbatim and mark them executable, so a compiled binary in
    ``scripts=`` isn't mangled by the stock command's shebang tokenizing."""

    def copy_scripts(self):
        self.mkpath(self.build_dir)
        outfiles = []
        for script in self.scripts:
            outfile = os.path.join(self.build_dir, os.path.basename(script))
            if not self.dry_run:
                shutil.copyfile(script, outfile)
                os.chmod(outfile, os.stat(outfile).st_mode | 0o111)
            outfiles.append(outfile)
        return outfiles, outfiles


def _native_client():
    """Compile client/cx → _cxbuild/chromectl. Return its repo-relative path, or
    None if we can't (no Go, no source, or the build failed)."""
    src = os.path.join(HERE, "client", "cx")
    if not (shutil.which("go") and os.path.isfile(os.path.join(src, "main.go"))):
        return None
    os.makedirs(os.path.join(HERE, STAGING), exist_ok=True)
    name = "chromectl.exe" if platform.system() == "Windows" else "chromectl"
    rel = f"{STAGING}/{name}"                       # setuptools wants /-separated
    out = os.path.join(HERE, STAGING, name)
    try:
        subprocess.run(["go", "build", "-o", out, "."], cwd=src, check=True,
                       env={**os.environ, "CGO_ENABLED": "0"})
    except (subprocess.CalledProcessError, OSError):
        return None
    return rel if os.path.isfile(out) else None


_rel = _native_client()
setup(
    scripts=[_rel] if _rel else [],
    cmdclass={"build_scripts": build_scripts},
)
