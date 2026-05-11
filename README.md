# mytunnel

A localhost tunneling service written in Python — similar to ngrok. Expose a local development server to the internet via a lightweight FastAPI relay server and a Python client.

## How It Works

```
Local App (port 8000)  ←→  mytunnel client  ←→  mytunnel server  ←→  Internet
```

The tunnel server receives incoming public requests and forwards them to the client, which proxies them to your local application.

## Tech Stack

- Python 3.10+
- FastAPI + uvicorn (tunnel server)
- requests (client HTTP forwarding)
- python-dotenv

## Setup

### 1. Clone

```bash
git clone https://github.com/samandar-hamrayev/my-tunnel.git
cd my-tunnel
```

### 2. Install

```bash
pip install -e .
# or
pip install -r requirements.txt
```

### 3. Start the tunnel server

```bash
uvicorn server.main:app --host 0.0.0.0 --port 8080
```

### 4. Connect the client

```bash
python cli.py --local-port 8000 --server http://your-server:8080
```

Your local app is now accessible through the tunnel server's public URL.

## Project Structure

```
my-tunnel/
├── server/        # FastAPI tunnel server
├── client/        # Client that proxies to local app
├── common/        # Shared utilities
├── scripts/       # Helper scripts
├── cli.py         # Command-line interface
└── pyproject.toml
```

## License

MIT — [Samandar Hamrayev](https://github.com/samandar-hamrayev)
