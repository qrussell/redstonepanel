"""Pretend cloudflared: fake.state.tunnel_services, fake.state.tunnel_validate."""
from core.platform import fake


def find(timeout=10):
    return [dict(s) for s in fake.state.tunnel_services]


def restart(service, timeout=60):
    fake.record("tunnel.restart", service)
    for s in fake.state.tunnel_services:
        if s["service"] == service:
            s["running"] = fake.state.tunnel_restart_ok
    return fake.state.tunnel_restart_ok


def validate(program, config, timeout=30):
    fake.record("tunnel.validate", config)
    ok = fake.state.tunnel_validate
    return (ok(config) if callable(ok) else ok), ("OK" if ok is True else "Validation failed")


def default_configs():
    return list(fake.state.tunnel_configs)


def install_hint():
    return "Install cloudflared."
