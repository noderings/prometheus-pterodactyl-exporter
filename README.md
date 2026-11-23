# prometheus-pterodactyl-exporter

A [Prometheus](https://prometheus.io/) exporter for the [Pterodactyl](https://pterodactyl.io/) game server panel. It reads nodes and servers from the panel API and exposes them as `ptero_*` metrics.

It was built for [NodeRings](https://noderings.com/), but it works with any Pterodactyl panel. It is a single Python file with no dependencies beyond the standard library.

## Features

- Node capacity: memory, disk and CPU, both allocated and total, plus overallocation settings.
- Maintenance and deployable flags for each node.
- Live per-server usage (CPU, memory, disk, network) when a client API key is set.
- Scrapes never hit the API. A background thread rebuilds the metrics every 45 seconds, and `/metrics` serves the last result.

## Quick start

### Docker

```bash
docker build -t prometheus-pterodactyl-exporter .
docker run -d -p 9222:9222 \
  -e PTERODACTYL_PANEL_URL=https://panel.example.com \
  -e PTERODACTYL_APPLICATION_KEY=ptla_... \
  -e PTERODACTYL_CLIENT_KEY=ptlc_... \
  prometheus-pterodactyl-exporter
```

### Python

```bash
PTERODACTYL_PANEL_URL=https://panel.example.com \
PTERODACTYL_APPLICATION_KEY=ptla_... \
python3 ptero_exporter.py
```

Requires Python 3.9+.

### Prometheus

```yaml
scrape_configs:
  - job_name: pterodactyl
    scrape_interval: 60s
    static_configs:
      - targets: ["exporter-host:9222"]
```

## Configuration

| Env | Required | Default | Description |
|-----|----------|---------|-------------|
| `PTERODACTYL_PANEL_URL` | yes | | Panel base URL |
| `PTERODACTYL_APPLICATION_KEY` | yes | | Application API key (`ptla_...`) with read access to nodes and servers |
| `PTERODACTYL_CLIENT_KEY` | no | | Client API key (`ptlc_...`) of an admin account. Turns on live per-server usage. |
| `PTERODACTYL_EXPORTER_PORT` | no | `9222` | Listen port |
| `PTERODACTYL_EXPORTER_ADDR` | no | `0.0.0.0` | Listen address |

## How it works

- Every 5 minutes the exporter lists all servers to count servers and CPU limits per node.
- Every 45 seconds it reads nodes, node configuration and `GET /api/application/nodes/deployable`, then rebuilds the metrics.
- Node CPU core counts come from each Wings daemon's `/api/system` endpoint, using the node token from the panel. TLS verification is off for this call because many Wings nodes use self-signed certificates. If Wings is unreachable, `ptero_node_cpu_cores` is `0`.
- Per-server series are only exported for servers whose **external ID** starts with `NR-`. Live usage is fetched through the client API in batches of 40 servers per refresh. Servers outside the current batch keep their last sample.

Every series carries a `panel` label holding the panel's `host[:port]`.

## Metrics

### Panel

| Metric | Description |
|--------|-------------|
| `ptero_up` | 1 when the panel API could be read |

### Nodes

Labels: `panel`, `node`, `node_id`.

| Metric | Description |
|--------|-------------|
| `ptero_node_memory_bytes` | Node memory limit |
| `ptero_node_memory_allocated_bytes` | Memory assigned to servers |
| `ptero_node_disk_bytes` | Node disk limit |
| `ptero_node_disk_allocated_bytes` | Disk assigned to servers |
| `ptero_node_memory_overallocate` | Memory overallocation percent (0 means up to the limit) |
| `ptero_node_disk_overallocate` | Disk overallocation percent |
| `ptero_node_cpu_cores` | CPU cores reported by Wings |
| `ptero_node_cpu_allocated` | Sum of server CPU limits, in cores |
| `ptero_node_servers` | Number of servers on the node |
| `ptero_node_maintenance` | 1 when the node is in maintenance mode |
| `ptero_node_deployable` | 1 when the panel lists the node as deployable for a zero-size reservation |

### Servers

Labels: `panel`, `name` (the server's external ID), `identifier`.

| Metric | Description |
|--------|-------------|
| `ptero_server_up` | 1 when the server is installed |
| `ptero_server_memory_limit_bytes` | Plan memory limit |
| `ptero_server_disk_limit_bytes` | Plan disk limit |
| `ptero_server_cpu_percent` | CPU use as a percent of the plan limit (needs the client key) |
| `ptero_server_memory_bytes` | Current memory use (needs the client key) |
| `ptero_server_disk_bytes` | Current disk use (needs the client key) |
| `ptero_server_network_rx_bytes` | Bytes received (counter, needs the client key) |
| `ptero_server_network_tx_bytes` | Bytes sent (counter, needs the client key) |

## License

Apache License 2.0. See [LICENSE](LICENSE).
