import keyring


class SystemSecrets:
    def get(self, key: str) -> str | None:
        return keyring.get_password("MuWeave", key)

    def set(self, key: str, value: str):
        if value:
            keyring.set_password("MuWeave", key, value)
        elif self.get(key) is not None:
            keyring.delete_password("MuWeave", key)
