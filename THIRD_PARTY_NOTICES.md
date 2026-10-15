# Third-party notices

The VTS Natural Expressor source code is licensed under the MIT License in `LICENSE`.
Third-party components used to build the Windows executable retain their own licenses.

## websocket-client

The application uses `websocket-client` (the `websocket-client>=1.8,<2` dependency in
`requirements.txt`), licensed under the Apache License, Version 2.0. A copy is in
`licenses/Apache-2.0.txt`. Project: <https://github.com/websocket-client/websocket-client>.

## Python 3.11

The build script downloads a local Python 3.11 runtime. Python is licensed under the
Python Software Foundation License Version 2. The license text is in
`licenses/Python-PSF-2.txt`.

## Tcl/Tk

The GUI uses Tkinter and the PyInstaller build may include Tcl/Tk runtime components.
Their license terms are in `licenses/Tcl.txt` and `licenses/Tk.txt`.

## PyInstaller

PyInstaller is licensed under GPL-2.0-or-later with a bootloader exception, plus Apache-2.0
for certain files. Its exception permits distributing executables produced from this source
under the application's license, subject to the licenses of bundled dependencies. See
<https://pyinstaller.org/en/latest/license.html>.

## uv

`build_windows.bat` downloads uv to the local `.build_tools` folder to provision Python and
build dependencies. uv is not included in the generated application executable. uv is
dual-licensed under MIT or Apache-2.0: <https://github.com/astral-sh/uv>.

When redistributing a generated executable, include this notice and the `licenses` folder
with it. Do not distribute `vts_token.json`; it contains a user's VTube Studio API token.
