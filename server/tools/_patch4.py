def edit(p, pairs):
    s = open(p, encoding='utf-8').read()
    for a, b in pairs:
        assert s.count(a) == 1, (p, a, s.count(a))
        s = s.replace(a, b)
    open(p, 'w', encoding='utf-8').write(s)


edit('jarvis/voice.py', [
('''    async def remind(self, text: str) -> bool:
        """Сказать напоминание вслух. False — колонки нет или она занята, повторим позже."""
        dev = self.hub.get()
        if dev is None or "speaker" not in dev.hello.caps:
            return False
        task = self.start(dev, f"Сработало напоминание: «{text}». Скажи пользователю об этом "
                               "одной короткой фразой, как будто сам вспомнил.")
        return task is not None
''', '''    async def announce(self, prompt: str) -> bool:
        """Колонка сама заговорит по этой подсказке. False — колонки нет или она занята."""
        dev = self.hub.get()
        if dev is None or "speaker" not in dev.hello.caps:
            return False
        return self.start(dev, prompt) is not None

    async def remind(self, text: str) -> bool:
        """Сказать напоминание вслух. False — колонки нет или она занята, повторим позже."""
        return await self.announce(f"Сработало напоминание: «{text}». Скажи пользователю об этом "
                                   "одной короткой фразой, как будто сам вспомнил.")
'''),
('''        "Делай это сразу функцией, а не обещай; после вызова коротко подтверди словами. "''',
 '''        "Делай это сразу функцией, а не обещай; после вызова коротко подтверди словами. "
        "Долгие задачи (поиск в интернете, новости, сравнения, подборки) отдавай в фон функцией "
        "start_task и сразу говори, что взялась: результат ты озвучишь сама, когда он будет готов, "
        "а пока можно говорить о другом. Быстрые вопросы отвечай сразу, без start_task. "'''),
])
edit('jarvis/brain.py', [
('from .ha import TOOLS as HOME_TOOLS, HomeAssistant\n',
 'from .agents import TOOLS as AGENT_TOOLS, Agents\nfrom .ha import TOOLS as HOME_TOOLS, HomeAssistant\n'),
('                 ha: HomeAssistant | None = None):\n        self.now = now\n        self.ha = ha',
 '                 ha: HomeAssistant | None = None, agents: Agents | None = None):\n        self.now = now\n        self.ha = ha\n        self.agents = agents'),
('        return TOOLS + (HOME_TOOLS if self.ha else [])',
 '        return TOOLS + (HOME_TOOLS if self.ha else []) + (AGENT_TOOLS if self.agents else [])'),
('        if name.startswith("home_") and self.ha:\n            return await self.ha.call(name, args)',
 '        if name.startswith("home_") and self.ha:\n            return await self.ha.call(name, args)\n        if name in ("start_task", "list_tasks", "cancel_task") and self.agents:\n            return await self.agents.call(name, args)'),
])
edit('jarvis/config.py', [
('    ha_url: str = ""', '    agent_model: str = "gemini-flash-latest"   # модель фоновых агентов (с поиском Google)\n    ha_url: str = ""'),
('        ha_url=env.get("JARVIS_HA_URL")', '        agent_model=env.get("JARVIS_AGENT_MODEL", "gemini-flash-latest"),\n        ha_url=env.get("JARVIS_HA_URL")'),
])
edit('jarvis/__main__.py', [
('from .ha import HomeAssistant\n', 'from .agents import Agents\nfrom .ha import HomeAssistant\n'),
('        brain.start(voice.remind)',
 '        brain.agents = Agents(settings.gemini_api_key, settings.agent_model, voice.announce)\n'
 '        brain.agents.start_delivery()\n        brain.start(voice.remind)'),
('            await brain.stop()', '            if brain.agents:\n                await brain.agents.stop()\n            await brain.stop()'),
])
edit('.env.example', [('# Куда стучатся', '# Модель фоновых агентов (долгие задачи с поиском в интернете).\n# JARVIS_AGENT_MODEL=gemini-flash-latest\n\n# Куда стучатся')])
