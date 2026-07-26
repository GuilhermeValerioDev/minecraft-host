# Minecraft Host

A lightweight, containerized Minecraft server setup built around Docker/Podman, designed to make self-hosting simple and portable.

This project started as a personal experiment: **Could a handheld gaming PC (an ASUS ROG Ally running Linux) reliably host a modded Minecraft server for a group of friends?**

It turns out the answer was **yes**.

The result became a reusable hosting environment that can be dropped onto almost any Linux machine with Podman or Docker installed.

---

## Features

- 🐳 Docker & Podman compatible
- 🎮 Supports Vanilla, NeoForge, Forge, Fabric, Paper, and more (via itzg/minecraft-server)
- 📦 Multiple server instances through a single configuration
- 🖥️ Simple management CLI
- 💬 Interactive RCON console
- 🗺️ BlueMap compatible
- 🔄 Syncthing-friendly directory layout
- 🌐 Perfect companion for Tailscale remote hosting

---

## Project Structure

```
minecraft-host/
├── backups/
├── instances/
├── scripts/
│   └── mc
├── compose.yaml
├── .env.example
└── README.md
```

Each server lives inside its own folder under `instances/`.

Switching between servers is as simple as changing the `INSTANCE` value in `.env`.

---

## Requirements

- Docker or Podman
- Docker Compose or Podman Compose
- Linux (tested on CachyOS and Bazzite)

---

## Installation

Clone the repository:

```bash
git clone <repo-url>
cd minecraft-host
```

Create your configuration:

```bash
cp .env.example .env
```

Edit `.env` to match your desired server configuration.

Then start the server:

```bash
./scripts/mc start
```

---

## CLI

The included `mc` script wraps the most common container operations.

```bash
./scripts/mc start
./scripts/mc stop
./scripts/mc restart
./scripts/mc status
./scripts/mc logs
./scripts/mc console
./scripts/mc cmd "say Hello!"
```

---

## BlueMap

BlueMap works out of the box as long as it is installed in your server.

If exposing it externally, remember to publish the BlueMap web port in `compose.yaml`.

---

## Why?

I wanted something that was:

- Easy to recreate
- Easy to back up
- Easy to synchronize between machines
- Independent from any launcher
- Simple enough that I wouldn't forget how it worked six months later

Instead of another collection of shell commands scattered across Discord messages and browser bookmarks, this repository became a complete, self-contained hosting environment.

---

## Tested On

- ASUS ROG Ally
- Bazzite Linux
- CachyOS
- Podman 5.x
- NeoForge 1.21.1
- Vanilla 1.21.1

Although originally built around a handheld PC, nothing here is Ally-specific. Any reasonably modern Linux machine capable of running Podman or Docker should work just as well.

---

## License

MIT