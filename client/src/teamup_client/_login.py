import asyncio
import getpass
import warnings

from ._config import (
    ConfigError,
    locked_config,
    save_account_token,
    verify_config_unchanged,
)
from ._upstream import connect


async def _authenticate(config, username, password):
    async with connect(config) as upstream:
        return await upstream.login(username, password)


def login(path, username):
    with locked_config(path) as (config, data, contents):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("TeamUp password: ")
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            raise ConfigError(
                "A terminal with hidden input is required for login"
            ) from None
        verify_config_unchanged(path, contents)
        token = asyncio.run(_authenticate(config, username, password))
        save_account_token(path, config, data, contents, token)
