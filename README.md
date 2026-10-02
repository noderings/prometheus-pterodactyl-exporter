# prometheus-pterodactyl-exporter

Scrapes a Pterodactyl application API and exposes `ptero_*` metrics on port 9222.

```
PTERODACTYL_PANEL_URL=http://panel.example PTERODACTYL_APPLICATION_KEY=ptla_... python3 ptero_exporter.py
```

Node series: `ptero_node_memory_bytes`, `ptero_node_memory_allocated_bytes`, `ptero_node_disk_bytes`, `ptero_node_disk_allocated_bytes`, `ptero_node_servers`, `ptero_node_maintenance`.

Server series: `ptero_server_up` labeled `name` and `identifier`. Live CPU and memory stay on the game console websocket; this process uses the application key only.
