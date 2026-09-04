import unittest

from secret_store import (
    CREDENTIAL_KEYS, CredentialStore, clear_secure_credentials,
    load_secure_credentials,
    remove_credentials_from_dict, save_secure_credentials,
)


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[(service, name)] = value

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


class SecureCredentialTests(unittest.TestCase):
    def test_credentials_round_trip_outside_settings_dictionary(self):
        backend = MemoryKeyring()
        store = CredentialStore(backend)
        settings = {name: f"secret-{name}" for name in CREDENTIAL_KEYS}

        save_secure_credentials(settings, store)
        remove_credentials_from_dict(settings)
        self.assertFalse(any(name in settings for name in CREDENTIAL_KEYS))

        load_secure_credentials(settings, store)
        self.assertEqual(settings["google_api_key"], "secret-google_api_key")
        self.assertEqual(
            settings["opensky_client_secret"],
            "secret-opensky_client_secret",
        )

        clear_secure_credentials(store)
        self.assertFalse(any(backend.values.values()))


if __name__ == "__main__":
    unittest.main()
