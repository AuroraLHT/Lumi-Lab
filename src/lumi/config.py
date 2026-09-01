from dynaconf import Dynaconf
from lumi.path import CONFIG_PATH, PROJECT_ROOT

_SETTINGS_FILE = CONFIG_PATH / "settings.toml"
_EXAMPLE_FILE = CONFIG_PATH / "settings.example.toml"

# settings.toml is machine-local and git-ignored -- every checkout starts from the
# tracked template. Fail loudly here rather than let dynaconf hand back a config
# full of None and surface as a confusing error deep in a node's startup.
if not _SETTINGS_FILE.exists():
    raise FileNotFoundError(
        f"{_SETTINGS_FILE} not found -- it is machine-local and not tracked in git.\n"
        f"Create it from the tracked template:\n\n"
        f"    cp {_EXAMPLE_FILE.relative_to(PROJECT_ROOT)} {_SETTINGS_FILE.relative_to(PROJECT_ROOT)}\n\n"
        f"then edit your copy (broker address, auth toggles, hardware paths)."
    )

settings = Dynaconf(
    envvar_prefix="DYNACONF",
    settings_files=[str(_SETTINGS_FILE), str(CONFIG_PATH / ".secrets.toml")],
)

# `envvar_prefix` = export envvars with `export DYNACONF_FOO=bar`.
# `settings_files` = Load these files in the order.
