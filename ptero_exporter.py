"""Prometheus text exporter for a Pterodactyl panel. Metric prefix is ptero_."""

from __future__ import annotations

import json
import os
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.request import Request, urlopen

ACCEPT = "Application/vnd.pterodactyl.v1+json"


def _get(base: str, key: str, path: str) -> dict[str, Any]:
    request = Request(
        base.rstrip("/") + path,
        headers={"Authorization": f"Bearer {key}", "Accept": ACCEPT},
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def collect(base: str, application_key: str, client_key: str = "") -> str:
    lines = [
        "# HELP ptero_node_memory_bytes Wings node memory limit.",
        "# TYPE ptero_node_memory_bytes gauge",
        "# HELP ptero_node_memory_allocated_bytes Memory assigned to servers on the node.",
        "# TYPE ptero_node_memory_allocated_bytes gauge",
        "# HELP ptero_node_disk_bytes Wings node disk limit.",
        "# TYPE ptero_node_disk_bytes gauge",
        "# HELP ptero_node_disk_allocated_bytes Disk assigned to servers on the node.",
        "# TYPE ptero_node_disk_allocated_bytes gauge",
        "# HELP ptero_node_allocations_free Unassigned allocations.",
        "# TYPE ptero_node_allocations_free gauge",
        "# HELP ptero_node_servers Servers on the node.",
        "# TYPE ptero_node_servers gauge",
        "# HELP ptero_node_memory_overallocate Extra memory percent above the node limit. 0 means sell up to the limit.",
        "# TYPE ptero_node_memory_overallocate gauge",
        "# HELP ptero_node_disk_overallocate Extra disk percent above the node limit. 0 means sell up to the limit.",
        "# TYPE ptero_node_disk_overallocate gauge",
        "# HELP ptero_node_cpu_cores Wings reported CPU cores.",
        "# TYPE ptero_node_cpu_cores gauge",
        "# HELP ptero_node_cpu_allocated Sum of server CPU limits, in cores.",
        "# TYPE ptero_node_cpu_allocated gauge",
        "# HELP ptero_node_maintenance Panel maintenance flag.",
        "# TYPE ptero_node_maintenance gauge",
        "# HELP ptero_server_up Server is installed.",
        "# TYPE ptero_server_up gauge",
        "# HELP ptero_server_cpu_percent CPU use as a percent of the plan limit.",
        "# TYPE ptero_server_cpu_percent gauge",
        "# HELP ptero_server_memory_bytes Current memory.",
        "# TYPE ptero_server_memory_bytes gauge",
        "# HELP ptero_server_memory_limit_bytes Plan memory limit.",
        "# TYPE ptero_server_memory_limit_bytes gauge",
        "# HELP ptero_server_disk_bytes Current disk.",
        "# TYPE ptero_server_disk_bytes gauge",
        "# HELP ptero_server_disk_limit_bytes Plan disk limit.",
        "# TYPE ptero_server_disk_limit_bytes gauge",
        "# HELP ptero_server_network_rx_bytes Bytes received.",
        "# TYPE ptero_server_network_rx_bytes counter",
        "# HELP ptero_server_network_tx_bytes Bytes sent.",
        "# TYPE ptero_server_network_tx_bytes counter",
    ]
    servers_payload = _get(base, application_key, "/api/application/servers?per_page=100")
    server_rows = [item.get("attributes") or {} for item in servers_payload.get("data") or []]
    nodes = _get(base, application_key, "/api/application/nodes?per_page=100")
    for item in nodes.get("data") or []:
        node = item["attributes"]
        node_id = int(node["id"])
        name = _label(node.get("name") or str(node_id))
        allocated = node.get("allocated_resources") or {}
        memory = int(node.get("memory") or 0) * 1024 * 1024
        disk = int(node.get("disk") or 0) * 1024 * 1024
        memory_used = int(allocated.get("memory") or 0) * 1024 * 1024
        disk_used = int(allocated.get("disk") or 0) * 1024 * 1024
        memory_over = int(node.get("memory_overallocate") or 0)
        disk_over = int(node.get("disk_overallocate") or 0)
        free = _free_allocations(base, application_key, node_id)
        servers = int(allocated.get("servers") or 0) if "servers" in allocated else _server_count_rows(server_rows, node_id)
        cpu_cores = _cpu_cores(base, application_key, node)
        cpu_allocated = _cpu_allocated(server_rows, node_id)
        maintenance = 1 if node.get("maintenance_mode") else 0
        labels = f'node="{name}",node_id="{node_id}"'
        lines.append(f"ptero_node_memory_bytes{{{labels}}} {memory}")
        lines.append(f"ptero_node_memory_allocated_bytes{{{labels}}} {memory_used}")
        lines.append(f"ptero_node_disk_bytes{{{labels}}} {disk}")
        lines.append(f"ptero_node_disk_allocated_bytes{{{labels}}} {disk_used}")
        lines.append(f"ptero_node_memory_overallocate{{{labels}}} {memory_over}")
        lines.append(f"ptero_node_disk_overallocate{{{labels}}} {disk_over}")
        lines.append(f"ptero_node_cpu_cores{{{labels}}} {cpu_cores}")
        lines.append(f"ptero_node_cpu_allocated{{{labels}}} {cpu_allocated}")
        lines.append(f"ptero_node_allocations_free{{{labels}}} {free}")
        lines.append(f"ptero_node_servers{{{labels}}} {servers}")
        lines.append(f"ptero_node_maintenance{{{labels}}} {maintenance}")
    for server in server_rows:
        label = f'name="{_label(server.get("name") or "")}",identifier="{_label(server.get("identifier") or "")}"'
        installed = 0 if server.get("status") in {"installing", "install_failed"} else 1
        lines.append(f"ptero_server_up{{{label}}} {installed}")
        lines.extend(_server_usage_lines(base, client_key, server))
    lines.append("")
    return "\n".join(lines)


def _free_allocations(base: str, key: str, node_id: int) -> int:
    page = 1
    free = 0
    while True:
        payload = _get(base, key, f"/api/application/nodes/{node_id}/allocations?per_page=100&page={page}")
        for item in payload.get("data") or []:
            if not item["attributes"].get("assigned"):
                free += 1
        total_pages = int(((payload.get("meta") or {}).get("pagination") or {}).get("total_pages") or 1)
        if page >= total_pages:
            return free
        page += 1


def _server_count_rows(rows: list[dict[str, Any]], node_id: int) -> int:
    return sum(1 for row in rows if int(row.get("node") or 0) == node_id)


def _cpu_allocated(rows: list[dict[str, Any]], node_id: int) -> float:
    """Pterodactyl cpu limit 100 is one core."""
    total = 0.0
    for row in rows:
        if int(row.get("node") or 0) != node_id:
            continue
        limits = row.get("limits") or {}
        total += int(limits.get("cpu") or 0) / 100.0
    return total


def _cpu_cores(base: str, key: str, node: dict[str, Any]) -> int:
    """Wings /api/system cpu_count. 0 when Wings cannot be reached."""
    try:
        node_id = int(node["id"])
        cfg = _get(base, key, f"/api/application/nodes/{node_id}/configuration")
        if isinstance(cfg.get("attributes"), dict):
            cfg = cfg["attributes"]
        token_id = str(cfg.get("token_id") or "")
        token = str(cfg.get("token") or "")
        fqdn = str(node.get("fqdn") or "")
        port = int(node.get("daemon_listen") or node.get("daemonListen") or 0)
        scheme = str(node.get("scheme") or "https")
        if not token or not fqdn or port <= 0:
            return 0
        # Current Wings accepts the node token alone. Older Wings wants token_id.token.
        headers = [f"Bearer {token}"]
        if token_id:
            headers.append(f"Bearer {token_id}.{token}")
        last_error: Exception | None = None
        for authorization in headers:
            request = Request(
                f"{scheme}://{fqdn}:{port}/api/system",
                headers={"Authorization": authorization, "Accept": "application/json"},
            )
            try:
                with urlopen(request, timeout=8, context=ssl._create_unverified_context()) as response:
                    payload = json.load(response)
                return int(payload.get("cpu_count") or 0)
            except Exception as exc:
                last_error = exc
                if getattr(exc, "code", None) not in {401, 403}:
                    return 0
        if last_error is not None:
            return 0
        return 0
    except Exception:
        return 0


def _server_usage_lines(base: str, client_key: str, server: dict[str, Any]) -> list[str]:
    """Live usage for a NodeRings server. Name label is NR-{external id}, same as a VM."""
    external_id = str(server.get("external_id") or "").strip()
    identifier = str(server.get("identifier") or "").strip()
    if not external_id or not identifier or not client_key:
        return []
    try:
        payload = _get(base, client_key, f"/api/client/servers/{identifier}/resources")
    except Exception:
        return []
    attributes = payload.get("attributes") or {}
    resources = attributes.get("resources") or {}
    limits = server.get("limits") or {}
    mib = 1024 * 1024
    cpu_absolute = float(resources.get("cpu_absolute") or 0)
    cpu_limit = float(limits.get("cpu") or 0)
    cpu_percent = (cpu_absolute / cpu_limit * 100) if cpu_limit > 0 else cpu_absolute
    labels = f'name="NR-{_label(external_id)}",identifier="{_label(identifier)}"'
    return [
        f"ptero_server_cpu_percent{{{labels}}} {cpu_percent}",
        f"ptero_server_memory_bytes{{{labels}}} {int(resources.get('memory_bytes') or 0)}",
        f"ptero_server_memory_limit_bytes{{{labels}}} {int(limits.get('memory') or 0) * mib}",
        f"ptero_server_disk_bytes{{{labels}}} {int(resources.get('disk_bytes') or 0)}",
        f"ptero_server_disk_limit_bytes{{{labels}}} {int(limits.get('disk') or 0) * mib}",
        f"ptero_server_network_rx_bytes{{{labels}}} {int(resources.get('network_rx_bytes') or 0)}",
        f"ptero_server_network_tx_bytes{{{labels}}} {int(resources.get('network_tx_bytes') or 0)}",
    ]


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


def panel_instance(base: str) -> str:
    """Host[:port] of the panel URL, matching the Platforms page endpoint key."""
    from urllib.parse import urlparse

    raw = base.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    port = parsed.port
    scheme = parsed.scheme or "http"
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return host
    return f"{host}:{port}"


def _stamp_panel(line: str, panel: str) -> str:
    if not line or line.startswith("#") or " " not in line:
        return line
    metric, rest = line.split(" ", 1)
    if "{" in metric:
        metric = metric.replace("{", '{panel="%s",' % panel, 1)
    else:
        metric = '%s{panel="%s"}' % (metric, panel)
    return f"{metric} {rest}"


def render(base: str, application_key: str, client_key: str = "") -> str:
    """Prometheus text. ptero_up is 0 when the panel API cannot be read."""
    panel = _label(panel_instance(base))
    header = (
        "# HELP ptero_up Panel API scrape succeeded.\n"
        "# TYPE ptero_up gauge\n"
    )
    try:
        body = collect(base, application_key, client_key)
    except Exception as exc:  # noqa: BLE001
        return header + f'ptero_up{{panel="{panel}"}} 0\n# error {exc}\n'
    stamped = "\n".join(_stamp_panel(line, panel) for line in body.splitlines() if line != "")
    return header + f'ptero_up{{panel="{panel}"}} 1\n' + stamped + "\n"


class Handler(BaseHTTPRequestHandler):
    base = ""
    key = ""
    client_key = ""

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in {"/metrics", "/"}:
            self.send_response(404)
            self.end_headers()
            return
        body = render(self.base, self.key, self.client_key).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    Handler.base = os.environ["PTERODACTYL_PANEL_URL"]
    Handler.key = os.environ["PTERODACTYL_APPLICATION_KEY"]
    Handler.client_key = os.environ.get("PTERODACTYL_CLIENT_KEY", "")
    port = int(os.environ.get("PTERODACTYL_EXPORTER_PORT", "9222"))
    host = os.environ.get("PTERODACTYL_EXPORTER_ADDR", "0.0.0.0")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
