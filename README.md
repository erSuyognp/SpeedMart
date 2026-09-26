# SpeedMart

## Setup

Requires Python 3.11.

Windows (PowerShell):

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

macOS / Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in the secrets.

## Run

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Check it is up: `curl localhost:8000/api/health`
