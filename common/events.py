"""Monitoring events shared by the collector and manager."""

PANEL_RESTART_MESSAGES = tuple(
    f"{name} 已重启"
    for name in (
        "本面板", "面板", "主管理器", "主机管理面板", "Fleet Panel 主机管理",
        "fleet-panel", "fleet-panel.service", "fleet-panel-manager", "fleet-panel-manager.service",
    )
)
VISIBLE_EVENT = (
    "NOT (kind = 'restart' AND trim(text) IN ("
    + ", ".join("?" for _ in PANEL_RESTART_MESSAGES)
    + "))"
)


def is_panel_restart(kind, message):
    return kind == "restart" and message.strip() in PANEL_RESTART_MESSAGES
