
from dynaconf import Dynaconf
from lumi.path import CONFIG_PATH

settings = Dynaconf(
    envvar_prefix="DYNACONF",
    settings_files=[str(CONFIG_PATH / "settings.toml"), str(CONFIG_PATH / ".secrets.toml")],
)

# `envvar_prefix` = export envvars with `export DYNACONF_FOO=bar`.
# `settings_files` = Load these files in the order.
