# External Watchdog — Agent Setup Guide

## What it does

Pings the OpenClaw gateway health endpoint (`/health`) from an EXTERNAL machine
on a schedule. When the gateway is unresponsive for N consecutive checks, it
triggers a restart via a webhook or SSH command.

## Why it is required

On-box watchdog (systemd `Restart=on-failure`) cannot recover a process that is
alive but frozen — e.g. stuck in a swap-thrash loop where the kernel is thrashing
pages but the process has not exited. In that state:

- `systemctl status openclaw-analyst` shows `active (running)`
- The gateway accepts no traffic
- Swap thrash events last 2–3 hours on a VM with 8 GB RAM + default swappiness

The only reliable recovery is a restart triggered from OUTSIDE the frozen box.

## Templates

Two templates are provided:

| File | When to use |
|---|---|
| `cloudflare-worker-template.js` | You have a Cloudflare account (free tier sufficient). Runs on Cloudflare's network — survives box freeze. |
| `generic-cron-template.sh` | You have a second Linux machine with SSH access to the gateway box. |

## Setup (Cloudflare Worker)

```bash
# 1. Copy the template
cp docs/watchdog/cloudflare-worker-template.js /tmp/openclaw-watchdog.js

# 2. In Cloudflare dashboard:
#    Workers & Pages -> Create Worker -> paste the file content
#    Settings -> Variables -> add:
#      GATEWAY_HEALTH_URL  = https://<your-gateway-domain>/health
#      RESTART_WEBHOOK_URL = <your-webhook-url>  (see step 3)
#    Triggers -> Cron Triggers -> add:  */5 * * * *

# 3. Create restart webhook (simplest: a Cloudflare Worker or n8n that SSHes to box):
#    The webhook must execute:  systemctl restart openclaw-analyst
#    on the gateway box when called via HTTP POST.

# 4. Signal that watchdog is configured (suppresses install.sh + doctor.sh warnings):
#    Add to your gateway.systemd.env (or the env file your systemd unit sources):
echo "DINOMEM_WATCHDOG_CONFIGURED=1" >> /etc/openclaw/gateway.env
#    Or set it in openclaw.json env block and restart:
#    openclaw config set agents.defaults.env.DINOMEM_WATCHDOG_CONFIGURED 1
#    openclaw gateway restart
```

## Setup (Generic Cron on Second Machine)

```bash
# 1. Copy template to second machine
scp docs/watchdog/generic-cron-template.sh user@monitor-box:/usr/local/bin/openclaw-watchdog.sh
chmod +x /usr/local/bin/openclaw-watchdog.sh

# 2. Edit variables at top of script:
#    DINOMEM_GATEWAY_URL — e.g. https://your-gateway.example.com
#    SSH_HOST            — IP or hostname of the gateway box
#    SSH_KEY             — path to SSH private key on the monitor box
#    CONSECUTIVE_THRESHOLD — number of failures before restart (default 3)

# 3. Add to crontab on the MONITOR box (not the gateway box):
echo "*/5 * * * * bash /usr/local/bin/openclaw-watchdog.sh >> /var/log/openclaw-watchdog.log 2>&1" | crontab -

# 4. Signal configured (on the gateway box):
echo "DINOMEM_WATCHDOG_CONFIGURED=1" >> /etc/openclaw/gateway.env
```

## Verify

```bash
# Check doctor.sh no longer warns:
bash <dinomem-base-dir>/scripts/doctor.sh | grep watchdog
# Expected: [ok]   DINOMEM_WATCHDOG_CONFIGURED is set

# Simulate a failure (on monitor box, temporarily block gateway port):
# Should trigger restart after CONSECUTIVE_THRESHOLD * check interval seconds.
```

## State signal

`DINOMEM_WATCHDOG_CONFIGURED=1` in the gateway's environment is the sole signal
doctor.sh and install.sh check. Set it via any method that survives gateway restart:
- `/etc/openclaw/gateway.env` (sourced by systemd unit)
- `openclaw config set agents.defaults.env.DINOMEM_WATCHDOG_CONFIGURED 1`
