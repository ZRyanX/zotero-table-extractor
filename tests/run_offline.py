"""Run regression tests in a disposable checkout, without user config or services.

Usage: python tests/run_offline.py [--pattern test_*.py]
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test_*.py')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.worker:
        from offline_guard import enable
        enable()
        os.chdir(root)
        sys.path.insert(0, str(root))
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=args.pattern)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1

    with tempfile.TemporaryDirectory(prefix='zotero-offline-tests-') as td:
        snapshot = Path(td) / 'repo'
        snapshot.mkdir()
        ignore = shutil.ignore_patterns('._*', '__pycache__', '*.pyc')
        for name in ('scripts', 'tests', 'benchmarks', 'scratch'):
            if (root / name).is_dir():
                shutil.copytree(root / name, snapshot / name, ignore=ignore)
        for path in root.iterdir():
            if path.is_file() and not path.name.startswith('._') and (path.suffix == '.py' or path.name in ('README.md', 'SKILL.md', 'config.example.json', 'requirements.txt')):
                shutil.copy2(path, snapshot / path.name)
        for name in ('models', 'figures'):
            if (root / name).is_dir():
                (snapshot / name).symlink_to(root / name, target_is_directory=True)
        # Python subprocesses inherit guards before importing application code.
        (snapshot / 'tests' / 'sitecustomize.py').write_text('from offline_guard import enable\nenable()\n')
        env = dict(os.environ)
        env['PYTHONPATH'] = str(snapshot / 'tests') + os.pathsep + env.get('PYTHONPATH', '')
        env['AGENT_BRIDGE_DIR'] = str(Path(td) / 'agent_bridge')
        env['ZOTERO_RUN_LIVE_BROWSER'] = '0'
        return subprocess.run([sys.executable, str(snapshot / 'tests' / 'run_offline.py'), '--worker', '--pattern', args.pattern], env=env, cwd=snapshot).returncode


if __name__ == '__main__':
    sys.exit(main())
