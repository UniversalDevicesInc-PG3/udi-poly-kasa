"""Tests for dev python-kasa bootstrap (no live git)."""

from __future__ import annotations

import json
import os
from unittest import mock

import dev_python_kasa_bootstrap as bootstrap


def _write_marker(tmp_path, **state):
    path = tmp_path / bootstrap.MARKER_NAME
    path.write_text(json.dumps(state), encoding='utf-8')
    return path


def test_default_repo_url():
    assert bootstrap.default_repo_url('') == bootstrap.DEFAULT_REPO_URL
    assert bootstrap.default_repo_url('https://example.com/kasa.git') == (
        'https://example.com/kasa.git'
    )


def test_param_enabled():
    assert bootstrap.param_enabled('true') is True
    assert bootstrap.param_enabled('false') is False
    assert bootstrap.param_enabled(None) is False


def test_git_executable_falls_back_to_usr_local_bin(monkeypatch):
    monkeypatch.delenv('PATH', raising=False)
    monkeypatch.setattr(bootstrap, '_GIT_EXECUTABLE', None)
    monkeypatch.setattr(bootstrap.shutil, 'which', lambda name, path=None: None)
    monkeypatch.setattr(
        bootstrap.os.path,
        'isfile',
        lambda p: p == '/usr/local/bin/git',
    )
    monkeypatch.setattr(
        bootstrap.os,
        'access',
        lambda p, mode: p == '/usr/local/bin/git',
    )
    assert bootstrap.git_executable() == '/usr/local/bin/git'


def test_read_write_marker(tmp_path):
    plugin_dir = str(tmp_path)
    assert bootstrap.read_marker(plugin_dir) == {}
    written = bootstrap.write_marker(plugin_dir, {
        'enabled': True,
        'repo': bootstrap.DEFAULT_REPO_URL,
        'head': 'abc123',
    })
    assert written['enabled'] is True
    assert written['head'] == 'abc123'
    assert 'updated_at' in written
    loaded = bootstrap.read_marker(plugin_dir)
    assert loaded['enabled'] is True
    assert loaded['repo'] == bootstrap.DEFAULT_REPO_URL


def test_params_require_restart():
    old = {'enabled': False}
    assert bootstrap.params_require_restart(old, False, None) is False
    assert bootstrap.params_require_restart(old, True, bootstrap.DEFAULT_REPO_URL) is True

    old = {'enabled': True, 'repo': bootstrap.DEFAULT_REPO_URL}
    assert bootstrap.params_require_restart(
        old, True, 'https://github.com/other/python-kasa.git'
    ) is True
    assert bootstrap.params_require_restart(
        old, True, bootstrap.DEFAULT_REPO_URL
    ) is False
    assert bootstrap.params_require_restart(old, False, None) is True


def test_disable_removes_symlink_and_clone(tmp_path):
    plugin_dir = str(tmp_path)
    clone = tmp_path / bootstrap.CLONE_DIR_NAME
    clone.mkdir()
    (clone / 'kasa').mkdir()
    link = tmp_path / bootstrap.SYMLINK_NAME
    link.symlink_to(clone / 'kasa')

    result = bootstrap.apply_dev_python_kasa(plugin_dir, False)
    assert result['changed'] is True
    assert result['action'] == 'disabled'
    assert not link.exists()
    assert not clone.exists()


@mock.patch('dev_python_kasa_bootstrap.git_clone_or_pull')
def test_enable_creates_symlink(mock_git, tmp_path):
    plugin_dir = str(tmp_path)
    kasa_pkg = tmp_path / bootstrap.CLONE_DIR_NAME / 'kasa'
    kasa_pkg.mkdir(parents=True)
    mock_git.return_value = (True, 'deadbeef' * 5, None)

    result = bootstrap.apply_dev_python_kasa(
        plugin_dir, True, repo_url=bootstrap.DEFAULT_REPO_URL
    )
    assert result['error'] is None
    assert result['changed'] is True
    link = tmp_path / bootstrap.SYMLINK_NAME
    assert link.is_symlink()
    assert os.path.realpath(str(link)) == os.path.realpath(str(kasa_pkg))
    mock_git.assert_called_once()


@mock.patch('dev_python_kasa_bootstrap.git_clone_or_pull')
def test_repo_url_change_reclone(mock_git, tmp_path):
    plugin_dir = str(tmp_path)
    kasa_pkg = tmp_path / bootstrap.CLONE_DIR_NAME / 'kasa'
    kasa_pkg.mkdir(parents=True)
    mock_git.return_value = (True, 'cafebabe' * 5, None)

    result = bootstrap.apply_dev_python_kasa(
        plugin_dir,
        True,
        repo_url='https://github.com/other/python-kasa.git',
    )
    assert result['error'] is None
    mock_git.assert_called_once_with(
        bootstrap.clone_dir(plugin_dir),
        'https://github.com/other/python-kasa.git',
    )


@mock.patch('dev_python_kasa_bootstrap.apply_dev_python_kasa')
def test_bootstrap_from_marker_skips_when_disabled(mock_apply, tmp_path):
    plugin_dir = str(tmp_path)
    _write_marker(tmp_path, enabled=False)
    result = bootstrap.bootstrap_from_marker(plugin_dir)
    assert result['action'] == 'skipped'
    mock_apply.assert_not_called()


@mock.patch('dev_python_kasa_bootstrap.apply_dev_python_kasa')
def test_bootstrap_from_marker_pulls_when_enabled(mock_apply, tmp_path):
    plugin_dir = str(tmp_path)
    _write_marker(tmp_path, enabled=True, repo=bootstrap.DEFAULT_REPO_URL)
    mock_apply.return_value = {
        'changed': True,
        'head': 'abc',
        'action': 'updated',
        'error': None,
        'enabled': True,
        'repo': bootstrap.DEFAULT_REPO_URL,
    }
    result = bootstrap.bootstrap_from_marker(plugin_dir)
    assert result['changed'] is True
    mock_apply.assert_called_once_with(
        plugin_dir, True, repo_url=bootstrap.DEFAULT_REPO_URL
    )
    marker = bootstrap.read_marker(plugin_dir)
    assert marker['enabled'] is True
    assert marker['head'] == 'abc'


@mock.patch('dev_python_kasa_bootstrap._run_git')
def test_git_clone_or_pull_clone(mock_run_git, tmp_path):
    dest = tmp_path / bootstrap.CLONE_DIR_NAME

    def fake_git(args, cwd=None):
        if args and args[0] == 'clone':
            dest.mkdir(parents=True)
            (dest / '.git').mkdir()
            (dest / 'kasa').mkdir()
            return '', None
        if args == ['rev-parse', 'HEAD']:
            return 'feedface' * 5, None
        return None, f'unexpected git {args}'

    mock_run_git.side_effect = fake_git
    changed, head, err = bootstrap.git_clone_or_pull(str(dest), bootstrap.DEFAULT_REPO_URL)
    assert err is None
    assert changed is True
    assert head == 'feedface' * 5


@mock.patch('dev_python_kasa_bootstrap._run_git')
def test_git_clone_or_pull_ff_only_error_keeps_head(mock_run_git, tmp_path):
    dest = tmp_path / bootstrap.CLONE_DIR_NAME
    dest.mkdir()
    (dest / '.git').mkdir()
    (dest / 'kasa').mkdir()

    def fake_git(args, cwd=None):
        if args == ['config', '--get', 'remote.origin.url']:
            return bootstrap.DEFAULT_REPO_URL, None
        if args == ['rev-parse', 'HEAD']:
            return 'abc123def456', None
        if args == ['pull', '--ff-only']:
            return None, 'not possible to fast-forward'
        return None, f'unexpected git {args}'

    mock_run_git.side_effect = fake_git
    changed, head, err = bootstrap.git_clone_or_pull(str(dest), bootstrap.DEFAULT_REPO_URL)
    assert err == 'not possible to fast-forward'
    assert changed is False
    assert head == 'abc123def456'
