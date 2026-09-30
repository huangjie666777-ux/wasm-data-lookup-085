# Wasm Execution Sandbox

Python 3.10 project skeleton. No execution functionality is implemented.

Dependencies are installed independently in each project .venv. Exact versions are in requirements.txt.

Start the skeleton with `.venv/bin/python -m uvicorn sandbox_service.app:app --host 127.0.0.1 --port 8081`.

Run project tests with `.venv/bin/python -m pytest`.

To restore dependencies: `python3 -m venv .venv` then `.venv/bin/python -m pip install -r requirements.txt`. Local .wheels contains the prepared distributions for offline restoration using `--no-index --find-links .wheels`.
