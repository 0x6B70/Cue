// Freezes the FastAPI backend into a standalone executable with PyInstaller.
//
// Invoked by the `build:backend` npm script. PyInstaller lives inside the
// project's virtualenv, not on PATH, and npm runs scripts through a shell that
// has not activated it -- so calling `pyinstaller` directly fails with "not
// recognized" / "command not found". This resolves the venv's Python explicitly
// (falling back to a PATH Python, for CI that builds without a local .venv) and
// runs PyInstaller as a module, which behaves the same on Windows, macOS and
// Linux.

const { spawnSync } = require('node:child_process');
const { existsSync } = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');

// Virtualenv layout differs by platform: Scripts\python.exe on Windows,
// bin/python everywhere else.
const venvPython =
  process.platform === 'win32'
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : path.join(root, '.venv', 'bin', 'python');

const python = existsSync(venvPython)
  ? venvPython
  : process.platform === 'win32'
    ? 'python'
    : 'python3';

const result = spawnSync(
  python,
  [
    '-m',
    'PyInstaller',
    'build/youtube-queue-backend.spec',
    '--noconfirm',
    '--distpath',
    'build/dist',
    '--workpath',
    'build/work'
  ],
  { cwd: root, stdio: 'inherit' }
);

if (result.error) {
  console.error(`Could not launch PyInstaller via ${python}: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
