"""Bootstrap a git-cloned python-kasa for dev without importing kasa."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone

LOGGER = logging.getLogger(__name__)

MARKER_NAME = '.dev_python_kasa.json'
CLONE_DIR_NAME = 'python-kasa'
SYMLINK_NAME = 'kasa'
DEFAULT_REPO_URL = 'https://github.com/jimboca/python-kasa.git'
_GIT_CANDIDATES = (
    '/usr/local/bin/git',
    '/usr/bin/git',
    '/opt/local/bin/git',
)
_GIT_EXECUTABLE = None


def git_executable():
    """Resolve git binary; PG3 NS often has a minimal PATH without /usr/local/bin."""
    global _GIT_EXECUTABLE
    if _GIT_EXECUTABLE is not None:
        return _GIT_EXECUTABLE

    found = shutil.which('git')
    if found:
        _GIT_EXECUTABLE = found
        return _GIT_EXECUTABLE

    path = os.environ.get('PATH', '')
    extra = os.pathsep.join(
        p for p in ('/usr/local/bin', '/usr/local/sbin', '/opt/local/bin')
        if p not in path.split(os.pathsep)
    )
    if extra:
        found = shutil.which('git', path=f'{extra}{os.pathsep}{path}')
        if found:
            _GIT_EXECUTABLE = found
            return _GIT_EXECUTABLE

    for candidate in _GIT_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _GIT_EXECUTABLE = candidate
            return _GIT_EXECUTABLE

    return None


def default_repo_url(repo_url=None):
    repo = str(repo_url or '').strip()
    return repo or DEFAULT_REPO_URL


def param_enabled(value):
    return str(value or '').strip().lower() == 'true'


def marker_path(plugin_dir):
    return os.path.join(plugin_dir, MARKER_NAME)


def clone_dir(plugin_dir):
    return os.path.join(plugin_dir, CLONE_DIR_NAME)


def symlink_path(plugin_dir):
    return os.path.join(plugin_dir, SYMLINK_NAME)


def kasa_package_dir(plugin_dir):
    return os.path.join(clone_dir(plugin_dir), 'kasa')


def read_marker(plugin_dir):
    path = marker_path(plugin_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as ex:
        LOGGER.warning('Unable to read %s: %s', path, ex)
        return {}
    return data if isinstance(data, dict) else {}


def write_marker(plugin_dir, state):
    path = marker_path(plugin_dir)
    payload = dict(state)
    payload['updated_at'] = datetime.now(timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%SZ'
    )
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write('\n')
    return payload


def _run_git(args, *, cwd=None):
    git_cmd = git_executable()
    if not git_cmd:
        return None, (
            'git not found on PATH (install git or ensure /usr/local/bin is '
            'available to the Node Server process)'
        )
    try:
        proc = subprocess.run(
            [git_cmd, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as ex:
        return None, f'git failed to run: {ex}'
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or '').strip()
        return None, detail or f'git {" ".join(args)} failed'
    return (proc.stdout or '').strip(), None


def git_head(repo_dir):
    out, err = _run_git(['rev-parse', 'HEAD'], cwd=repo_dir)
    if err:
        return None, err
    return out, None


def git_remote_url(repo_dir):
    out, err = _run_git(['config', '--get', 'remote.origin.url'], cwd=repo_dir)
    if err:
        return None, err
    return out, None


def git_clone_or_pull(dest, repo_url):
    """Clone or fast-forward pull repo_url into dest. Returns (changed, head, error)."""
    repo_url = default_repo_url(repo_url)
    if not os.path.isdir(os.path.join(dest, '.git')):
        parent = os.path.dirname(dest)
        os.makedirs(parent, exist_ok=True)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        _, err = _run_git(['clone', repo_url, dest])
        if err:
            return False, None, err
        head, err = git_head(dest)
        return True, head, err

    current_remote, err = git_remote_url(dest)
    if err:
        return False, None, err
    if current_remote and current_remote.rstrip('/') != repo_url.rstrip('/'):
        shutil.rmtree(dest)
        return git_clone_or_pull(dest, repo_url)

    before, err = git_head(dest)
    if err:
        return False, None, err
    _, err = _run_git(['pull', '--ff-only'], cwd=dest)
    if err:
        return False, before, err
    after, err = git_head(dest)
    if err:
        return False, before, err
    return after != before, after, None


def _remove_path(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
        return True
    if os.path.isdir(path):
        shutil.rmtree(path)
        return True
    return False


def _ensure_symlink(plugin_dir):
    link = symlink_path(plugin_dir)
    target = kasa_package_dir(plugin_dir)
    if not os.path.isdir(target):
        return False, f'missing kasa package at {target}'
    rel_target = os.path.join(CLONE_DIR_NAME, 'kasa')
    if os.path.lexists(link):
        if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(target):
            return False, None
        _remove_path(link)
    os.symlink(rel_target, link)
    return True, None


def disable_dev_python_kasa(plugin_dir):
    """Remove dev clone and kasa symlink."""
    changed = False
    link = symlink_path(plugin_dir)
    if os.path.lexists(link):
        _remove_path(link)
        changed = True
    repo = clone_dir(plugin_dir)
    if os.path.exists(repo):
        shutil.rmtree(repo)
        changed = True
    return {
        'changed': changed,
        'head': None,
        'action': 'disabled',
        'error': None,
        'enabled': False,
        'repo': None,
    }


def apply_dev_python_kasa(plugin_dir, enabled, repo_url=None):
    """Enable or disable nested python-kasa clone + kasa symlink."""
    enabled = bool(enabled)
    if not enabled:
        return disable_dev_python_kasa(plugin_dir)

    repo_url = default_repo_url(repo_url)
    dest = clone_dir(plugin_dir)
    changed, head, err = git_clone_or_pull(dest, repo_url)
    if err:
        return {
            'changed': False,
            'head': head,
            'action': 'error',
            'error': err,
            'enabled': True,
            'repo': repo_url,
        }

    symlink_changed, symlink_err = _ensure_symlink(plugin_dir)
    if symlink_err:
        return {
            'changed': changed or symlink_changed,
            'head': head,
            'action': 'error',
            'error': symlink_err,
            'enabled': True,
            'repo': repo_url,
        }

    action = 'updated' if changed else 'enabled'
    return {
        'changed': changed or symlink_changed,
        'head': head,
        'action': action,
        'error': None,
        'enabled': True,
        'repo': repo_url,
    }


def bootstrap_from_marker(plugin_dir):
    """Run git pull + symlink setup from marker before kasa is imported."""
    marker = read_marker(plugin_dir)
    if not marker.get('enabled'):
        return {
            'changed': False,
            'head': None,
            'action': 'skipped',
            'error': None,
        }
    repo_url = default_repo_url(marker.get('repo'))
    result = apply_dev_python_kasa(plugin_dir, True, repo_url=repo_url)
    if result.get('error'):
        LOGGER.error(
            'dev python-kasa bootstrap failed: %s',
            result['error'],
        )
        return result
    if result.get('changed'):
        LOGGER.info(
            'dev python-kasa bootstrap %s at %s',
            result.get('action'),
            (result.get('head') or '')[:12],
        )
    write_marker(plugin_dir, {
        'enabled': True,
        'repo': repo_url,
        'head': result.get('head'),
    })
    return result


def params_require_restart(old_marker, enabled, repo_url):
    """True when a running process must restart to apply param changes."""
    old_enabled = bool(old_marker.get('enabled'))
    old_repo = default_repo_url(old_marker.get('repo')) if old_enabled else None
    new_repo = default_repo_url(repo_url) if enabled else None
    return old_enabled != enabled or old_repo != new_repo


def sync_marker(plugin_dir, enabled, repo_url, result):
    if enabled:
        write_marker(plugin_dir, {
            'enabled': True,
            'repo': default_repo_url(repo_url),
            'head': result.get('head'),
        })
    else:
        write_marker(plugin_dir, {
            'enabled': False,
            'repo': None,
            'head': None,
        })
