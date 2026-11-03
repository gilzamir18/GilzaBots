"""The snap's interpreter has to be able to create virtual environments.

``$SNAP/bin`` leads PATH, so ``python`` in the integrated terminal is the app's
own interpreter: a ``venv --copies`` made at build time by the gnome SDK's
``python3``. venv records that interpreter as ``home`` in ``pyvenv.cfg``, and
the SDK is a build-only snap, so ``python -m venv .venv`` linked
``.venv/bin/python`` to a file that does not exist at runtime and failed with
``[Errno 2] No such file or directory``. Once that link is valid, ensurepip
still has to be reachable from the base snap's standard library, with pip's
wheel at the absolute path Debian's ensurepip reads it from.
"""

import pathlib

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _snapcraft():
    return yaml.safe_load((ROOT / "snapcraft.yaml").read_text(encoding="utf-8"))


def test_the_bundled_venv_names_the_runtime_base_interpreter():
    build = _snapcraft()["parts"]["gilzabots"]["override-build"]
    rewrite = "sed -i 's#^home = .*#home = /usr/bin#' \"$CRAFT_PART_INSTALL/pyvenv.cfg\""
    assert rewrite in build
    # The pip installs run before the rewrite, with the interpreter that made the venv.
    assert build.index(rewrite) > build.rindex("-m pip install")


def test_the_snap_stages_ensurepip_and_the_pip_wheel():
    # python3.12-venv carries ensurepip and pulls in python3-pip-whl.
    assert "python3.12-venv" in _snapcraft()["parts"]["gilzabots"]["stage-packages"]


def test_the_snap_exposes_ensurepip_and_the_wheels_at_their_absolute_paths():
    layout = _snapcraft()["layout"]
    assert layout["/usr/lib/python3.12/ensurepip"] == {
        "bind": "$SNAP/usr/lib/python3.12/ensurepip"
    }
    assert layout["/usr/share/python-wheels"] == {
        "bind": "$SNAP/usr/share/python-wheels"
    }
