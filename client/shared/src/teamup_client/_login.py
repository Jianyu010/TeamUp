import asyncio
import getpass
import warnings
from dataclasses import replace

from ._config import (
    ConfigError,
    locked_config,
    save_account_token,
    verify_config_unchanged,
)
from ._upstream import connect


async def sign_in_profile(path, profile, authenticate, username, password):
    """Authenticate and update the profile while its process holds the lock."""
    config, data, contents = profile
    verify_config_unchanged(path, contents)
    token = await authenticate(username, password)
    save_account_token(path, config, data, contents, token)
    return replace(config, account_token=token, enrollment_key=None)


async def _terminal_login(path, profile, username, password):
    async with connect(profile[0]) as upstream:
        await sign_in_profile(path, profile, upstream.login, username, password)


def login(path, username):
    with locked_config(path) as profile:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("TeamUp password: ")
        except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
            raise ConfigError(
                "A terminal with hidden input is required for login"
            ) from None
        asyncio.run(_terminal_login(path, profile, username, password))
