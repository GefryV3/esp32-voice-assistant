"""Образец навыка: бросок кубика («брось кубик», «d20»)."""

import random

META = {"name": "dice", "description": "Бросок игрального кубика с нужным числом граней.",
        "version": "1.0", "permissions": []}

TOOLS = [
    {"name": "roll_dice", "description": "Бросить кубик. sides — число граней (по умолчанию 6), "
     "count — сколько кубиков (по умолчанию 1, не больше 10). Назови результат словами.",
     "parameters": {"type": "OBJECT", "properties": {
         "sides": {"type": "NUMBER"}, "count": {"type": "NUMBER"}}}},
]


async def call(name, args, ctx):
    sides = min(max(int(args.get("sides") or 6), 2), 1000)
    count = min(max(int(args.get("count") or 1), 1), 10)
    rolls = [random.randint(1, sides) for _ in range(count)]
    return {"ok": True, "rolls": rolls, "total": sum(rolls), "sides": sides}
