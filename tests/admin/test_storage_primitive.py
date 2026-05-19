from claw_proxy.admin.storage import EncryptedJsonFile


def test_encrypted_json_file_roundtrip(tmp_path, fernet):
    path = tmp_path / "admin.json"
    storage = EncryptedJsonFile(path, fernet)
    payload = {
        "name": "alice",
        "count": 3,
        "nested": {"enabled": True, "items": ["a", "b"]},
        "none": None,
    }

    storage.write(payload)

    assert storage.read() == payload


def test_encrypted_json_file_returns_default_when_missing(tmp_path, fernet):
    path = tmp_path / "missing.json"
    storage = EncryptedJsonFile(path, fernet)
    default = {"fallback": True}

    assert storage.read(default=default) == default


def test_encrypted_json_file_writes_ciphertext_not_plaintext(tmp_path, fernet):
    path = tmp_path / "secret.json"
    storage = EncryptedJsonFile(path, fernet)
    payload = {"secret": "top-secret", "count": 7}

    storage.write(payload)

    disk_contents = path.read_text()
    assert disk_contents != '{"secret":"top-secret","count":7}'
    assert "top-secret" not in disk_contents


def test_encrypted_json_file_overwrites_existing_file(tmp_path, fernet):
    path = tmp_path / "secret.json"
    storage = EncryptedJsonFile(path, fernet)
    first = {"version": 1, "name": "alice"}
    second = {"version": 2, "name": "bob"}

    storage.write(first)
    storage.write(second)

    assert storage.read() == second
