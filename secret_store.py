"""Secure storage for service credentials.

The keyring package maps to Windows Credential Manager on Windows and
Keychain on macOS.  Portable builds may explicitly retain credentials in their
settings file when the user chooses portability over secure storage.
"""

from __future__ import annotations


SERVICE_NAME = "MapInABox"
CREDENTIAL_KEYS = (
    "google_api_key",
    "mistral_api_key",
    "here_api_key",
    "ors_api_key",
    "aviationstack_api_key",
    "rapidapi_key",
    "opensky_client_id",
    "opensky_client_secret",
    # Retain and protect credentials saved by earlier MIAB integrations, even
    # when the current build no longer presents those services in Settings.
    "gemini_api_key",
    "aerodatabox_api_key",
)
SECURE = "secure"
PORTABLE_PLAINTEXT = "portable_plaintext"


class SecretStoreError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, backend=None):
        if backend is None:
            try:
                import keyring
                backend = keyring
            except Exception as exc:
                raise SecretStoreError(
                    "Secure credential storage is unavailable.") from exc
        self.backend = backend

    def read(self, name: str) -> str:
        try:
            return self.backend.get_password(SERVICE_NAME, name) or ""
        except Exception as exc:
            raise SecretStoreError(
                "Secure credentials could not be read.") from exc

    def write(self, name: str, value: str) -> None:
        try:
            if value:
                self.backend.set_password(SERVICE_NAME, name, value)
            else:
                try:
                    self.backend.delete_password(SERVICE_NAME, name)
                except Exception:
                    # Deleting a credential which does not exist is harmless.
                    pass
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(
                "Secure credentials could not be saved.") from exc


def load_secure_credentials(settings: dict, store=None) -> None:
    store = store or CredentialStore()
    for name in CREDENTIAL_KEYS:
        value = store.read(name)
        if value:
            settings[name] = value


def save_secure_credentials(settings: dict, store=None) -> None:
    """Save and verify every credential before callers remove plaintext."""
    store = store or CredentialStore()
    for name in CREDENTIAL_KEYS:
        value = str(settings.get(name, "") or "").strip()
        store.write(name, value)
        if value and store.read(name) != value:
            raise SecretStoreError(
                "A credential could not be verified after saving.")


def clear_secure_credentials(store=None) -> None:
    store = store or CredentialStore()
    for name in CREDENTIAL_KEYS:
        store.write(name, "")


def remove_credentials_from_dict(settings: dict) -> None:
    for name in CREDENTIAL_KEYS:
        settings.pop(name, None)
