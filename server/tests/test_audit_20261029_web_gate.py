import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def load(name):
    path = Path(__file__).resolve().parents[2] / 'deploy' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree(tmp_path):
    root = tmp_path / 'tree'
    root.mkdir()
    data = b'<html>signed</html>'
    (root / 'index.html').write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (root / 'manifest.sha256').write_text(f'{digest}  {len(data)}  index.html\n')
    (root / 'manifest.sig').write_bytes(b'fixture')
    trust = tmp_path / 'trust.pem'
    trust.write_text('fixture')
    return root, trust


def test_signed_file_set_is_closed(tmp_path):
    module = load('web_pool_preflight')
    root, trust = tree(tmp_path)
    with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
        assert module.verify_tree(root, trust)[0]
        (root / 'extra.js').write_text('unsigned')
        assert not module.verify_tree(root, trust)[0]


@pytest.mark.parametrize('entry', ['../index.html', '/absolute', 'sub/../index.html', './index.html', 'index.html\\evil'])
def test_pool_rejects_unsafe_paths(tmp_path, entry):
    module = load('web_pool_preflight')
    root, trust = tree(tmp_path)
    (root / 'manifest.sha256').write_text('0' * 64 + '  1  ' + entry + '\n')
    with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
        assert not module.verify_tree(root, trust)[0]


@pytest.mark.parametrize('text', ['', 'not-a-hash  1  index.html\n', '0' * 64 + '  invalid  index.html\n'])
def test_pool_rejects_invalid_manifests(tmp_path, text):
    module = load('web_pool_preflight')
    root, trust = tree(tmp_path)
    (root / 'manifest.sha256').write_text(text)
    with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
        assert not module.verify_tree(root, trust)[0]


def test_duplicate_does_not_hide_unsigned_file(tmp_path):
    module = load('web_pool_preflight')
    root, trust = tree(tmp_path)
    manifest = root / 'manifest.sha256'
    manifest.write_text(manifest.read_text() * 2)
    (root / 'extra.js').write_text('unsigned')
    with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
        assert not module.verify_tree(root, trust)[0]


def test_missing_expected_build_is_fatal(tmp_path):
    module = load('web_pool_preflight')
    assert module.main(['--web-root', str(tmp_path)]) == 6


def test_serving_gate_rejects_extra_file(tmp_path):
    module = load('web_preflight')
    root, _ = tree(tmp_path)
    entries = module.parse_manifest((root / 'manifest.sha256').read_text())
    assert module.check_entries(root, entries) == 1
    (root / 'extra.html').write_text('unsigned')
    with pytest.raises(SystemExit):
        module.check_entries(root, entries)


def test_serving_gate_requires_manifest(tmp_path):
    module = load('web_preflight')
    with pytest.raises(SystemExit):
        module.check(tmp_path, quiet=True)


def test_legacy_nonexecutable_metadata_is_explicitly_allowed(tmp_path):
    module = load('web_pool_preflight')
    root, trust = tree(tmp_path)
    (root / 'BUILD_INFO.json').write_text('{"buildId":"fixture"}')
    with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
        assert module.verify_tree(root, trust)[0]
