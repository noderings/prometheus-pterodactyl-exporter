"""Prometheus text exporter for a Pterodactyl panel. Metric prefix is ptero_."""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ACCEPT = "Application/vnd.pterodactyl.v1+json"
# Live Wings usage for this many NodeRings servers per scrape. The rest keep their last sample.
_USAGE_BATCH = 40
_INDEX_INTERVAL = 300
_RENDER_INTERVAL = 45
_index_lock = threading.Lock()
_index: dict[str, Any] = {"at": 0.0, "nr": [], "by_node": {}, "cursor": 0, "usage": {}}
_render_lock = threading.Lock()
_rendered = {"body": ""}


def _read_page(base: str, key: str, path: str, per_page: int, page: int) -> tuple[list[dict[str, Any]], int]:
    """One list page. A 400/500 page is split so one bad server does not drop the rest."""
    join = "&" if "?" in path else "?"
    try:
        payload = _get(base, key, f"{path}{join}per_page={per_page}&page={page}")
    except HTTPError as exc:
        if exc.code not in {400, 500} or per_page <= 1:
            if exc.code in {400, 500} and per_page <= 1:
                print(f"skipping {path} page {page}: {exc.code}", flush=True)
                return [], 0
            raise
        factor = 10 if per_page >= 10 else per_page
        smaller = max(per_page // factor, 1)
        start = (page - 1) * factor + 1
        rows: list[dict[str, Any]] = []
        for sub in range(start, start + factor):
            part, _total = _read_page(base, key, path, smaller, sub)
            rows.extend(part)
        return rows, 0
    rows = []
    for item in payload.get("data") or []:
        attrs = item.get("attributes") or {}
        if isinstance(attrs, dict):
            rows.append(attrs)
    total_pages = int(((payload.get("meta") or {}).get("pagination") or {}).get("total_pages") or 1)
    return rows, total_pages


def _nr_name(server: dict[str, Any]) -> str:
    """NR external id for a NodeRings server. Empty unless external_id is NR-*."""
    external_id = str(server.get("external_id") or "").strip()
    if external_id.upper().startswith("NR-"):
        return external_id
    return ""


def _index_servers(base: str, key: str) -> None:
    """All servers, for node totals. Per-server series are only external ids that start with NR-.

    A page that returns 400/500 is split so one bad server does not drop the index.
    """
    nr: list[dict[str, Any]] = []
    by_node: dict[int, dict[str, Any]] = {}
    page = 1
    total_pages = 1
    while page <= total_pages:
        part, reported = _read_page(base, key, "/api/application/servers", 100, page)
        for row in part:
            node_id = int(row.get("node") or 0)
            slot = by_node.setdefault(node_id, {"servers": 0, "cpu": 0.0})
            slot["servers"] += 1
            slot["cpu"] += int((row.get("limits") or {}).get("cpu") or 0) / 100.0
            if _nr_name(row):
                nr.append(row)
        if reported:
            total_pages = reported
        page += 1
    with _index_lock:
        _index["at"] = time.monotonic()
        _index["nr"] = nr
        _index["by_node"] = by_node
    panel_servers = sum(int(slot["servers"]) for slot in by_node.values())
    print(f"server index ready: {len(nr)} noderings, {panel_servers} on the panel", flush=True)


def _refresh_index(base: str, key: str) -> None:
    while True:
        try:
            _index_servers(base, key)
        except Exception as exc:  # noqa: BLE001
            print(f"server index failed: {exc}", flush=True)
        time.sleep(_INDEX_INTERVAL)


def _indexed_servers() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], bool]:
    with _index_lock:
        return list(_index["nr"]), dict(_index["by_node"]), float(_index["at"]) > 0


def _get(base: str, key: str, path: str) -> dict[str, Any]:
    request = Request(
        base.rstrip("/") + path,
        headers={"Authorization": f"Bearer {key}", "Accept": ACCEPT},
    )
    delay = 2.0
    for attempt in range(4):
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


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
        "# HELP ptero_node_deployable 1 when the panel lists the node on GET /api/application/nodes/deployable. Read-only.",
        "# TYPE ptero_node_deployable gauge",
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
    nr_servers, by_node, indexed = _indexed_servers()
    usage = _usage_for_scrape(base, client_key, nr_servers)
    deployable = _deployable_ids(base, application_key)
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
        stats = by_node.get(node_id) if indexed else None
        if stats is None and indexed:
            stats = {"servers": 0, "cpu": 0.0}
        cpu_cores = _cpu_cores(base, application_key, node)
        maintenance = 1 if node.get("maintenance_mode") else 0
        deployable_flag = 1 if node_id in deployable else 0
        labels = f'node="{name}",node_id="{node_id}"'
        lines.append(f"ptero_node_memory_bytes{{{labels}}} {memory}")
        lines.append(f"ptero_node_memory_allocated_bytes{{{labels}}} {memory_used}")
        lines.append(f"ptero_node_disk_bytes{{{labels}}} {disk}")
        lines.append(f"ptero_node_disk_allocated_bytes{{{labels}}} {disk_used}")
        lines.append(f"ptero_node_memory_overallocate{{{labels}}} {memory_over}")
        lines.append(f"ptero_node_disk_overallocate{{{labels}}} {disk_over}")
        lines.append(f"ptero_node_cpu_cores{{{labels}}} {cpu_cores}")
        if stats is not None:
            lines.append(f"ptero_node_cpu_allocated{{{labels}}} {stats['cpu']}")
        if stats is not None:
            lines.append(f"ptero_node_servers{{{labels}}} {stats['servers']}")
        lines.append(f"ptero_node_maintenance{{{labels}}} {maintenance}")
        lines.append(f"ptero_node_deployable{{{labels}}} {deployable_flag}")
    for server in nr_servers:
        nr_name = _nr_name(server)
        label = f'name="{_label(nr_name)}",identifier="{_label(server.get("identifier") or "")}"'
        installed = 0 if server.get("status") in {"installing", "install_failed"} else 1
        lines.append(f"ptero_server_up{{{label}}} {installed}")
        lines.extend(usage.get(nr_name) or _limit_lines(server))
    lines.append("")
    return "\n".join(lines)


def _deployable_ids(base: str, key: str) -> set[int]:
    """Panel node ids from GET /nodes/deployable for a zero-size reservation.

    memory=0 and disk=0 ask which public nodes are not already over their limit.
    Page is omitted so the panel returns the whole list. A 400 is that empty
    result. Other errors fail the scrape.
    """
    try:
        payload = _get(base, key, "/api/application/nodes/deployable?memory=0&disk=0")
    except HTTPError as exc:
        if exc.code == 400:
            return set()
        raise
    ids: set[int] = set()
    for item in payload.get("data") or []:
        attrs = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attrs, dict):
            continue
        raw = attrs.get("id")
        if raw is None:
            continue
        try:
            node_id = int(raw)
        except (TypeError, ValueError):
            continue
        if node_id > 0:
            ids.add(node_id)
    return ids


def _limit_lines(server: dict[str, Any]) -> list[str]:
    nr_name = _nr_name(server)
    identifier = str(server.get("identifier") or "").strip()
    if not nr_name or not identifier:
        return []
    limits = server.get("limits") or {}
    mib = 1024 * 1024
    labels = f'name="{_label(nr_name)}",identifier="{_label(identifier)}"'
    return [
        f"ptero_server_memory_limit_bytes{{{labels}}} {int(limits.get('memory') or 0) * mib}",
        f"ptero_server_disk_limit_bytes{{{labels}}} {int(limits.get('disk') or 0) * mib}",
    ]


def _usage_for_scrape(base: str, client_key: str, servers: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Refresh live usage for a slice of NodeRings servers. Earlier slices keep their last sample."""
    with _index_lock:
        cached = {key: list(lines) for key, lines in _index["usage"].items()}
        cursor = int(_index["cursor"])
    if not servers or not client_key:
        return cached
    count = min(_USAGE_BATCH, len(servers))
    fresh: dict[str, list[str]] = {}
    for offset in range(count):
        server = servers[(cursor + offset) % len(servers)]
        nr_name = _nr_name(server)
        lines = _server_usage_lines(base, client_key, server)
        if nr_name and lines:
            fresh[nr_name] = lines
    with _index_lock:
        _index["cursor"] = (cursor + count) % len(servers)
        _index["usage"].update(fresh)
        cached.update(fresh)
    return cached


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
    """Live usage for a server whose panel name is NR-*."""
    nr_name = _nr_name(server)
    identifier = str(server.get("identifier") or "").strip()
    if not nr_name or not identifier or not client_key:
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
    labels = f'name="{_label(nr_name)}",identifier="{_label(identifier)}"'
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
    """Host[:port] of the panel URL, used as the panel label."""
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


def _refresh_render(base: str, application_key: str, client_key: str) -> None:
    """Rebuild the metrics text off the scrape path. A panel walk takes longer than the scrape timeout."""
    while True:
        try:
            body = render(base, application_key, client_key)
            with _render_lock:
                _rendered["body"] = body
            print("metrics refresh ready", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"metrics refresh failed: {exc}", flush=True)
        time.sleep(_RENDER_INTERVAL)


def _cached_body(panel: str) -> str:
    with _render_lock:
        body = _rendered["body"]
    if body:
        return body
    return (
        "# HELP ptero_up Panel API scrape succeeded.\n"
        "# TYPE ptero_up gauge\n"
        f'ptero_up{{panel="{panel}"}} 0\n'
    )


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
        body = _cached_body(panel_instance(self.base)).encode()
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
    threading.Thread(
        target=_refresh_index,
        args=(Handler.base, Handler.key),
        name="server-index",
        daemon=True,
    ).start()
    threading.Thread(
        target=_refresh_render,
        args=(Handler.base, Handler.key, Handler.client_key),
        name="metrics-refresh",
        daemon=True,
    ).start()
    port = int(os.environ.get("PTERODACTYL_EXPORTER_PORT", "9222"))
    host = os.environ.get("PTERODACTYL_EXPORTER_ADDR", "0.0.0.0")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
