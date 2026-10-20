"""Навыки Маги: по одному модулю на навык (см. docs/skills.md и jarvis.skills_loader).

Модуль навыка обязан иметь:
    META  = {"name": ..., "description": ..., "version": "1.0", "permissions": [...]}
    TOOLS = [декларации функций Gemini]
    async def call(name, args, ctx) -> dict
Разрешения: net, ha, google, files. Без разрешения соответствующей возможности в ctx нет.
"""
