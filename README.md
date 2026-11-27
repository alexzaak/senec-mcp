# senec-mcp

MCP server and CLI for a **SENEC.Home** battery storage system. It talks to the
device on your local network (`lala.cgi`, the same endpoint as its web UI) and
to the **mein-senec.de** cloud.

## MCP tools

| Kind | Tools |
|---|---|
| Live device (read) | `status`, `battery_info`, `battery_cells`, `pv`, `grid`, `system_info`, `system_temps`, `raw_get` |
| Cloud (read) | `cloud_dashboard`, `cloud_history`, `cloud_availability`, `cloud_details`, `cloud_settings_list`, `cloud_reserve`, `cloud_peak_shaving` |
| Write | `safe_charge` (on/off), `set_reserve` (0–50 %), `set_peak_shaving` |

Write tools only return the planned request unless called with `confirm=true`,
and their descriptions tell the model to ask the user first. Installer-only
functions (setup login, test charge/discharge, raw writes) are **not** exposed.

## Configuration

| Variable | Purpose |
|---|---|
| `SENEC_LOCAL_HOST` | Device URL, default `https://senec.home.arpa` (self-signed TLS accepted) |
| `SENEC_CLOUD_URL` | mein-senec.de portal URL, default `https://mein-senec.de` |
| `SENEC_MAIL` / `SENEC_PASSWORD` | mein-senec.de login, needed for the `cloud_*` and settings tools |
| `SENEC_CLOUD_SYSTEM` | Optional system ID if the account has several |

Quote passwords containing `$`, `\` etc. in single quotes when exporting them
from a shell profile.

## Use with OpenCode

Runs straight from this repo with [uv](https://docs.astral.sh/uv/):

```jsonc
"senec": {
  "type": "local",
  "command": [
    "uvx", "--from",
    "git+ssh://git@github.com/alexzaak/senec-mcp.git",
    "senec-mcp"
  ],
  "environment": {
    "SENEC_MAIL": "{env:SENEC_MAIL}",
    "SENEC_PASSWORD": "{env:SENEC_PASSWORD}"
  }
}
```

## CLI and development

The package also ships `senec`, a full CLI (live readings,
snapshots, monitoring, cloud history and settings, installer mode).

```bash
devbox run install   # creates .venv with CLI + MCP server
devbox run test      # needs the device and cloud credentials
```

Details: [`senec_mcp/README.md`](senec_mcp/README.md),
protocol notes in [`SENEC.md`](SENEC.md).
