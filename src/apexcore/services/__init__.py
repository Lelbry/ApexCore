"""Long-running системные сервисы apexcore.

В этом пакете живут не интерфейсные демоны — они обслуживают apexcore-клиентов
через локальные IPC (shared memory) и работают под повышенными привилегиями
(LocalSystem). Главная цель — убрать UAC из горячего пути CLI: разовая
установка сервиса даёт apexcore'у доступ к MSR/PCI-сенсорам без admin-прав
на каждый запуск.

Содержимое:

- :mod:`apexcore.services.shm_layout` — бинарный лэйаут shared-memory
  snapshot'а и FNV-1a 64-bit хеширование sensor-ключей.
- :mod:`apexcore.services.sensord` — Windows-сервис ``apexcore_sensord``:
  держит LHM/PawnIO открытым, раз в N мс пишет snapshot в Global mapping.
- :mod:`apexcore.services.shm_adapter` — read-only клиент Global mapping'а
  для использования внутри apexcore-процессов без admin.
"""
