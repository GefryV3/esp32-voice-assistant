"""Образец навыка: точное время и дата (раньше жило прямо в brain.py)."""

META = {"name": "clock", "description": "Точное текущее время и дата.", "version": "1.0", "permissions": []}

TOOLS = [
    {"name": "current_time", "description": "Точное текущее время и дата.",
     "parameters": {"type": "OBJECT", "properties": {}}},
]


async def call(name, args, ctx):
    now = ctx.now().replace(tzinfo=None)
    return {"time": f"{now:%H:%M}", "date": f"{now:%d.%m.%Y}", "weekday": now.strftime("%A")}
