"""Only reviewed coordinator bytes can be loaded; enrollment remains explicit."""
import hashlib

import pytest

from agent import local_model_admission as admission


@pytest.mark.parametrize("mode", ["changed_bytes", "group_writable", "symlink"])
def test_untrusted_coordinator_source_fails_closed(tmp_path, mode):
    source = tmp_path.resolve() / "coordinator.py"
    content = b"verified_marker = 42\n"
    source.write_bytes(content)
    source.chmod(0o600)
    digest = hashlib.sha256(content).hexdigest()
    if mode == "changed_bytes":
        source.write_bytes(b"raise AssertionError('must never execute unchecked bytes')\n")
    elif mode == "group_writable":
        source.chmod(0o620)
    else:
        link = source.parent / "coordinator-link.py"
        link.symlink_to(source)
        source = link
    with pytest.raises(admission.LocalModelAdmissionError):
        admission._load_coordinator(str(source), digest)


def test_checked_bytes_are_loaded_without_relying_on_bytecode_cache(tmp_path):
    source = tmp_path.resolve() / "coordinator.py"
    content = b"verified_marker = 42\n"
    source.write_bytes(content)
    source.chmod(0o600)
    loaded = admission._load_coordinator(str(source), hashlib.sha256(content).hexdigest())
    assert loaded.verified_marker == 42


@pytest.mark.parametrize("cfg", [{}, {"local_model_admission": {"enabled": False}}])
def test_absent_or_explicitly_disabled_policy_leaves_local_clients_unchanged(monkeypatch, cfg):
    from hermes_cli import config
    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)
    assert admission.guarded_client_kwargs({"base_url": "http://127.0.0.1:11434/v1"}) is None
